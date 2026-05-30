"""
Verificación end-to-end de los tres arreglos (fuentes/acentos, enlaces,
alineación implícita) sobre el documento real, usando el código REAL de
translator.pdf con texto español acentuado (sin Gemini).
"""

import sys
import tempfile
from pathlib import Path

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from translator.pdf import _extract_spans, _apply_page_spans  # noqa: E402

OUT = Path(__file__).resolve().parent / "verify_out"
OUT.mkdir(exist_ok=True)

# Frase con acentos y signos españoles para cada span (simula la traducción).
SPANISH = "Visión áéíóú: la Niña ¿cuál? ¡Atención! número"


def main():
    src = Path(sys.argv[1])
    render_pages = [int(x) for x in sys.argv[2].split(",")] if len(sys.argv) > 2 else [2, 5]

    doc = fitz.open(str(src))
    page_count = doc.page_count

    # Enlaces antes (página del índice).
    toc_idx = next((i for i in range(min(page_count, 8)) if doc[i].get_links()), None)
    links_before = len(doc[toc_idx].get_links()) if toc_idx is not None else 0

    for pno in range(page_count):
        page = doc[pno]
        spans = _extract_spans(page)
        entries = [
            (SPANISH, s["bbox"], s["origin"], s["font"], s["size"], s["color"],
             s["flags"], s["avail_width"])
            for s in spans
        ]
        _apply_page_spans(page, entries)

    links_after = len(doc[toc_idx].get_links()) if toc_idx is not None else 0

    for pno in render_pages:
        if pno < page_count:
            doc[pno].get_pixmap(dpi=130).save(str(OUT / f"fix_p{pno}.png"))

    tmp = Path(tempfile.gettempdir()) / "verify_fixes.pdf"
    doc.save(str(tmp), garbage=4, deflate=True)
    doc.close()

    # Reabrir para comprobar la CAPA DE TEXTO completa (no solo un substring).
    doc = fitz.open(str(tmp))
    embebidas = [f[3] for f in doc[2].get_fonts(full=True) if f[1]]
    # Texto de todas las páginas: contar caracteres de control (GIDs crudos = basura).
    full = "".join(doc[p].get_text() for p in range(doc.page_count))
    garbage = sum(1 for c in full if ord(c) < 32 and c not in "\n\r\t ")
    visibles = sum(1 for c in full if c.strip())
    out_size = tmp.stat().st_size
    doc.close()
    tmp.unlink()

    print(f"--- ENLACES (#1) en página {toc_idx} ---")
    print(f"  antes={links_before}  despues={links_after}  "
          f"{'OK' if links_after >= links_before and links_before > 0 else 'REVISAR'}")
    print(f"--- ACENTOS / CAPA DE TEXTO (#4) ---")
    print(f"  'Visión' presente: {'Visión' in full}")
    print(f"  caracteres de control (GIDs crudos / basura): {garbage} de {visibles} visibles "
          f"-> {'LIMPIO' if garbage == 0 else 'BASURA!'}")
    print(f"  fuentes embebidas en salida: {sorted(set(str(e) for e in embebidas))}")
    print(f"--- TAMAÑO ---")
    print(f"  salida: {out_size:,} bytes ({out_size/1_048_576:.2f} MB) vs original "
          f"{src.stat().st_size/1_048_576:.2f} MB")
    print(f"  renders -> {[f'verify_out/fix_p{p}.png' for p in render_pages if p < page_count]}")


if __name__ == "__main__":
    main()
