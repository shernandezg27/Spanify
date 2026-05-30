"""Tests de translator.common: chunking y saneado de respuestas del modelo."""

from translator.common import split_into_chunks, strip_markdown_fences


class TestSplitIntoChunks:
    def test_vacio(self):
        assert split_into_chunks([], 100) == []

    def test_todo_en_un_chunk(self):
        textos = ["aaa", "bbb", "ccc"]
        assert split_into_chunks(textos, 100) == [["aaa", "bbb", "ccc"]]

    def test_parte_por_limite(self):
        # 'aaaa'(4) + 'bbbb'(4) = 8 > 6 -> el segundo abre chunk nuevo.
        assert split_into_chunks(["aaaa", "bbbb"], 6) == [["aaaa"], ["bbbb"]]

    def test_string_mas_largo_que_el_limite_va_solo(self):
        # Un string suelto más largo que max_chars no se descarta: va en su chunk.
        out = split_into_chunks(["x" * 50], 10)
        assert out == [["x" * 50]]

    def test_no_pierde_ningun_fragmento(self):
        textos = [f"frag{i}" for i in range(20)]
        out = split_into_chunks(textos, 13)  # ~2-3 frags por chunk
        plano = [t for chunk in out for t in chunk]
        assert plano == textos  # mismo contenido y mismo orden

    def test_limite_exacto_no_rompe(self):
        # 'aaa'(3)+'bbb'(3)=6 == max -> caben juntos (la condición es estricta >).
        assert split_into_chunks(["aaa", "bbb"], 6) == [["aaa", "bbb"]]


class TestStripMarkdownFences:
    def test_sin_fences(self):
        assert strip_markdown_fences("hola") == "hola"

    def test_quita_fence_simple(self):
        assert strip_markdown_fences("```\nhola\n```") == "hola"

    def test_quita_fence_con_lenguaje(self):
        assert strip_markdown_fences("```html\n<p>hi</p>\n```") == "<p>hi</p>"

    def test_conserva_pipes_internos(self):
        # No debe tocar el separador ||| que usa el traductor de PDF.
        assert strip_markdown_fences("a ||| b ||| c") == "a ||| b ||| c"
