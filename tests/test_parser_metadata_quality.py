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


def test_editorial_volume_and_medium_name():
    record = cvlac_record("Publicaciones editoriales no especializadas", EDITORIAL)
    assert record["volume"] == "11"
    assert record["journal_name"] == "DOBRAS"


def test_empty_volume_labels_are_not_volumes():
    # COL0095868: "vol:N/A" was published as volume "N".
    record = parse_product_row(group_row(
        "40.- <strong>Revisión (Survey):</strong> Nación, memoria e identidad<br/> "
        "Colombia, Conjeuras ISSN: 1657-5830, 2011 vol:N/A fasc: N/A p&aacute;gs: 4 - 13, "
        "<strong>DOI:</strong> <br> Autores: JORGE ORLANDO BLANCO SUAREZ"
    ), "Artículos publicados")
    assert record["volume"] == "" and record["issue"] == "" and record["pages"] == "4 - 13"
    from yuku.cvlac_related_works import extract_volume
    assert extract_volume("ISBN: 958-8018-44-7 v. pags. Areas: Ciencias") == ""


def test_issue_label_is_a_whole_word_outside_the_title():
    from yuku.scienti_journals import journal_context
    values = journal_context(
        '"Número 1!" . En: Colombia CURRENT ISSUES IN TEACHERS PROFESSIONAL DEVELOPMENT '
        "ISSN: 1234-5678 v.2 fasc.3 p.1 - 9 ,2015,", "Número 1!", country="Colombia")
    assert values["issue"] == "3"
    assert values["journal_name"] == "CURRENT ISSUES IN TEACHERS PROFESSIONAL DEVELOPMENT"


def test_alphanumeric_pages():
    # COL0134634 and newspaper sections in 0001424608.
    record = parse_product_row(group_row(
        "100.- <strong>Corto (Resumen):</strong> PIN66 - Potential Impact<br/> Colombia, "
        "VALUE IN HEALTH ISSN: 1524-4733, 2013 vol:16 fasc: 3 p&aacute;gs: A91 - A91, "
        "<strong>DOI:</strong> <br> Autores: NELSON RAFAEL ALVIS GUZMAN"
    ), "Artículos publicados")
    assert (record["pages"], record["start_page"], record["volume"]) == ("A91 - A91", "A91", "16")
    from yuku.cvlac_related_works import extract_pages
    assert extract_pages("ISSN: 0124-9320 p. 6A - 6A v.1 Areas: X", {}) == ("6A - 6A", "6A", "6A")


def test_gruplac_manual_publisher():
    # COL0032358: manuals and guides use the book layout "ISBN, Ed. <publisher>".
    record = parse_product_row(group_row(
        "1.- <strong>Manuales y Guías Especializadas</strong> : Urbanismo táctico. Por una "
        "movilidad humana y sostenible: Guía práctica<br/> Colombia, 2018, "
        "ISBN: 978-958-5448-31-5, Ed. Municipio de medellin <br> Autores: FRANCISCO LUIS GIRALDO"
    ), "Manuales y Guías Especializadas")
    assert record["publisher"] == "Municipio de medellin"


def cvlac_section(heading, bold, blockquote):
    from yuku.cvlac_related_works import normalize_related_works_document
    html = (f"<table><tr><td><h3>{heading}</h3></td></tr>"
            f"<tr><td><li><b>{bold}</b></li></td></tr><tr><td>{blockquote}</td></tr></table>")
    return normalize_related_works_document("0000000001", html)["production"]


def test_cvlac_working_papers_are_parsed():
    # 0000535591.
    (record,) = cvlac_section(
        "Documentos de trabajo", "Producción bibliográfica - Documento de trabajo (Working Paper)",
        '<blockquote> MANUEL FRANCO AVELLANEDA, "Evaluación en Maloka: una propuesta para '
        'discusión." En: Colombia. 2012. p. <br/><b>Areas: </b><br/> Ciencias Sociales </blockquote>')
    assert record["type_impactu"] == "Documento de trabajo"
    assert record["source_section"] == "Documentos de trabajo"
    assert (record["title"], record["year"], record["country"]) == (
        "Evaluación en Maloka: una propuesta para discusión", 2012, "Colombia")
    assert record["authors"]


def test_cvlac_translations_and_manuals_are_parsed():
    # 0000342840 and 0000131708.
    (translation,) = cvlac_section(
        "Traducciones", "Producción bibliográfica - Traducciones - Artículo",
        '<blockquote> BEATRIZ ELENA GARCIA ARBOLEDA, ESPERANZA HIDALGO URREA, "TRAUMATISME ET '
        'VIOLENCE: UNE RENCONTRE RÉELLE Á TAMBO (COLOMBIE)" En: Colombia. 2010. . . <i>Idioma '
        'original:</i>. <i>Idioma traducción:</i>Español. <i>Autor: </i>Oscar Muñiz. <i>Nombre '
        'original: </i>. <i>fasc. </i>. v.. <br/><b>Areas: </b><br/> Ciencias Sociales </blockquote>')
    assert (translation["type_impactu"], translation["year"]) == ("Artículo de revista", 2010)
    assert translation["title"].startswith("TRAUMATISME ET VIOLENCE")
    (manual,) = cvlac_section(
        "Manuales y Guías Especializadas",
        "Producción bibliográfica - Libro - Manuales y Guías Especializadas ",
        "<blockquote> <i>Nombre del libro: </i>Las Buenas Practicas Agrícolas, <i>Fecha de "
        "presentación: </i>2018 - Febrero, <i>Isbn: </i>978-958-5471-40-5, <i>Medio de "
        "divulgación: </i>Papel, <i>Lugar de publicación: </i>Colombia, <i>Editorial: </i>"
        "Ediciones Usta Universidad Santo Tomás, <br/><b>Areas: </b><br/> Ciencias Agrícolas "
        "</blockquote>")
    assert manual["type_impactu"] == "Publicaciones editoriales no especializadas"
    assert (manual["title"], manual["year"], manual["isbn"]) == (
        "Las Buenas Practicas Agrícolas", 2018, ["978-958-5471-40-5"])
    assert (manual["publisher"], manual["publication_place"]) == (
        "Ediciones Usta Universidad Santo Tomás", "Colombia")
