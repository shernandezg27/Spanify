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


def _image_obstacles(page) -> list:
    """
    Recuadros de imágenes que pueden estorbar al texto, para usarlos como límite
    derecho: el texto traducido puede crecer hasta el borde izquierdo de una
    imagen, pero no meterse en ella. Se excluyen los fondos a página completa
    (no son obstáculos: el texto va encima a propósito).

    Esto resuelve el caso en que el original evita una imagen ACORTANDO líneas
    (no hay nada en el texto que marque esa forma); sin esto, el español al
    expandirse crecería hacia la derecha y pisaría la imagen.
    """
    try:
        infos = page.get_image_info()
    except Exception:
        return []
    page_area = page.rect.width * page.rect.height
    rects = []
    for info in infos:
        r = fitz.Rect(info["bbox"])
        if r.is_empty or r.is_infinite:
            continue
        if page_area and (r.width * r.height) > 0.85 * page_area:
            continue  # fondo a página completa
        rects.append(r)
    return rects


def _same_band(a: tuple, b: tuple) -> bool:
    """
    True si dos bboxes están en la misma banda horizontal de texto (la misma
    línea visual), aunque PyMuPDF los haya repartido en 'line'/'block' distintos.
    Se mide por solape vertical: a menudo el original coloca palabras contiguas
    como trozos separados (p. ej. "Templar" y "Commandery", o el número y la
    descripción de una tabla).
    """
    overlap = min(a[3], b[3]) - max(a[1], b[1])
    if overlap <= 0:
        return False
    min_h = min(a[3] - a[1], b[3] - b[1])
    return min_h > 0 and overlap >= 0.5 * min_h


def _extract_spans(page) -> list[dict]:
    page_right = page.rect.x1
    img_rects = _image_obstacles(page)

    # Paso 1: todos los spans no vacíos en orden de documento (bloque->línea->span).
    # Ese orden es el que se manda a traducir y con el que se recolocan, así que
    # debe conservarse.
    raw = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type", 1) != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if span.get("text", "").strip():
                    raw.append(span)

    # Paso 2: el hueco de cada span = distancia al obstáculo más cercano a su
    # derecha (otro span de su MISMA banda vertical en CUALQUIER parte de la
    # página, una imagen, o el borde de la página). Mirar solo dentro de la
    # 'line' de PyMuPDF dejaba crecer el texto traducido sobre palabras que en el
    # original están en líneas/bloques separados pero a la misma altura.
    spans = []
    for span in raw:
        bbox = tuple(span["bbox"])
        origin = tuple(span.get("origin", (bbox[0], bbox[3])))
        right = page_right
        for other in raw:
            if other is span:
                continue
            ob = other["bbox"]
            if ob[0] > bbox[0] + 1 and _same_band(bbox, ob):
                right = min(right, ob[0])
        # Nunca por debajo del ancho original del span (ese hueco ya lo ocupaba el
        # inglés): evita condensar de más frente a un vecino de texto pegado.
        right = max(right, bbox[2])
        # Una imagen a la derecha sí puede recortar por debajo (no escribir encima).
        for ir in img_rects:
            if ir.x0 > bbox[0] and ir.y0 < bbox[3] and ir.y1 > bbox[1]:
                right = min(right, ir.x0)
        avail_width = max(right - origin[0], 1.0)
        spans.append({
            "text": span.get("text", ""),
            "bbox": bbox,
            "origin": origin,
            "font": span.get("font", ""),
            "size": float(span.get("size", 11.0)),
            "color": int(span.get("color", 0)),
            "flags": int(span.get("flags", 0)),
            "avail_width": float(avail_width),
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


# El texto traducido casi siempre ocupa más que el original (el español se
# alarga respecto al inglés). En vez de encoger unas líneas sí y otras no —lo
# que deja alturas desiguales—, reducimos el tamaño de TODO el texto por un
# mismo factor global: la altura queda uniforme en todo el documento y, al
# partir de un tamaño menor, casi nunca hace falta condensar a lo ancho. El
# ajuste fino que quede se hace solo condensando (sin volver a tocar el tamaño).
GLOBAL_SIZE_FACTOR = 0.80  # -20 % de tamaño, igual para todo el documento
MIN_HSCALE = 0.50          # suelo de condensado horizontal (rara vez se alcanza)


def _fit_scale(text_width: float, avail_width, size: float) -> tuple[float, float]:
    """
    Devuelve (escala_horizontal, tamaño_a_dibujar) para reinsertar un texto.

    - El tamaño se reduce SIEMPRE por GLOBAL_SIZE_FACTOR (uniforme en todo el
      documento), para que la altura sea igual entre líneas.
    - El ajuste al hueco disponible se hace solo condensando horizontalmente
      (hasta MIN_HSCALE). En expansiones extremas se acepta un mínimo desborde
      antes que romper la uniformidad de tamaño.

    `text_width` se mide al tamaño original; al dibujar al tamaño reducido, el
    ancho se reduce en la misma proporción.
    """
    draw_size = size * GLOBAL_SIZE_FACTOR
    scaled_width = text_width * GLOBAL_SIZE_FACTOR
    if not avail_width or avail_width <= 0 or scaled_width <= 0 or scaled_width <= avail_width:
        return 1.0, draw_size
    hscale = max(avail_width / scaled_width, MIN_HSCALE)
    return hscale, draw_size


def _place_span(page, entry) -> bool:
    """
    Reinserta un span traducido anclado a su línea base (origin) con una fuente
    sustituta embebida elegida por estilo (serif/sans + negrita/cursiva).

    El tamaño se reduce por un factor global uniforme (ver _fit_scale), de modo
    que la altura del texto es la misma en todo el documento. Si tras esa
    reducción el fragmento aún no cabe en su hueco (avail_width), se condensa
    horizontalmente lo justo para no salirse ni pisar lo contiguo, sin volver a
    tocar el tamaño (así no aparecen alturas desiguales entre líneas).
    """
    text, bbox, origin, font_name, size, color, flags = entry[:7]
    avail_width = entry[7] if len(entry) >= 8 else None
    if not text:
        return False
    primary = substitute_font(flags)
    fallback = broad_font()
    color_tuple = normalize_color(color)

    runs = _glyph_runs(text, primary, fallback)
    total_width = sum(rf.text_length(rt, fontsize=size) for rf, rt in runs)
    hscale, draw_size = _fit_scale(total_width, avail_width, size)

    writer = fitz.TextWriter(page.rect, color=color_tuple)
    x, y = origin[0], origin[1]
    placed = False
    for run_font, run_text in runs:
        try:
            writer.append(fitz.Point(x, y), run_text, font=run_font, fontsize=draw_size)
            x += run_font.text_length(run_text, fontsize=draw_size)
            placed = True
        except Exception:
            continue
    if not placed:
        return False
    if hscale != 1.0:
        # Compresión solo horizontal (la 'y' no se toca) pivotando en el origin,
        # así la línea base se mantiene y el texto encoge hacia la derecha.
        pivot = fitz.Point(origin[0], origin[1])
        writer.write_text(page, morph=(pivot, fitz.Matrix(hscale, 1)))
    else:
        writer.write_text(page)
    return True


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


# Orden canónico de una entrada de span:
#   (text, bbox, origin, font, size, color, flags, avail_width)
# avail_width puede ser None (sin restricción de ancho).

def _entry_to_saved(entry) -> list:
    text, bbox, origin, font, size, color, flags = entry[:7]
    avail = entry[7] if len(entry) >= 8 else None
    return [text, list(bbox), list(origin), font, size, color, flags, avail]


def _entry_from_saved(e) -> tuple:
    avail = None
    if len(e) >= 8:
        text, bbox, origin, font, size, color, flags, avail = e[:8]
    elif len(e) == 7:
        # Checkpoint sin avail_width: sin restricción de ancho.
        text, bbox, origin, font, size, color, flags = e
    else:
        # Checkpoint antiguo sin origin: derivarlo del bbox (esquina inferior izda.).
        text, bbox, font, size, color, flags = e[0], e[1], e[2], e[3], e[4], e[5]
        origin = (bbox[0], bbox[3])
    return (text, tuple(bbox), tuple(origin), font, float(size), int(color), int(flags),
            None if avail is None else float(avail))


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
                        span["avail_width"],
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
