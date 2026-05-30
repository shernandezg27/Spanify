"""
Traductor de PDF al español de España: extrae spans con PyMuPDF, traduce
en bloques con Gemini y reconstruye el PDF preservando posiciones y estilos.
"""

import json
import re
import time
from pathlib import Path

import fitz

from .common import (
    setup_gemini,
    send_chunk,
    split_into_chunks,
    mock_translate_enabled,
    APIKeyError,
    CuotaAgotadaError,
    TraductorError,
    MAX_CHUNK_CHARS,
    REQUEST_DELAY,
    PREV_CTX_CHARS,
    UPLOADS_DIR,
    CHECKPOINTS_DIR,
)
from .prompts import PDF_TRANSLATE, PREV_CONTEXT
from .fonts import substitute_font, broad_font


def normalize_color(color: int) -> tuple[float, float, float]:
    # PyMuPDF empaqueta el color como int 0xRRGGBB; los métodos de dibujo esperan floats 0..1.
    if color is None:
        return (0.0, 0.0, 0.0)
    r = (color >> 16) & 0xFF
    g = (color >> 8) & 0xFF
    b = color & 0xFF
    return (r / 255.0, g / 255.0, b / 255.0)


def _extract_spans(page) -> list[dict]:
    spans = []
    data = page.get_text("dict")
    for block in data.get("blocks", []):
        if block.get("type", 1) != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "")
                if not text.strip():
                    continue
                bbox = tuple(span["bbox"])
                # origin = punto de la línea base donde empieza el span; es lo que
                # usamos para reinsertar el texto. Si falta, lo derivamos del bbox.
                origin = tuple(span.get("origin", (bbox[0], bbox[3])))
                spans.append({
                    "text": text,
                    "bbox": bbox,
                    "origin": origin,
                    "font": span.get("font", ""),
                    "size": float(span.get("size", 11.0)),
                    "color": int(span.get("color", 0)),
                    "flags": int(span.get("flags", 0)),
                })
    return spans


def _glyph_runs(text: str, primary, fallback):
    """
    Parte el texto en tramos (run) consecutivos que comparten fuente: usa la
    fuente PRINCIPAL (la elegida por estilo) para cada carácter que tenga, y la
    FALLBACK de cobertura amplia solo para algún glifo raro que le falte. Ambas
    son fuentes nuestras correctamente embebidas, así que el texto sale bien.
    """
    runs: list[tuple[object, str]] = []
    for ch in text:
        font = primary if primary.has_glyph(ord(ch)) else fallback
        if runs and runs[-1][0] is font:
            runs[-1] = (font, runs[-1][1] + ch)
        else:
            runs.append((font, ch))
    return runs


def _place_span(page, entry) -> bool:
    """
    Reinserta un span traducido anclado a su línea base (origin) con una fuente
    sustituta embebida elegida por estilo (serif/sans + negrita/cursiva). Se
    respeta el cuerpo original (sin reescalar): preservar los tamaños evita el
    efecto de "tamaños arbitrarios".
    """
    text, bbox, origin, font_name, size, color, flags = entry
    if not text:
        return False
    primary = substitute_font(flags)
    fallback = broad_font()
    color_tuple = normalize_color(color)

    writer = fitz.TextWriter(page.rect, color=color_tuple)
    x, y = origin[0], origin[1]
    placed = False
    for run_font, run_text in _glyph_runs(text, primary, fallback):
        try:
            writer.append(fitz.Point(x, y), run_text, font=run_font, fontsize=size)
            x += run_font.text_length(run_text, fontsize=size)
            placed = True
        except Exception:
            continue
    if placed:
        writer.write_text(page)
    return placed


def _apply_page_spans(page, translated_spans: list) -> None:
    # apply_redactions elimina, junto al texto, las anotaciones Link que solapan
    # (p.ej. los enlaces del índice). Las guardamos antes y las reinsertamos
    # después para no romper la navegación del documento.
    saved_links = page.get_links()

    # Primero anotamos todas las redacciones y luego las aplicamos en bloque;
    # si insertásemos texto antes del apply_redactions, se borraría con ellas.
    #
    # images=0, graphics=0: las redacciones SOLO borran el texto original. Con los
    # valores por defecto (images=2, graphics=1) PyMuPDF re-codifica cada imagen
    # tocada por una caja (tamaño x50) y borra los vectores cubiertos (páginas en
    # blanco). En un manual el texto va sobre arte a página completa, así que esos
    # defaults arrasan con el documento.
    for entry in translated_spans:
        bbox = entry[1]
        page.add_redact_annot(fitz.Rect(*bbox))
    page.apply_redactions(images=0, graphics=0)

    for entry in translated_spans:
        _place_span(page, entry)

    for link in saved_links:
        try:
            page.insert_link(link)
        except Exception:
            pass


def _load_checkpoint(progress_path: Path):
    if not progress_path.exists():
        return None
    try:
        return json.loads(progress_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_checkpoint(progress_path: Path, state: dict) -> None:
    progress_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


# Orden canónico de una entrada de span: (text, bbox, origin, font, size, color, flags).

def _entry_to_saved(entry) -> list:
    text, bbox, origin, font, size, color, flags = entry
    return [text, list(bbox), list(origin), font, size, color, flags]


def _entry_from_saved(e) -> tuple:
    if len(e) >= 7:
        text, bbox, origin, font, size, color, flags = e[0], e[1], e[2], e[3], e[4], e[5], e[6]
    else:
        # Checkpoint antiguo sin origin: derivarlo del bbox (esquina inferior izda.).
        text, bbox, font, size, color, flags = e[0], e[1], e[2], e[3], e[4], e[5]
        origin = (bbox[0], bbox[3])
    return (text, tuple(bbox), tuple(origin), font, float(size), int(color), int(flags))


_NUMBERED_RE = re.compile(r"\[\[\s*(\d+)\s*\]\]\s*(.*)")


def _parse_numbered_response(resp: str, chunk: list[str]) -> list[str]:
    """
    Parsea la respuesta numerada del modelo ([[N]] traducción) emparejando cada
    traducción con su fragmento por su NÚMERO, no por su posición. Así, si el
    modelo fusiona, divide o se salta algún fragmento, el resto NO se desplaza:
    los huecos se rellenan con el texto original (mantiene la alineación con los
    spans del PDF, que es lo que rompía la maquetación al desajustarse).
    """
    by_idx: dict[int, str] = {}
    for line in resp.splitlines():
        m = _NUMBERED_RE.match(line.strip())
        if not m:
            continue
        idx = int(m.group(1))
        if 0 <= idx < len(chunk):
            by_idx[idx] = m.group(2).strip()
    return [by_idx.get(i) or chunk[i] for i in range(len(chunk))]


def _translate_page_spans(
    client, spans: list[dict], prev_context: str
) -> tuple[list[str], str]:
    if not spans:
        return [], prev_context

    spans_text = [s["text"] for s in spans]

    # Modo mock: devuelve el texto sin traducir para probar la reconstrucción
    # del PDF sin llamar a Gemini (ver SPANIFY_MOCK_TRANSLATE en common.py).
    if mock_translate_enabled():
        return spans_text, prev_context

    chunks = split_into_chunks(spans_text, MAX_CHUNK_CHARS)

    translated: list[str] = []
    for chunk in chunks:
        ctx_block = PREV_CONTEXT.format(prev=prev_context.strip()) if prev_context.strip() else ""
        prompt = PDF_TRANSLATE.format(
            context=ctx_block,
            fragmentos="\n".join(f"[[{i}]] {frag}" for i, frag in enumerate(chunk)),
        )
        resp = send_chunk(client, prompt)
        parts = _parse_numbered_response(resp, chunk)

        translated.extend(parts)
        joined = " ".join(parts)
        prev_context = (prev_context + " " + joined)[-PREV_CTX_CHARS:]
        time.sleep(REQUEST_DELAY)

    return translated, prev_context


def translate_pdf(input_path_str: str, api_key: str | None = None, progress_cb=None) -> Path:
    def report(pct: int, msg: str):
        if progress_cb:
            try:
                progress_cb(pct, msg)
            except Exception:
                pass

    report(0, "Inicializando...")

    input_path = Path(input_path_str)
    if not input_path.exists():
        raise TraductorError(f"No se encuentra el archivo: {input_path}")

    stem = input_path.stem
    uploads_abs = UPLOADS_DIR.resolve()
    output_path = uploads_abs / f"{stem}_es.pdf"
    partial_path = uploads_abs / f"{stem}_es.partial.pdf"
    progress_path = CHECKPOINTS_DIR.resolve() / f"{stem}.progress"

    if output_path.exists():
        report(100, "Ya estaba traducido.")
        return output_path

    client = setup_gemini(api_key)

    doc = fitz.open(str(input_path))
    total_pages = doc.page_count

    state = _load_checkpoint(progress_path)
    start_page = 0
    prev_context = ""

    if state and not output_path.exists():
        saved_pages = state.get("pages", {})
        for page_idx_str, entries in saved_pages.items():
            page_idx = int(page_idx_str)
            if page_idx >= total_pages:
                continue
            translated_spans = [_entry_from_saved(e) for e in entries]
            try:
                _apply_page_spans(doc[page_idx], translated_spans)
            except Exception as exc:
                print(f"    ⚠ Error reaplicando página {page_idx}: {exc}")
        start_page = int(state.get("last_page", -1)) + 1
        prev_context = state.get("prev_context", "")
        report(
            int(start_page / max(total_pages, 1) * 100),
            f"Reanudando desde la página {start_page + 1}/{total_pages}...",
        )
    else:
        state = {"last_page": -1, "pages": {}, "prev_context": ""}

    report(
        int(start_page / max(total_pages, 1) * 100),
        f"Traduciendo {total_pages} páginas...",
    )

    try:
        for page_idx in range(start_page, total_pages):
            page = doc[page_idx]
            try:
                spans = _extract_spans(page)
                translated_texts, prev_context = _translate_page_spans(client, spans, prev_context)

                translated_spans = []
                for span, text in zip(spans, translated_texts):
                    translated_spans.append((
                        text or span["text"],
                        span["bbox"],
                        span["origin"],
                        span["font"],
                        span["size"],
                        span["color"],
                        span["flags"],
                    ))

                _apply_page_spans(page, translated_spans)

                state["pages"][str(page_idx)] = [
                    _entry_to_saved(entry) for entry in translated_spans
                ]
                state["last_page"] = page_idx
                state["prev_context"] = prev_context
                _save_checkpoint(progress_path, state)
                # NOTA: no guardamos el PDF parcial aquí. Guardar el documento en
                # cada página vuelve a subsetear las fuentes Noto embebidas y las
                # corrompe (texto ilegible / GIDs crudos). El reanudado se
                # reconstruye desde el checkpoint JSON, no desde el PDF parcial,
                # así que el único guardado del PDF es el final.

            except (CuotaAgotadaError, APIKeyError, KeyboardInterrupt):
                raise
            except Exception as exc:
                print(f"    ⚠ Error en la página {page_idx + 1}: {exc}. Continuando.")

            pct = int((page_idx + 1) / total_pages * 100)
            report(pct, f"Página {page_idx + 1}/{total_pages} traducida.")

        doc.save(str(partial_path), garbage=4, deflate=True, incremental=False)
        doc.close()

        if partial_path.exists():
            if output_path.exists():
                output_path.unlink()
            partial_path.rename(output_path)

        if progress_path.exists():
            progress_path.unlink()

        report(100, "Traducción completada.")
        return output_path

    except (CuotaAgotadaError, APIKeyError, KeyboardInterrupt):
        try:
            doc.save(str(partial_path), garbage=4, deflate=True, incremental=False)
        except Exception:
            pass
        doc.close()
        raise
