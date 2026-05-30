"""
Traductor de EPUB inglés → español (castellano) con Google Gemini.
"""

import json
import zipfile
import shutil
import time
from pathlib import Path
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup
from tqdm import tqdm

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
from .prompts import EPUB_TRANSLATE, PREV_CONTEXT


BLOCK_TAGS = [
    "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "td", "th", "blockquote", "figcaption",
    "cite", "dt", "dd", "title"
]
SKIP_TAGS = {"script", "style", "code", "pre", "kbd", "var", "samp"}


def has_translatable_text(element) -> bool:
    if element.name in SKIP_TAGS:
        return False
    for parent in element.parents:
        if hasattr(parent, 'name') and parent.name in SKIP_TAGS:
            return False
    return bool(element.get_text(strip=True))


def translate_html_block(client, html: str, prev_context: str) -> str:
    # Modo mock: devuelve el HTML sin traducir (ver SPANIFY_MOCK_TRANSLATE).
    if mock_translate_enabled():
        return html
    ctx_block = PREV_CONTEXT.format(prev=prev_context.strip()) if prev_context.strip() else ""
    prompt = EPUB_TRANSLATE.format(context=ctx_block, html=html)
    return send_chunk(client, prompt)


def translate_xhtml_file(client, content: str) -> str:
    is_xml = content.lstrip().startswith("<?xml") or 'xmlns' in content[:300]
    parser = "xml" if is_xml else "html.parser"
    try:
        soup = BeautifulSoup(content, parser)
    except Exception:
        soup = BeautifulSoup(content, "html.parser")

    blocks = [b for b in soup.find_all(BLOCK_TAGS) if has_translatable_text(b)]
    if not blocks:
        return content

    chunks: list[list] = []
    current: list = []
    current_len: int = 0

    for block in blocks:
        block_str = str(block)
        if current and current_len + len(block_str) > MAX_CHUNK_CHARS:
            chunks.append(current)
            current = [block]
            current_len = len(block_str)
        else:
            current.append(block)
            current_len += len(block_str)
    if current:
        chunks.append(current)

    prev_context = ""

    with tqdm(chunks, desc="    chunks", unit="chunk", leave=False) as pbar:
        for chunk in pbar:
            combined_html = "\n".join(str(b) for b in chunk)
            translated_html = translate_html_block(client, combined_html, prev_context)
            try:
                t_soup = BeautifulSoup(translated_html, "html.parser")
                t_blocks = t_soup.find_all(BLOCK_TAGS)
                if len(t_blocks) == len(chunk):
                    for original, translated in zip(chunk, t_blocks):
                        original.replace_with(translated)
                else:
                    matched = min(len(t_blocks), len(chunk))
                    for j in range(matched):
                        chunk[j].replace_with(t_blocks[j])
                prev_context = t_soup.get_text(separator=" ", strip=True)[-PREV_CTX_CHARS:]
            except Exception as e:
                pbar.write(f"    ✗ error al aplicar traducción: {e}")
            time.sleep(REQUEST_DELAY)
    return str(soup)


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

    with tqdm(content_files, desc="archivos", unit="file") as pbar:
        for idx, path in enumerate(pbar):
            rel = path.relative_to(tmp_es).as_posix()
            pct = 3 + int((idx / max(total, 1)) * 95)
            if rel in done:
                _progress(pct, f"Saltado {path.name} ({idx+1}/{total})")
                continue
            _progress(pct, f"Traduciendo {path.name} ({idx+1}/{total})")
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = path.read_text(encoding="utf-8", errors="ignore")
            try:
                translated = translate_xhtml_file(client, content)
                path.write_text(translated, encoding="utf-8")
                done.add(rel)
                save_progress(input_path, {"done": sorted(done), "files": rel_files})
            except CuotaAgotadaError:
                save_progress(input_path, {"done": sorted(done), "files": rel_files})
                raise

    _progress(98, "Empaquetando EPUB traducido...")
    repack_epub(tmp_es, output_path)
    clear_progress(input_path)
    if tmp_eng.exists():
        shutil.rmtree(tmp_eng, ignore_errors=True)
    if tmp_es.exists():
        shutil.rmtree(tmp_es, ignore_errors=True)

    _progress(100, "¡Traducción completada!")
    return output_path
