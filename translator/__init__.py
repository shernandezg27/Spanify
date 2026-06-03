"""
Traductor de documentos: EPUB y PDF -> español de España con Google Gemini.
"""

from .common import (
    APIKeyError,
    CuotaAgotadaError,
    TraductorError,
    GEMINI_MODEL,
)
from .epub import translate_epub
from .pdf import translate_pdf

__all__ = [
    "translate_epub",
    "translate_pdf",
    "APIKeyError",
    "CuotaAgotadaError",
    "TraductorError",
    "GEMINI_MODEL",
]
