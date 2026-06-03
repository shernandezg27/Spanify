"""Configuración común de los tests: path al paquete y fixtures de PDF."""

import sys
from pathlib import Path

import pymupdf as fitz
import pytest

# Permite `import translator...` ejecutando pytest desde la raíz del repo.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _make_pdf_with_image_and_text(path: Path) -> None:
    """
    Genera un PDF de 1 página que reproduce el escenario problemático:
    una imagen de fondo a página completa con texto encima. Es justo el caso
    que el pipeline antiguo destrozaba (imagen rasterizada + texto perdido).
    """
    doc = fitz.open()
    page = doc.new_page(width=300, height=200)

    # Imagen de fondo (PNG en memoria, 4x4 azul) escalada a toda la página.
    img = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 4, 4), False)
    img.set_rect(img.irect, (30, 60, 200))
    page.insert_image(fitz.Rect(0, 0, 300, 200), pixmap=img)

    # Texto encima de la imagen.
    page.insert_text(fitz.Point(20, 40), "Hello world", fontsize=12, fontname="helv")
    page.insert_text(fitz.Point(20, 80), "Second line here", fontsize=10, fontname="hebo")

    doc.save(str(path))
    doc.close()


@pytest.fixture
def pdf_with_image(tmp_path) -> Path:
    p = tmp_path / "sample.pdf"
    _make_pdf_with_image_and_text(p)
    return p
