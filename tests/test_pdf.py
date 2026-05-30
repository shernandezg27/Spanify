"""Tests de translator.pdf y translator.fonts: funciones puras, reconstrucción,
fuentes sustitutas, preservación de enlaces y realineado robusto."""

import pymupdf as fitz
import pytest

from translator import pdf
from translator.pdf import (
    normalize_color,
    _glyph_runs,
    _parse_numbered_response,
    _extract_spans,
    _apply_page_spans,
    _entry_to_saved,
    _entry_from_saved,
    _translate_page_spans,
)
from translator import fonts
from translator.fonts import substitute_font, broad_font


# ── Funciones puras ──────────────────────────────────────────────────────────

class TestNormalizeColor:
    def test_negro(self):
        assert normalize_color(0) == (0.0, 0.0, 0.0)

    def test_rojo(self):
        assert normalize_color(0xFF0000) == (1.0, 0.0, 0.0)

    def test_none_es_negro(self):
        assert normalize_color(None) == (0.0, 0.0, 0.0)

    def test_componentes_en_rango(self):
        r, g, b = normalize_color(0x336699)
        assert (r, g, b) == (0x33 / 255, 0x66 / 255, 0x99 / 255)
        assert all(0.0 <= c <= 1.0 for c in (r, g, b))


# ── Fuentes sustitutas ───────────────────────────────────────────────────────

class TestSubstituteFont:
    def test_regular_es_sans(self):
        assert substitute_font(0).name == "Noto Sans Regular"

    def test_serif(self):
        assert substitute_font(fonts.FLAG_SERIF).name == "Noto Serif Regular"

    def test_serif_negrita(self):
        assert substitute_font(fonts.FLAG_SERIF | fonts.FLAG_BOLD).name == "Noto Serif Bold"

    def test_sans_negrita_cursiva(self):
        f = substitute_font(fonts.FLAG_BOLD | fonts.FLAG_ITALIC)
        assert f.name == "Noto Sans Bold Italic"

    def test_mono_cae_en_sans(self):
        # No bundleamos mono: se aproxima con sans (no debe reventar).
        assert "Noto Sans" in substitute_font(fonts.FLAG_MONO).name

    def test_cubren_acentos_espanoles(self):
        for flags in (0, fonts.FLAG_SERIF, fonts.FLAG_BOLD, fonts.FLAG_ITALIC):
            f = substitute_font(flags)
            assert all(f.has_glyph(ord(c)) for c in "áéíóúñÑ¿¡")


class FakeFont:
    """Fuente de juguete para probar _glyph_runs de forma determinista."""
    def __init__(self, disponibles):
        self.disponibles = {ord(c) for c in disponibles}

    def has_glyph(self, cp):
        return cp in self.disponibles


class TestGlyphRuns:
    def test_todo_con_primary_un_solo_run(self):
        prim = FakeFont("abc")
        fb = FakeFont("")
        runs = _glyph_runs("abc", prim, fb)
        assert [(f is prim, t) for f, t in runs] == [(True, "abc")]

    def test_caracter_que_falta_cae_a_fallback(self):
        prim = FakeFont("abc")
        fb = FakeFont("x")
        runs = _glyph_runs("abxc", prim, fb)
        assert [(f is prim, t) for f, t in runs] == [(True, "ab"), (False, "x"), (True, "c")]


# ── Realineado robusto por número ────────────────────────────────────────────

class TestParseNumberedResponse:
    def test_orden_correcto(self):
        chunk = ["one", "two", "three"]
        resp = "[[0]] uno\n[[1]] dos\n[[2]] tres"
        assert _parse_numbered_response(resp, chunk) == ["uno", "dos", "tres"]

    def test_indice_que_falta_conserva_original(self):
        chunk = ["one", "two", "three"]
        resp = "[[0]] uno\n[[2]] tres"  # falta el 1
        assert _parse_numbered_response(resp, chunk) == ["uno", "two", "tres"]

    def test_indices_fuera_de_rango_se_ignoran(self):
        chunk = ["one", "two"]
        resp = "[[0]] uno\n[[9]] basura"
        assert _parse_numbered_response(resp, chunk) == ["uno", "two"]

    def test_sin_marcadores_todo_original(self):
        # Si el modelo ignora el formato, no se desplaza nada: todo original.
        chunk = ["one", "two"]
        assert _parse_numbered_response("uno dos", chunk) == ["one", "two"]

    def test_marcadores_con_espacios(self):
        chunk = ["one", "two", "three"]
        resp = "[[ 0 ]]  uno\n[[1]]\tdos\n[[2]] tres"
        assert _parse_numbered_response(resp, chunk) == ["uno", "dos", "tres"]


# ── Serialización de checkpoint ───────────────────────────────────────────────

class TestEntrySerialization:
    def test_roundtrip(self):
        entry = ("Hola", (10.0, 20.0, 50.0, 32.0), (10.0, 30.0), "Garamond", 11.0, 0, 0)
        restored = _entry_from_saved(_entry_to_saved(entry))
        assert restored == entry

    def test_formato_antiguo_sin_origin_se_deriva_del_bbox(self):
        old = ["Hola", [10.0, 20.0, 50.0, 32.0], "Garamond", 11.0, 0, 0]
        restored = _entry_from_saved(old)
        assert restored[2] == (10.0, 32.0)  # origin derivado
        assert restored[0] == "Hola"


# ── Extracción ────────────────────────────────────────────────────────────────

class TestExtractSpans:
    def test_extrae_texto_con_origin(self, pdf_with_image):
        doc = fitz.open(str(pdf_with_image))
        spans = _extract_spans(doc[0])
        doc.close()
        textos = " ".join(s["text"] for s in spans)
        assert "Hello world" in textos
        for s in spans:
            assert "origin" in s and len(s["origin"]) == 2
            assert len(s["bbox"]) == 4

    def test_ignora_spans_vacios(self, tmp_path):
        p = tmp_path / "spaces.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text(fitz.Point(20, 40), "real", fontsize=12)
        doc.save(str(p))
        doc.close()
        doc = fitz.open(str(p))
        spans = _extract_spans(doc[0])
        doc.close()
        assert all(s["text"].strip() for s in spans)


# ── Traducción: realineado y mock ────────────────────────────────────────────

class TestTranslatePageSpans:
    def _spans(self, n):
        return [
            {"text": f"word{i}", "bbox": (0, 0, 10, 10), "origin": (0, 10),
             "font": "Garamond", "size": 11.0, "color": 0, "flags": 0}
            for i in range(n)
        ]

    def test_respuesta_numerada_correcta(self, monkeypatch):
        monkeypatch.setattr(pdf, "REQUEST_DELAY", 0)
        monkeypatch.setattr(pdf, "send_chunk",
                            lambda c, p: "[[0]] uno\n[[1]] dos\n[[2]] tres")
        out, _ = _translate_page_spans(None, self._spans(3), "")
        assert out == ["uno", "dos", "tres"]

    def test_modelo_se_salta_uno_no_desplaza(self, monkeypatch):
        monkeypatch.setattr(pdf, "REQUEST_DELAY", 0)
        monkeypatch.setattr(pdf, "send_chunk", lambda c, p: "[[0]] uno\n[[2]] tres")
        out, _ = _translate_page_spans(None, self._spans(3), "")
        assert out == ["uno", "word1", "tres"]  # el hueco mantiene el original, sin desplazar

    def test_sin_spans_devuelve_vacio(self):
        out, ctx = _translate_page_spans(None, [], "contexto")
        assert out == []
        assert ctx == "contexto"

    def test_modo_mock_devuelve_identidad_sin_llamar_api(self, monkeypatch):
        monkeypatch.setenv("SPANIFY_MOCK_TRANSLATE", "1")

        def boom(*a, **k):
            raise AssertionError("no debe llamar a la API en modo mock")

        monkeypatch.setattr(pdf, "send_chunk", boom)
        out, _ = _translate_page_spans(None, self._spans(3), "")
        assert out == ["word0", "word1", "word2"]


# ── Integración: reconstrucción, acentos, enlaces ────────────────────────────

class TestApplyPageSpans:
    def _entries_from(self, page, translate):
        spans = _extract_spans(page)
        return [
            (translate(s["text"]), s["bbox"], s["origin"],
             s["font"], s["size"], s["color"], s["flags"])
            for s in spans
        ]

    def test_texto_traducido_reemplaza_al_original(self, pdf_with_image):
        doc = fitz.open(str(pdf_with_image))
        page = doc[0]
        entries = self._entries_from(page, lambda t: t.replace("Hello world", "Hola mundo"))
        _apply_page_spans(page, entries)
        texto = page.get_text()
        doc.close()
        assert "Hola mundo" in texto
        assert "Hello world" not in texto  # el original fue redactado

    def test_acentos_correctos_y_capa_de_texto_limpia(self, pdf_with_image, tmp_path):
        # Regresión: reutilizar la fuente original producía GIDs crudos (basura).
        # Con fuente sustituta embebida el texto debe salir correcto y copiable.
        doc = fitz.open(str(pdf_with_image))
        page = doc[0]
        entries = self._entries_from(page, lambda t: "Visión áéíóú ñ ¿qué?")
        _apply_page_spans(page, entries)
        out = tmp_path / "acentos.pdf"
        doc.save(str(out), garbage=4, deflate=True)
        doc.close()

        doc = fitz.open(str(out))
        txt = doc[0].get_text()
        doc.close()
        assert "Visión áéíóú ñ ¿qué?" in txt
        # Sin caracteres de control: si hubiera GIDs crudos, aparecerían aquí.
        assert not any(ord(c) < 32 and c not in "\n\r\t " for c in txt)

    def test_fuente_insertada_va_embebida(self, pdf_with_image, tmp_path):
        # Guarda contra el bug original: base-14 NO embebidas que el visor corrompe.
        doc = fitz.open(str(pdf_with_image))
        page = doc[0]
        _apply_page_spans(page, self._entries_from(page, lambda t: "Atención"))
        out = tmp_path / "emb.pdf"
        doc.save(str(out), garbage=4, deflate=True)
        doc.close()
        doc = fitz.open(str(out))
        noto = [f for f in doc[0].get_fonts(full=True) if "Noto" in str(f[3])]
        doc.close()
        assert noto, "debe insertarse una fuente Noto"
        assert all(f[1] for f in noto), "la fuente Noto debe ir embebida (ext no vacío)"

    def test_enlaces_se_conservan(self, tmp_path):
        # Los enlaces solo son visibles para get_links() una vez persistidos,
        # así que creamos el PDF con el enlace, lo guardamos y reabrimos (como
        # llega un PDF real con su índice enlazado).
        src = tmp_path / "con_enlace.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text(fitz.Point(20, 40), "Go to page", fontsize=12)
        page.insert_link({
            "kind": fitz.LINK_GOTO, "from": fitz.Rect(20, 30, 120, 45),
            "page": 0, "to": fitz.Point(0, 0),
        })
        doc.save(str(src))
        doc.close()

        doc = fitz.open(str(src))
        page = doc[0]
        assert len(page.get_links()) == 1  # de partida hay 1 enlace
        entries = [
            ("Ir a la página", s["bbox"], s["origin"], s["font"], s["size"], s["color"], s["flags"])
            for s in _extract_spans(page)
        ]
        _apply_page_spans(page, entries)
        # get_links() no refleja enlaces reinsertados hasta guardar; comprobamos
        # sobre el archivo guardado/reabierto, que es lo que recibe el usuario.
        out = tmp_path / "con_enlace_out.pdf"
        doc.save(str(out), garbage=4, deflate=True)
        doc.close()
        doc = fitz.open(str(out))
        links = doc[0].get_links()
        doc.close()
        assert len(links) >= 1  # el enlace sobrevive a la reconstrucción

    def test_imagen_se_conserva(self, pdf_with_image):
        doc = fitz.open(str(pdf_with_image))
        page = doc[0]
        imgs_antes = len(page.get_images(full=True))
        _apply_page_spans(page, self._entries_from(page, lambda t: t))
        imgs_despues = len(page.get_images(full=True))
        doc.close()
        assert imgs_antes >= 1
        assert imgs_despues >= imgs_antes  # la imagen NO desaparece

    def test_no_explota_el_tamano(self, pdf_with_image, tmp_path):
        size_antes = pdf_with_image.stat().st_size
        doc = fitz.open(str(pdf_with_image))
        page = doc[0]
        entries = self._entries_from(page, lambda t: t + " traducido más largo")
        _apply_page_spans(page, entries)
        out = tmp_path / "out.pdf"
        doc.save(str(out), garbage=4, deflate=True)
        doc.close()
        # Embeber una fuente Noto añade un coste fijo (~decenas de KB). Lo que
        # NO debe pasar es la explosión del bug original (imágenes rasterizadas,
        # x50). Margen amplio para el coste de fuentes, pero lejos de los MB.
        assert out.stat().st_size < size_antes + 300_000
