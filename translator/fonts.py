"""
Resolución de fuentes para reinsertar texto traducido en el PDF.

Se usa SIEMPRE una fuente sustituta Unicode embebida, elegida por el ESTILO del
span (serif/sans + negrita/cursiva) a partir de sus flags. No hay nada
específico de Garamond.

Por qué no se reutiliza la fuente original embebida del PDF: sus subsets suelen
ser Type0/Identity-H sin un mapa Unicode estándar; al escribir texto nuevo con
ellos, PyMuPDF produce un PDF cuyo texto sale desplazado/ilegible en visores
reales y como IDs de glifo crudos al copiar. Las sustitutas de
translator/fonts/*.ttf (subsets latinos de Noto, licencia OFL) sí se embeben
con codificación correcta (Identity-H + ToUnicode), así que el resultado es
correcto y portable en cualquier visor.

Flags de span de PyMuPDF (bitfield):
  bit 1 (2)  = cursiva
  bit 2 (4)  = serif
  bit 3 (8)  = monoespaciada
  bit 4 (16) = negrita
"""

import functools
from pathlib import Path

import pymupdf as fitz

_FONTS_DIR = Path(__file__).resolve().parent / "fonts"

FLAG_ITALIC = 2
FLAG_SERIF = 4
FLAG_MONO = 8
FLAG_BOLD = 16


def _style_suffix(bold: bool, italic: bool) -> str:
    if bold and italic:
        return "BoldItalic"
    if bold:
        return "Bold"
    if italic:
        return "Italic"
    return "Regular"


@functools.lru_cache(maxsize=None)
def substitute_font(flags: int) -> fitz.Font:
    """
    Devuelve la fuente sustituta embebida para un span según sus flags.
    Serif -> Noto Serif; resto (incl. monoespaciada) -> Noto Sans.
    Cacheada: hay como mucho 8 combinaciones.
    """
    family = "NotoSerif" if (flags & FLAG_SERIF) else "NotoSans"
    suffix = _style_suffix(bool(flags & FLAG_BOLD), bool(flags & FLAG_ITALIC))
    return fitz.Font(fontfile=str(_FONTS_DIR / f"{family}-{suffix}.ttf"))


def broad_font() -> fitz.Font:
    """Fuente de cobertura amplia (Noto Sans Regular) para glifos que falten en la elegida."""
    return substitute_font(0)
