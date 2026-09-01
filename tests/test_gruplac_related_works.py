import unittest

from bs4 import BeautifulSoup

from yuku.gruplac_related_works import parse_product_row


class GruplacRelatedWorksParsingTest(unittest.TestCase):
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
