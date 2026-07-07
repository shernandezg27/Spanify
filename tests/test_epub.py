"""
Tests de translator.epub: serialización texto↔marcadores, selección de bloques,
round-trip en modo mock y traducción del índice NCX.
"""

import pytest
from bs4 import BeautifulSoup

from translator.epub import (
    serialize_block,
    deserialize_block,
    outermost_blocks,
    translate_xhtml_file,
    translate_ncx,
    _apply_translation,
)


def _block(html: str, name: str = "p"):
    soup = BeautifulSoup(html, "html.parser")
    return soup.find(name), soup


def _roundtrip(html: str, name: str = "p") -> str:
    """Serializa un bloque, 'traduce' con identidad y lo reconstruye."""
    block, soup = _block(html, name)
    text, tag_map = serialize_block(block)
    nodes = deserialize_block(text, tag_map, soup)
    assert nodes is not None
    _apply_translation(block, nodes)
    return str(block)


class TestSerializeBlock:
    def test_parrafo_plano(self):
        block, _ = _block("<p>Hello world</p>")
        text, tag_map = serialize_block(block)
        assert text == "Hello world"
        assert tag_map == {}

    def test_inline_como_marcador(self):
        block, _ = _block('<p>He said <em>hi</em> to <a href="x.html">Mary</a>.</p>')
        text, tag_map = serialize_block(block)
        assert text == "He said <t1>hi</t1> to <t2>Mary</t2>."
        assert tag_map[1].name == "em"
        assert tag_map[2].name == "a"
        assert tag_map[2]["href"] == "x.html"

    def test_colapsa_whitespace(self):
        block, _ = _block("<p>Hello\n     brave\n\t world</p>")
        text, _ = serialize_block(block)
        assert text == "Hello brave world"

    def test_br_e_img_son_opacos(self):
        block, _ = _block('<p>line one<br/>line <img src="i.png"/> two</p>')
        text, tag_map = serialize_block(block)
        assert text == "line one<t1/>line <t2/> two"
        assert tag_map[1].name == "br"
        assert tag_map[2].name == "img"

    def test_code_es_opaco(self):
        # El interior de SKIP_TAGS no viaja al modelo: no debe traducirse.
        block, _ = _block("<p>Run <code>rm -rf /</code> now</p>")
        text, tag_map = serialize_block(block)
        assert text == "Run <t1/> now"
        assert tag_map[1].name == "code"


class TestRoundTrip:
    def test_plano(self):
        assert _roundtrip("<p>Hello world</p>") == "<p>Hello world</p>"

    def test_inline_anidados_conservan_atributos(self):
        html = '<p class="x">A <em id="e"><strong>very</strong> bold</em> move</p>'
        out = _roundtrip(html)
        soup = BeautifulSoup(out, "html.parser")
        em = soup.find("em")
        assert em["id"] == "e"
        assert em.find("strong").get_text() == "very"
        assert soup.p["class"] == ["x"]
        assert soup.get_text() == "A very bold move"

    def test_href_intacto(self):
        out = _roundtrip('<p>See <a href="ch1.xhtml#s1" class="ref">here</a></p>')
        assert 'href="ch1.xhtml#s1"' in out

    def test_opacos_restaurados_integros(self):
        out = _roundtrip('<p>x<br/>y <code>a &amp; b</code></p>')
        soup = BeautifulSoup(out, "html.parser")
        assert soup.find("br") is not None
        assert soup.find("code").get_text() == "a & b"


class TestDeserializeRechaza:
    def _setup(self):
        block, soup = _block("<p>He said <em>hi</em> to <b>Mary</b>.</p>")
        _, tag_map = serialize_block(block)
        return tag_map, soup

    def test_marcador_perdido(self):
        tag_map, soup = self._setup()
        assert deserialize_block("Dijo <t1>hola</t1> a Mary.", tag_map, soup) is None

    def test_marcador_duplicado(self):
        tag_map, soup = self._setup()
        resp = "Dijo <t1>hola</t1> y <t1>hola</t1> a <t2>Mary</t2>."
        assert deserialize_block(resp, tag_map, soup) is None

    def test_marcador_inventado(self):
        tag_map, soup = self._setup()
        resp = "Dijo <t1>hola</t1> a <t2>Mary</t2> <t9>ya</t9>."
        assert deserialize_block(resp, tag_map, soup) is None

    def test_mal_anidado(self):
        tag_map, soup = self._setup()
        resp = "Dijo <t1>hola a <t2>Mary</t1>.</t2>"
        assert deserialize_block(resp, tag_map, soup) is None

    def test_marcador_con_contenido_no_puede_volver_vacio(self):
        tag_map, soup = self._setup()
        resp = "Dijo <t1/> a <t2>Mary</t2> hola."
        assert deserialize_block(resp, tag_map, soup) is None

    def test_opaco_devuelto_como_par_se_acepta_y_descarta_su_interior(self):
        block, soup = _block("<p>one<br/>two</p>")
        _, tag_map = serialize_block(block)
        nodes = deserialize_block("uno<t1>basura</t1>dos", tag_map, soup)
        assert nodes is not None
        _apply_translation(block, nodes)
        assert str(block) == "<p>uno<br/>dos</p>"

    def test_tolera_espacios_en_marcadores(self):
        block, soup = _block("<p>one<br/>two <em>x</em></p>")
        _, tag_map = serialize_block(block)
        nodes = deserialize_block("uno<t1 />dos < t2 >y</ t2 >", tag_map, soup)
        assert nodes is not None


class TestOutermostBlocks:
    def test_bloque_anidado_no_se_duplica(self):
        soup = BeautifulSoup(
            "<blockquote><p>one</p><p>two</p></blockquote>", "html.parser"
        )
        blocks = outermost_blocks(soup)
        assert [b.name for b in blocks] == ["blockquote"]

    def test_li_con_p(self):
        soup = BeautifulSoup("<ul><li><p>x</p></li><li>y</li></ul>", "html.parser")
        assert [b.name for b in outermost_blocks(soup)] == ["li", "li"]

    def test_skip_tags_fuera(self):
        soup = BeautifulSoup(
            "<div><pre><p>code</p></pre><p>text</p></div>", "html.parser"
        )
        blocks = outermost_blocks(soup)
        assert len(blocks) == 1
        assert blocks[0].get_text() == "text"

    def test_bloques_sin_texto_fuera(self):
        soup = BeautifulSoup('<p><img src="x.png"/></p><p>hi</p>', "html.parser")
        assert len(outermost_blocks(soup)) == 1


XHTML_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter One</title></head>
<body>
  <h1 id="c1">Chapter One</h1>
  <p class="first">It was a <em>dark</em> and stormy night.</p>
  <p>See <a href="notes.xhtml#n1">note</a>.<br/>The end.</p>
</body>
</html>"""


class TestTranslateXhtmlMock:
    @pytest.fixture(autouse=True)
    def _mock(self, monkeypatch):
        monkeypatch.setenv("SPANIFY_MOCK_TRANSLATE", "1")

    def test_roundtrip_conserva_estructura_y_texto(self):
        out, failed = translate_xhtml_file(None, XHTML_SAMPLE)
        assert failed == 0
        soup = BeautifulSoup(out, "xml")
        assert soup.find("h1")["id"] == "c1"
        assert soup.find("p")["class"] == "first" or soup.find("p")["class"] == ["first"]
        assert soup.find("a")["href"] == "notes.xhtml#n1"
        assert soup.find("br") is not None
        assert "dark" in soup.find("em").get_text()
        assert "It was a dark and stormy night." in soup.get_text()

    def test_sin_bloques_devuelve_identico(self):
        content = "<html><body><div>   </div></body></html>"
        out, failed = translate_xhtml_file(None, content)
        assert out == content
        assert failed == 0


NCX_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="uid"/></head>
  <docTitle><text>My Great Book</text></docTitle>
  <navMap>
    <navPoint id="np1" playOrder="1">
      <navLabel><text>Chapter One</text></navLabel>
      <content src="ch1.xhtml#c1"/>
    </navPoint>
    <navPoint id="np2" playOrder="2">
      <navLabel><text>Some Custom Label</text></navLabel>
      <content src="ch1.xhtml"/>
    </navPoint>
  </navMap>
</ncx>"""


class TestTranslateNcx:
    @pytest.fixture(autouse=True)
    def _mock(self, monkeypatch):
        monkeypatch.setenv("SPANIFY_MOCK_TRANSLATE", "1")

    def _make_book(self, tmp_path, translated_heading="Capítulo uno"):
        tmp_eng = tmp_path / "eng"
        tmp_es = tmp_path / "es"
        for d in (tmp_eng, tmp_es):
            d.mkdir()
        chapter = '<html><body><h1 id="c1">{h}</h1><p>text</p></body></html>'
        (tmp_eng / "ch1.xhtml").write_text(chapter.format(h="Chapter One"), encoding="utf-8")
        (tmp_es / "ch1.xhtml").write_text(chapter.format(h=translated_heading), encoding="utf-8")
        (tmp_es / "toc.ncx").write_text(NCX_SAMPLE, encoding="utf-8")
        return tmp_eng, tmp_es

    def test_etiqueta_reutiliza_titulo_traducido(self, tmp_path):
        tmp_eng, tmp_es = self._make_book(tmp_path)
        translate_ncx(None, tmp_es, tmp_eng)
        out = (tmp_es / "toc.ncx").read_text(encoding="utf-8")
        soup = BeautifulSoup(out, "xml")
        labels = [t.get_text() for t in soup.find_all("text")]
        # docTitle intacto, cap. 1 tomado del cuerpo traducido, etiqueta sin
        # correspondencia pasa por el traductor (mock = se queda igual).
        assert labels[0] == "My Great Book"
        assert labels[1] == "Capítulo uno"
        assert labels[2] == "Some Custom Label"

    def test_navegacion_intacta(self, tmp_path):
        tmp_eng, tmp_es = self._make_book(tmp_path)
        translate_ncx(None, tmp_es, tmp_eng)
        soup = BeautifulSoup((tmp_es / "toc.ncx").read_text(encoding="utf-8"), "xml")
        points = soup.find_all("navPoint")
        assert [p["id"] for p in points] == ["np1", "np2"]
        assert [p["playOrder"] for p in points] == ["1", "2"]
        srcs = [p.find("content")["src"] for p in points]
        assert srcs == ["ch1.xhtml#c1", "ch1.xhtml"]

    def test_ncx_malformado_se_deja_intacto(self, tmp_path):
        tmp_eng, tmp_es = self._make_book(tmp_path)
        broken = "<ncx><navMap><navPoint>no cierra"
        (tmp_es / "toc.ncx").write_text(broken, encoding="utf-8")
        translate_ncx(None, tmp_es, tmp_eng)  # no debe lanzar
        # lxml puede "reparar" al parsear; lo esencial: no lanza y el archivo
        # sigue existiendo con contenido.
        assert (tmp_es / "toc.ncx").read_text(encoding="utf-8")
