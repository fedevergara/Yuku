import unittest

from bs4 import BeautifulSoup

from yuku.cvlac_related_works import clean_publisher_value, is_suspicious_publisher
from yuku.gruplac_related_works import parse_product_row


class GruplacRelatedWorksParsingTest(unittest.TestCase):
    def test_publisher_validation_matches_all_observed_audit_values(self):
        legitimate = [
            "Voluntad",
            "Voluntad Sa",
            "Páginas de agua Editorial",
            "Voluntad Editores Ltda. y Cia. S.C.A. (Bogotá)",
            "Volcán ediciones",
            "pagina seis",
            "Voluntad Editores",
            "V Centenario, Comisión de Murcia. Colección Carabelas",
            "V O Graficas",
        ]
        contaminated = [
            "78-958-8891-35-4",
            "978-958-44-2350-4",
            "978-958-5533-03-5",
            (
                "ISBN 978-958-699-297-8, Medio de divulgación: Papel Idioma "
                "del documento original: Inglés, Idioma de la traducción: Español "
                "Edición: 3, Serie: , Autor del documento original: Jefrrey K. Pinto"
            ),
            (
                "ISBN 978-958-699-297-8, Medio de divulgación: Papel Idioma "
                "del documento original: Inglés, Idioma de la traducción: Español "
                "Edición: 3a, Serie: 1, Autor del documento original: Jeffrey K. Pinto"
            ),
            (
                "ISBN 978-958-699-297-8, Medio de divulgación: Papel Idioma "
                "del documento original: Inglés, Idioma de la traducción: Español "
                "Edición: 3era, Serie: , Autor del documento original: Jeffrey K. Pinto"
            ),
        ]
        for value in legitimate:
            with self.subTest(value=value):
                self.assertFalse(is_suspicious_publisher(value))
                self.assertEqual(clean_publisher_value(value), value)
        for value in contaminated:
            with self.subTest(value=value):
                self.assertTrue(is_suspicious_publisher(value))
                self.assertEqual(clean_publisher_value(value), "")

    def test_rejects_isbn_in_editorial_slot_and_preserves_raw_evidence(self):
        raw = (
            "1.- <strong>Otro libro publicado :</strong> Análisis de Fourier<br/>"
            "Colombia,2007, ISBN: 978-958-44-2350-4 vol: 1 págs: 244, "
            "Ed. 978-958-44-2350-4 Autores: JULIAN PERSONA"
        )
        row = BeautifulSoup(
            f'<tr><td class="celdas_1"></td><td class="celdas1">{raw}</td></tr>',
            "lxml",
        ).find("tr")
        record = parse_product_row(row, "Otros Libros publicados")
        self.assertEqual(record["publisher"], "")
        self.assertEqual(record["isbn"], ["978-958-44-2350-4"])
        self.assertEqual(record["volume"], "1")
        self.assertIn("Ed. 978-958-44-2350-4", record["raw_text"])

    def test_rejects_translation_metadata_leaked_as_publisher(self):
        row = BeautifulSoup(
            """
            <tr><td class="celdas_1"></td><td class="celdas1">
              1.- <strong>Libro :</strong> Project Management<br/>
              2014, Revista: ISSN , Libro: Project Management 3 Th Ed.
              ISBN 978-958-699-297-8, Medio de divulgación: Papel
              Idioma del documento original: Inglés, Edición: 3
              Autores: ANA PERSONA
            </td></tr>
            """,
            "lxml",
        ).find("tr")
        record = parse_product_row(row, "Traducciones")
        self.assertEqual(record["publisher"], "")
        self.assertEqual(record["edition"], "3")
        self.assertEqual(record["publication_place"], "")
        self.assertIn("ISBN 978-958-699-297-8", record["raw_text"])

    def test_empty_volume_does_not_absorb_pages_label(self):
        row = BeautifulSoup(
            """
            <tr><td class="celdas_1"></td><td class="celdas1">
              1.- <strong>Libro pedagógico :</strong> Libro verificable<br/>
              Colombia, 2019, ISBN: 978-958-5533-03-5 vol: págs: ,
              Ed. 978-958-5533-03-5 Autores: ANA PERSONA
            </td></tr>
            """,
            "lxml",
        ).find("tr")
        record = parse_product_row(row, "Otros Libros publicados")
        self.assertEqual(record["volume"], "")
        self.assertEqual(record["publisher"], "")

    def test_volume_label_does_not_match_publisher_and_stops_at_isbn(self):
        publisher_row = BeautifulSoup(
            """
            <tr><td class="celdas_1"></td><td class="celdas1">
              1.- <strong>Libro :</strong> Libro verificable<br/>
              Colombia, 2019, Ed. Voluntad Autores: ANA PERSONA
            </td></tr>
            """,
            "lxml",
        ).find("tr")
        volume_row = BeautifulSoup(
            """
            <tr><td class="celdas_1"></td><td class="celdas1">
              1.- <strong>Libro :</strong> Libro verificable<br/>
              Colombia, 2019, Volumen 2. ISBN: 978-958-5533-03-5,
              Autores: ANA PERSONA
            </td></tr>
            """,
            "lxml",
        ).find("tr")
        publisher_record = parse_product_row(publisher_row, "Libros publicados")
        volume_record = parse_product_row(volume_row, "Libros publicados")
        self.assertEqual(publisher_record["publisher"], "Voluntad")
        self.assertEqual(publisher_record["volume"], "")
        self.assertEqual(volume_record["volume"], "2")

    def test_empty_pages_does_not_absorb_authors(self):
        row = BeautifulSoup(
            """
            <tr><td class="celdas_1"></td><td class="celdas1">
              1.- <strong>Libro :</strong> Libro verificable<br/>
              Colombia, 2023, Editorial: Editorial Ejemplo, Idiomas: Español,
              Páginas: Autores: ANA PERSONA
            </td></tr>
            """,
            "lxml",
        ).find("tr")
        record = parse_product_row(row, "Ediciones")
        self.assertEqual(record["pages"], "")
        self.assertEqual(record["authors"], ["ANA PERSONA"])

    def test_timeline_sections_keep_title_period_and_description_separate(self):
        sections = [
            "Estrategias Pedagógicas para el fomento a la CTI",
            "Estrategias de Comunicación del Conocimiento",
            "Participación Ciudadana en Proyectos de CTI",
        ]
        for section in sections:
            with self.subTest(section=section):
                row = BeautifulSoup(
                    """
                    <tr>
                      <td class="celdas_1"><img src="chulo_1.jpg"/></td>
                      <td class="celdas1">
                        1.- <strong>Nombre real de la estrategia</strong>:
                        desde Marzo 2016 hasta Septiembre 2018<br/>
                        Descripción: Texto detallado de la actividad.
                      </td>
                    </tr>
                    """,
                    "lxml",
                ).find("tr")
                record = parse_product_row(row, section)
                self.assertEqual(record["title"], "Nombre real de la estrategia")
                self.assertEqual(record["product_type"], section)
                self.assertEqual(record["type_impactu"], section)
                self.assertEqual(record["start_date"], "Marzo 2016")
                self.assertEqual(record["end_date"], "Septiembre 2018")
                self.assertEqual(record["year"], 2018)
                self.assertEqual(record["description"], "Texto detallado de la actividad.")
                self.assertTrue(record["validated"])

    def test_bibliographic_row_keeps_generic_parser_contract(self):
        row = BeautifulSoup(
            """
            <tr>
              <td class="celdas_1"></td>
              <td class="celdas1">
                1.- <strong>Publicado en revista especializada:</strong>
                Un artículo verificable<br/>
                Colombia, Revista Ejemplo ISSN: 1234-5678, 2024,
                DOI: 10.1000/example Autores: ANA PERSONA
              </td>
            </tr>
            """,
            "lxml",
        ).find("tr")
        record = parse_product_row(row, "Artículos publicados")
        self.assertEqual(record["title"], "Un artículo verificable")
        self.assertEqual(record["type_impactu"], "Articulo de revista")
        self.assertEqual(record["year"], 2024)
        self.assertEqual(record["authors"], ["ANA PERSONA"])

    def test_chapter_extracts_book_publisher_and_pages(self):
        row = BeautifulSoup(
            """
            <tr>
              <td class="celdas_1"><img src="chulo_1.jpg"/></td>
              <td class="celdas1">
                1.- <strong>Capítulo de libro :</strong>
                Enseñanza de la Psicología Educativa<br/>
                Colombia, 2024, La enseñanza de la psicología en Colombia,
                ISBN: 978-958-798-684-6, Vol. 2, págs:100 - 120,
                Ed. Ediciones Uniandes Autores: ANA PERSONA
              </td>
            </tr>
            """,
            "lxml",
        ).find("tr")
        record = parse_product_row(row, "Capítulos de libro publicados")
        self.assertEqual(
            record["book_title"], "La enseñanza de la psicología en Colombia"
        )
        self.assertEqual(record["publisher"], "Ediciones Uniandes")
        self.assertEqual(record["volume"], "2")
        self.assertEqual(record["pages"], "100 - 120")
        self.assertEqual(record["start_page"], "100")
        self.assertEqual(record["end_page"], "120")

    def test_editorial_label_stops_before_language_and_pages(self):
        row = BeautifulSoup(
            """
            <tr><td class="celdas_1"></td><td class="celdas1">
              4.- <strong>Libro :</strong> Ebook Línea de Contenidos<br/>
              Colombia, 2016, Editorial: Servicio Nacional de Aprendizaje,
              Idiomas: Español, Páginas: 88 Autores: SONIA PERSONA
            </td></tr>
            """,
            "lxml",
        ).find("tr")
        record = parse_product_row(row, "Libros publicados")
        self.assertEqual(record["publisher"], "Servicio Nacional de Aprendizaje")
        self.assertEqual(record["language"], "Español")
        self.assertEqual(record["pages"], "88")

    def test_language_label_inside_book_title_does_not_leak_prose(self):
        row = BeautifulSoup(
            """
        <tr><td class="celdas_1"><img src="chulo_1.jpg"></td><td class="celdas1">
          4.- <strong>Capítulo de libro :</strong> Inmigrantes<br>
          Estados Unidos, 2017,
          educación de idiomas: nuevas direcciones para adultos y educación
          continua, Número 155, ISBN: 978-1-119-44378-0, Vol. , págs:61 - 69,
          Ed. Autores: CLARENA LARROTA
        </td></tr>
        """,
            "lxml",
        ).find("tr")
        record = parse_product_row(row, "Capítulos de libro publicados")
        self.assertEqual(record["language"], "")

    def test_empty_ed_field_does_not_absorb_authors(self):
        row = BeautifulSoup(
            """
            <tr><td class="celdas_1"></td><td class="celdas1">
              1.- <strong>Capítulo de libro :</strong> Capítulo verificable<br/>
              Colombia, 2020, Libro contenedor, ISBN: 978-958-798-684-6,
              Vol. , págs:18 - 118, Ed. Autores: ANA PERSONA
            </td></tr>
            """,
            "lxml",
        ).find("tr")
        record = parse_product_row(row, "Capítulos de libro publicados")
        self.assertEqual(record["publisher"], "")
        self.assertEqual(record["authors"], ["ANA PERSONA"])

    def test_timeline_without_end_date_keeps_start_date(self):
        row = BeautifulSoup(
            """
            <tr>
              <td class="celdas_1"></td>
              <td class="celdas1">
                1.- <strong>Semillero vigente</strong>:
                desde Febrero 2017 hasta<br/>
                Descripción: Actividad aún vigente.
              </td>
            </tr>
            """,
            "lxml",
        ).find("tr")
        record = parse_product_row(
            row, "Estrategias Pedagógicas para el fomento a la CTI"
        )
        self.assertEqual(record["title"], "Semillero vigente")
        self.assertEqual(record["start_date"], "Febrero 2017")
        self.assertEqual(record["end_date"], "")
        self.assertEqual(record["year"], 2017)

    def test_malformed_strong_tag_does_not_swallow_timeline_metadata(self):
        cases = [
            (
                """
                <tr><td class="celdas_0"></td><td class="celdas0">
                  6.- <strong>Revista Voces de la Escuela M
                  <ternal< strong>: desde Diciembre 2009 hasta<br/>
                  Descripción: Revista especializada
                  </ternal<></strong>
                </td></tr>
                """,
                "Revista Voces de la Escuela M",
                "Diciembre 2009",
                "",
            ),
            (
                """
                <tr><td class="celdas_1"></td><td class="celdas1">
                  5.- <strong>Semillero de
                  <investigación strong Ágora<>: desde Febrero 2018 hasta
                  Diciembre 2018<br/>
                  Descripción: Semillero fronterizo
                  </investigación></strong>
                </td></tr>
                """,
                "Semillero de",
                "Febrero 2018",
                "Diciembre 2018",
            ),
        ]
        section = "Estrategias Pedagógicas para el fomento a la CTI"
        for html, title, start_date, end_date in cases:
            with self.subTest(title=title):
                row = BeautifulSoup(html, "lxml").find("tr")
                record = parse_product_row(row, section)
                self.assertEqual(record["title"], title)
                self.assertEqual(record["start_date"], start_date)
                self.assertEqual(record["end_date"], end_date)


if __name__ == "__main__":
    unittest.main()
