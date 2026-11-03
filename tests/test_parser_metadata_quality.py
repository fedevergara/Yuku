"""Field extraction regressions found on frozen CvLAC/GrupLAC HTML."""

from bs4 import BeautifulSoup

from yuku.cvlac_related_works import ISBN_RE, normalize_isbn, parse_blockquote
from yuku.gruplac_related_works import parse_product_row


def cvlac_record(section, html):
    blockquote = BeautifulSoup(html, "html.parser").find("blockquote")
    return parse_blockquote("0000000001", section, blockquote)


def group_row(text):
    return BeautifulSoup(
        f"<tr><td class='celdas_1'></td><td class='celdas1'>{text}</td></tr>", "lxml"
    ).find("tr")


# 0001463422: the ISSN prefix 1982 was taken as the year.
EDITORIAL = """<blockquote> ANDRES LEONARDO CABALLERO PIZA,
 "The Land of the Most Beautiful Natural Resources. Miss Universo."
 En: Brasil. 2018. DOBRAS. <i>ISSN:</i> 1982-0313 <i>p.</i>22 - 40 v.11
 <br/><b>Palabras: </b><br/> identidad, </blockquote>"""

# 0001463422: the <br> is the only separator between country and journal.
ARTICLE = """<blockquote> ANDRES LEONARDO CABALLERO PIZA, JULIANA SILVA DIAZ,
 "Artistas Plásticos Según Su Público." . En: Colombia <br/>
 CALLE 14 REVISTA DE INVESTIGACION EN EL CAMPO DEL ARTE <i>ISSN:</i> 2011-3757
 <i>ed:</i> <br/> <i>v.</i>8 <i>fasc.</i>N/A p.95 - 109 ,2014, <i> DOI: </i>
</blockquote>"""

# 0000003778: inner quotes belong to the title.
INNER_QUOTES = """<blockquote> HERNANDO DUQUE ORREGO, AIDA ESTHER PENUELA MARTINEZ,
 "IMPACTO ECONÓMICO DEL PROYECTO CAFÉ ESPECIAL "LA VEREDA", EN EL MUNICIPIO DE RIOSUCIO, CALDAS"
 . En: Colombia <br/> Revista Del Centro Nacional De Investigaciones De Café - Cenicafé
 <i>ISSN:</i> 0120-0275 <i>ed:</i> CENICAFE<br/> <i>v.</i>58 <i>fasc.</i>3 p.196 - 215 ,2007,
 <i> DOI: </i> </blockquote>"""


def test_year_is_never_read_from_an_issn():
    assert cvlac_record("Publicaciones editoriales no especializadas", EDITORIAL)["year"] == 2018


def test_country_stops_at_line_break_and_journal_keeps_its_name():
    record = cvlac_record("Articulos", ARTICLE)
    assert record["country"] == "Colombia"
    assert record["publication_place"] == "Colombia"
    assert record["journal_name"] == "CALLE 14 REVISTA DE INVESTIGACION EN EL CAMPO DEL ARTE"
    assert record["year"] == 2014


def test_title_keeps_inner_quotes():
    record = cvlac_record("Articulos", INNER_QUOTES)
    assert record["title"] == ('IMPACTO ECONÓMICO DEL PROYECTO CAFÉ ESPECIAL "LA VEREDA", '
                               "EN EL MUNICIPIO DE RIOSUCIO, CALDAS")
    assert record["year"] == 2007


def test_isbn10_with_single_digit_group():
    assert ISBN_RE.findall("ISBN: 0-415-99044-0 ed: Routledge") == ["0-415-99044-0"]
    assert normalize_isbn("0-415-99044-0") == "0-415-99044-0"
    assert ISBN_RE.findall("ISBN: 978-980-244-537-8") == ["978-980-244-537-8"]


def test_gruplac_year_is_never_read_from_an_issn():
    # COL0110679 production row 7.
    record = parse_product_row(group_row(
        "7.- <strong>Publicado en revista especializada:</strong> Association of major "
        "depressive disorder<br/> Colombia, NUTRICION CLINICA Y DIETETICA HOSPITALARIA "
        "ISSN: 1989-208X, 2024 vol:44 fasc: 1 p&aacute;gs: 303 - 109, "
        "<strong>DOI:</strong>10.12873/441castano <br> Autores: MARIA NATALIA GARCIA RESTREPO"
    ), "Artículos publicados")
    assert record["year"] == 2024
