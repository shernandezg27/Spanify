"""
Traductor de EPUB inglés → español (castellano) con Google Gemini.

A Gemini solo viaja el TEXTO del libro: cada bloque (<p>, <h1>, <li>…) se
serializa a texto plano con marcadores ligeros <tN> en lugar de los tags
inline, y se envía en fragmentos numerados [[N]]. Los tags reales y sus
atributos nunca salen de Python, así que el modelo no puede romper el XHTML;
el emparejado por número + la validación de marcadores evitan que los bloques
se pierdan o se desplacen (ante cualquier fallo, el bloque conserva su
contenido original).
"""

import copy
import json
import re
import zipfile
import shutil
import time
from pathlib import Path
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup, NavigableString, Tag
from tqdm import tqdm

from .common import (
    setup_gemini,
    send_chunk,
    split_into_chunks,
    parse_numbered_response,
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
from .prompts import EPUB_TRANSLATE, PREV_CONTEXT


BLOCK_TAGS = [
    "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "td", "th", "blockquote", "figcaption",
    "cite", "dt", "dd", "title"
]
SKIP_TAGS = {"script", "style", "code", "pre", "kbd", "var", "samp"}
HEADING_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6"]

_WS_RE = re.compile(r"\s+")
_MARKER_RE = re.compile(r"<\s*t(\d+)\s*(/?)\s*>|<\s*/\s*t(\d+)\s*>")


def has_translatable_text(element) -> bool:
    if element.name in SKIP_TAGS:
        return False
    for parent in element.parents:
        if hasattr(parent, 'name') and parent.name in SKIP_TAGS:
            return False
    return bool(element.get_text(strip=True))


def outermost_blocks(soup) -> list:
    """
    Bloques traducibles que no están dentro de otro bloque ya seleccionado:
    si un <blockquote> contiene <p>, se procesa solo el blockquote (el <p>
    viaja dentro como marcador), evitando traducir dos veces el mismo texto.
    """
    blocks = [b for b in soup.find_all(BLOCK_TAGS) if has_translatable_text(b)]
    selected = {id(b) for b in blocks}
    return [b for b in blocks if not any(id(p) in selected for p in b.parents)]


# ── Serialización texto ↔ marcadores ──────────────────────────────────────────

def _is_opaque(tag) -> bool:
    """
    Tags cuyo interior no debe viajar al modelo: sin texto traducible
    (<br/>, <img/>, spans vacíos) o de SKIP_TAGS (<code>…). Se envían como
    marcador suelto <tN/> y se restauran íntegros desde el original.
    """
    return tag.name in SKIP_TAGS or not tag.get_text(strip=True)


def serialize_block(block) -> tuple[str, dict]:
    """
    Convierte el contenido de un bloque en texto plano con marcadores: cada tag
    inline se sustituye por <tN>…</tN> (o <tN/> si es opaco) y se guarda
    {N: nodo original} para restaurarlo con todos sus atributos al deserializar.
    El whitespace se colapsa (inocuo fuera de <pre>, que va en SKIP_TAGS, y
    ahorra tokens).
    """
    parts: list[str] = []
    tag_map: dict[int, object] = {}

    def walk(node) -> None:
        for child in node.children:
            if isinstance(child, Tag):
                idx = len(tag_map) + 1
                tag_map[idx] = child
                if _is_opaque(child):
                    parts.append(f"<t{idx}/>")
                else:
                    parts.append(f"<t{idx}>")
                    walk(child)
                    parts.append(f"</t{idx}>")
            elif type(child) is NavigableString:
                parts.append(str(child))
            else:
                # Comentarios, CData…: opacos, se restauran tal cual.
                idx = len(tag_map) + 1
                tag_map[idx] = child
                parts.append(f"<t{idx}/>")

    walk(block)
    return _WS_RE.sub(" ", "".join(parts)).strip(), tag_map


def deserialize_block(text: str, tag_map: dict, soup) -> list | None:
    """
    Inversa de serialize_block sobre el texto TRADUCIDO: reconstruye la lista
    de nodos restaurando los tags originales del mapa. Devuelve None si los
    marcadores no cuadran (falta alguno, sobra, se repite o está mal anidado);
    el llamante conserva entonces el contenido original del bloque.
    """
    root: list = []
    containers: list = [root]  # el root (list) o los Tags abiertos
    stack: list[int] = []
    seen: set[int] = set()

    pos = 0
    for m in _MARKER_RE.finditer(text):
        literal = text[pos:m.start()]
        pos = m.end()
        if literal:
            containers[-1].append(NavigableString(literal))
        if m.group(3) is not None:  # cierre </tN>
            idx = int(m.group(3))
            if not stack or stack[-1] != idx:
                return None
            stack.pop()
            containers.pop()
            continue
        idx = int(m.group(1))
        self_closing = m.group(2) == "/"
        orig = tag_map.get(idx)
        if orig is None or idx in seen:
            return None
        seen.add(idx)
        if not isinstance(orig, Tag) or _is_opaque(orig):
            containers[-1].append(copy.copy(orig))
            if not self_closing:
                # El modelo devolvió <tN>…</tN> para un marcador suelto: su
                # interior se descarta (el contenido válido es el original).
                stack.append(idx)
                containers.append(soup.new_tag("x-discard"))
        elif self_closing:
            # Un marcador con contenido no puede volver vacío: perdería texto.
            return None
        else:
            new_tag = soup.new_tag(orig.name)
            new_tag.attrs = dict(orig.attrs)
            containers[-1].append(new_tag)
            stack.append(idx)
            containers.append(new_tag)

    tail = text[pos:]
    if tail:
        containers[-1].append(NavigableString(tail))
    if stack or seen != set(tag_map):
        return None
    return root


def _apply_translation(block, nodes: list) -> None:
    block.clear()
    for node in nodes:
        block.append(node)


def _strip_markers(text: str) -> str:
    return _MARKER_RE.sub("", text)


# ── Traducción de fragmentos ──────────────────────────────────────────────────

def _translate_fragments(
    client, fragments: list[str], show_progress: bool = True
) -> list[str | None]:
    """
    Traduce una lista de fragmentos (texto con marcadores) por chunks con el
    protocolo numerado [[N]]. Devuelve una lista paralela: traducción o None
    si el modelo omitió ese número. En modo mock devuelve los fragmentos tal
    cual (ejercita el round-trip completo sin API).
    """
    if mock_translate_enabled():
        return list(fragments)

    results: list[str | None] = [None] * len(fragments)
    chunks = split_into_chunks(fragments, MAX_CHUNK_CHARS)
    iterator = tqdm(chunks, desc="    chunks", unit="chunk", leave=False) if show_progress else chunks
    prev_context = ""
    base = 0
    for chunk in iterator:
        ctx_block = PREV_CONTEXT.format(prev=prev_context.strip()) if prev_context.strip() else ""
        prompt = EPUB_TRANSLATE.format(
            context=ctx_block,
            fragmentos="\n".join(f"[[{i}]] {frag}" for i, frag in enumerate(chunk)),
        )
        resp = send_chunk(client, prompt)
        by_idx = parse_numbered_response(resp, len(chunk))
        for i in range(len(chunk)):
            if by_idx.get(i):
                results[base + i] = by_idx[i]
        joined = " ".join(_strip_markers(by_idx.get(i) or chunk[i]) for i in range(len(chunk)))
        prev_context = (prev_context + " " + joined)[-PREV_CTX_CHARS:]
        base += len(chunk)
        time.sleep(REQUEST_DELAY)
    return results


def translate_xhtml_file(client, content: str) -> tuple[str, int]:
    """
    Traduce un archivo XHTML enviando a Gemini solo el texto (con marcadores).
    Devuelve (contenido traducido, nº de bloques que conservan el original
    por fallo persistente del modelo).
    """
    is_xml = content.lstrip().startswith("<?xml") or 'xmlns' in content[:300]
    parser = "xml" if is_xml else "html.parser"
    try:
        soup = BeautifulSoup(content, parser)
    except Exception:
        soup = BeautifulSoup(content, "html.parser")

    blocks = outermost_blocks(soup)
    if not blocks:
        return content, 0

    serialized = [serialize_block(b) for b in blocks]
    results = _translate_fragments(client, [text for text, _ in serialized])

    pending = []  # (bloque, texto serializado original, tag_map)
    for block, (text, tag_map), translated in zip(blocks, serialized, results):
        nodes = deserialize_block(translated, tag_map, soup) if translated else None
        if nodes is None:
            pending.append((block, text, tag_map))
        else:
            _apply_translation(block, nodes)

    # Pasada de rescate: reintenta una vez los fragmentos omitidos o con
    # marcadores rotos. Lo que siga fallando conserva el original (nunca se
    # pierde contenido ni se rompe el XHTML) y se cuenta para el log.
    failed = 0
    if pending:
        retry = _translate_fragments(
            client, [text for _, text, _ in pending], show_progress=False
        )
        for (block, _, tag_map), translated in zip(pending, retry):
            nodes = deserialize_block(translated, tag_map, soup) if translated else None
            if nodes is None:
                failed += 1
            else:
                _apply_translation(block, nodes)
    return str(soup), failed


# ── Índice de navegación (toc.ncx, EPUB 2) ────────────────────────────────────

def _norm_text(s: str) -> str:
    return _WS_RE.sub(" ", s).strip().casefold()


def _corresponding_element(el, orig_soup, trans_soup):
    """
    Localiza en el documento traducido el elemento equivalente a `el` del
    original: por id si lo tiene, o por posición entre los tags de su mismo
    nombre (la traducción no altera la estructura, solo el texto).
    """
    el_id = el.get("id") if isinstance(el, Tag) else None
    if el_id:
        return trans_soup.find(id=el_id)
    same = orig_soup.find_all(el.name)
    try:
        idx = same.index(el)
    except ValueError:
        return None
    candidates = trans_soup.find_all(el.name)
    return candidates[idx] if idx < len(candidates) else None


def _matched_label(label: str, frag: str, orig_soup, trans_soup) -> str | None:
    """
    Si la etiqueta del NCX coincide con el destino o un encabezado del capítulo
    en el ORIGINAL, devuelve el texto YA TRADUCIDO de ese mismo elemento: así
    índice y cuerpo quedan idénticos, sin coste de API.
    """
    candidates = []
    if frag:
        target = orig_soup.find(id=frag)
        if target:
            candidates.append(target)
            candidates.extend(target.find_all(HEADING_TAGS, limit=3))
    candidates.extend(orig_soup.find_all(HEADING_TAGS, limit=8))
    if orig_soup.title:
        candidates.append(orig_soup.title)

    wanted = _norm_text(label)
    for cand in candidates:
        if cand and _norm_text(cand.get_text()) == wanted:
            trans_el = _corresponding_element(cand, orig_soup, trans_soup)
            if trans_el:
                return _WS_RE.sub(" ", trans_el.get_text()).strip()
    return None


def translate_ncx(client, tmp_es: Path, tmp_eng: Path) -> None:
    """
    Traduce las etiquetas visibles del índice EPUB 2 (toc.ncx) manteniéndolo
    navegable: solo se reescriben los <text> de <navLabel>; src, id, playOrder
    y docTitle (título del libro, se conserva en el idioma original) no se
    tocan, así que la navegación no puede romperse. Ante cualquier error el
    NCX se deja intacto (nunca se empeora el libro).
    """
    for ncx_path in tmp_es.rglob("*.ncx"):
        try:
            _translate_ncx_file(client, ncx_path, tmp_es, tmp_eng)
        except (CuotaAgotadaError, APIKeyError, KeyboardInterrupt):
            raise
        except Exception as e:
            print(f"    ⚠ Índice {ncx_path.name} sin traducir ({e}); se conserva el original.")


def _translate_ncx_file(client, ncx_path: Path, tmp_es: Path, tmp_eng: Path) -> None:
    soup = BeautifulSoup(ncx_path.read_text(encoding="utf-8"), "xml")

    entries = []
    for nav_point in soup.find_all("navPoint"):
        label = nav_point.find("navLabel")
        content = nav_point.find("content")
        text_el = label.find("text") if label else None
        src = content.get("src", "") if content else ""
        if text_el is not None and text_el.get_text(strip=True):
            entries.append((text_el, src))
    if not entries:
        return

    # Cache de documentos destino: (soup original, soup traducido) por archivo.
    doc_cache: dict = {}

    def load_docs(rel: Path):
        if rel not in doc_cache:
            orig_p, trans_p = tmp_eng / rel, tmp_es / rel
            if orig_p.exists() and trans_p.exists():
                doc_cache[rel] = (
                    BeautifulSoup(orig_p.read_text(encoding="utf-8", errors="ignore"), "html.parser"),
                    BeautifulSoup(trans_p.read_text(encoding="utf-8", errors="ignore"), "html.parser"),
                )
            else:
                doc_cache[rel] = None
        return doc_cache[rel]

    # Primero, reutilizar el título ya traducido del capítulo destino.
    unmatched = []
    for text_el, src in entries:
        new_label = None
        file_part, _, frag = src.partition("#")
        if file_part:
            try:
                rel = (ncx_path.parent / file_part).resolve().relative_to(tmp_es.resolve())
                docs = load_docs(rel)
            except Exception:
                docs = None
            if docs:
                new_label = _matched_label(text_el.get_text(), frag, docs[0], docs[1])
        if new_label:
            text_el.string = new_label
        else:
            unmatched.append(text_el)

    # Las etiquetas sin correspondencia en el cuerpo se traducen en una única
    # petición numerada; si falta alguna en la respuesta conserva su original.
    if unmatched:
        labels = [_WS_RE.sub(" ", el.get_text()).strip() for el in unmatched]
        translations = _translate_fragments(client, labels, show_progress=False)
        for el, translated in zip(unmatched, translations):
            if translated:
                cleaned = _strip_markers(translated).strip()
                if cleaned:
                    el.string = cleaned

    ncx_path.write_text(str(soup), encoding="utf-8")


# ── Pipeline EPUB ─────────────────────────────────────────────────────────────

def find_content_files(epub_dir: Path) -> list[Path]:
    opf_files = list(epub_dir.rglob("*.opf"))
    if opf_files:
        opf_path = opf_files[0]
        try:
            tree = ET.parse(opf_path)
            root = tree.getroot()
            ns = {"opf": "http://www.idpf.org/2007/opf"}
            base = opf_path.parent
            items = []
            for item in root.findall(".//opf:manifest/opf:item", ns):
                media_type = item.get("media-type", "")
                href = item.get("href", "")
                if "html" in media_type.lower() and href:
                    candidate = (base / href).resolve()
                    if candidate.exists():
                        items.append(candidate)
            if items:
                return items
        except Exception:
            pass
    return [p for p in epub_dir.rglob("*") if p.suffix.lower() in (".xhtml", ".html", ".htm")]


def repack_epub(source_dir: Path, output_path: Path) -> None:
    if output_path.exists():
        output_path.unlink()
    with zipfile.ZipFile(output_path, "w") as zf:
        mimetype_path = source_dir / "mimetype"
        if mimetype_path.exists():
            zf.write(mimetype_path, "mimetype", compress_type=zipfile.ZIP_STORED)
        for path in source_dir.rglob("*"):
            if path.is_dir():
                continue
            if path.name == "mimetype" and path.parent == source_dir:
                continue
            arcname = path.relative_to(source_dir).as_posix()
            zf.write(path, arcname, compress_type=zipfile.ZIP_DEFLATED)


def checkpoint_path(input_path: Path) -> Path:
    return CHECKPOINTS_DIR / f"{input_path.stem}.progress"


def load_progress(input_path: Path) -> dict | None:
    cp = checkpoint_path(input_path)
    if not cp.exists():
        return None
    try:
        return json.loads(cp.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_progress(input_path: Path, data: dict) -> None:
    cp = checkpoint_path(input_path)
    cp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def clear_progress(input_path: Path) -> None:
    cp = checkpoint_path(input_path)
    if cp.exists():
        cp.unlink()


def translate_epub(input_path_str: str, api_key: str | None = None, progress_cb=None) -> Path:
    def _progress(pct: int, msg: str) -> None:
        if progress_cb:
            progress_cb(pct, msg)

    input_path = Path(input_path_str)
    if not input_path.exists():
        raise TraductorError(f"El archivo no existe: {input_path}")
    if input_path.suffix.lower() != ".epub":
        raise TraductorError(f"No es un EPUB: {input_path}")

    client = setup_gemini(api_key)

    stem = input_path.stem
    # Resolvemos a absoluto para que coincida con las rutas que devuelve
    # find_content_files (hace .resolve() internamente) y .relative_to() funcione.
    uploads_abs = UPLOADS_DIR.resolve()
    tmp_eng = uploads_abs / f"_tmp_{stem}_eng"
    tmp_es = uploads_abs / f"_tmp_{stem}_es"
    output_path = uploads_abs / f"{stem}_es.epub"

    progress = load_progress(input_path)
    if progress and tmp_es.exists():
        done = set(progress.get("done", []))
        _progress(2, f"Reanudando ({len(done)} archivos ya completados)")
    else:
        if tmp_eng.exists():
            shutil.rmtree(tmp_eng)
        if tmp_es.exists():
            shutil.rmtree(tmp_es)
        _progress(1, "Extrayendo EPUB...")
        tmp_eng.mkdir(parents=True)
        with zipfile.ZipFile(input_path, "r") as zf:
            zf.extractall(tmp_eng)
        shutil.copytree(tmp_eng, tmp_es)
        done = set()

    content_files = find_content_files(tmp_es)
    rel_files = [p.relative_to(tmp_es).as_posix() for p in content_files]

    total = len(content_files)
    _progress(3, f"{total} archivos de contenido encontrados")

    total_failed = 0
    with tqdm(content_files, desc="archivos", unit="file") as pbar:
        for idx, path in enumerate(pbar):
            rel = path.relative_to(tmp_es).as_posix()
            pct = 3 + int((idx / max(total, 1)) * 94)
            if rel in done:
                _progress(pct, f"Saltado {path.name} ({idx+1}/{total})")
                continue
            _progress(pct, f"Traduciendo {path.name} ({idx+1}/{total})")
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = path.read_text(encoding="utf-8", errors="ignore")
            try:
                translated, failed = translate_xhtml_file(client, content)
                path.write_text(translated, encoding="utf-8")
                if failed:
                    total_failed += failed
                    pbar.write(f"    ⚠ {failed} bloque(s) de {path.name} conservan el original")
                done.add(rel)
                save_progress(input_path, {"done": sorted(done), "files": rel_files})
            except CuotaAgotadaError:
                save_progress(input_path, {"done": sorted(done), "files": rel_files})
                raise

    _progress(97, "Traduciendo índice de navegación...")
    translate_ncx(client, tmp_es, tmp_eng)

    _progress(98, "Empaquetando EPUB traducido...")
    repack_epub(tmp_es, output_path)
    clear_progress(input_path)
    if tmp_eng.exists():
        shutil.rmtree(tmp_eng, ignore_errors=True)
    if tmp_es.exists():
        shutil.rmtree(tmp_es, ignore_errors=True)

    if total_failed:
        _progress(100, f"¡Traducción completada! ({total_failed} bloques conservan el texto original)")
    else:
        _progress(100, "¡Traducción completada!")
    return output_path
