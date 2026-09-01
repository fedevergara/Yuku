import unittest
from unittest.mock import patch

import mongomock

from yuku.scienti_profiles import ScientiProfileDownloader, decode_scienti_response


class FakeResponse:
    def __init__(self, content, encoding=None):
        self.content = content
        self.encoding = encoding


class ScientiResponseDecodingTest(unittest.TestCase):
    def test_declared_iso_encoding_wins_over_missing_or_heuristic_information(self):
        response = FakeResponse(
            b'<meta http-equiv="Content-Type" content="text/html; charset=ISO-8859-1">'
            b'Datos b\xe1sicos, L\xEDder y producci\xF3n',
            encoding=None,
        )
        html, encoding, replacement_chars = decode_scienti_response(response)
        self.assertEqual(encoding, "iso-8859-1")
        self.assertIn("Datos básicos, Líder y producción", html)
        self.assertEqual(replacement_chars, 0)

    def test_response_encoding_is_used_when_meta_charset_is_absent(self):
        response = FakeResponse("Información".encode("utf-8"), encoding="utf-8")
        html, encoding, replacement_chars = decode_scienti_response(response)
        self.assertEqual(html, "Información")
        self.assertEqual(encoding, "utf-8")
        self.assertEqual(replacement_chars, 0)

    def test_gruplac_without_basic_section_is_preserved_as_incomplete(self):
        html = """
        <html><head><title>GrupLAC - Plataforma SCienTI - Colombia</title></head>
        <body><td class="celdaEncabezado">Plan Estratégico</td></body></html>
        """
        self.assertEqual(
            ScientiProfileDownloader.classify_html("gruplac", html),
            "incomplete",
        )

    def test_downloader_reduces_rate_after_final_error_threshold(self):
        db = mongomock.MongoClient().dam
        downloader = ScientiProfileDownloader(
            db, workers=1, requests_per_second=4.0, state_collection="states"
        )
        with patch.object(downloader, "_download_one", return_value="error"):
            counts = downloader.download(
                "cvlac",
                [{"id": "1", "url": "https://example.test/1"}],
                raw_collection="raw",
                fallback_requests_per_second=2.0,
                fallback_after_errors=1,
            )
        self.assertEqual(counts["rate_fallbacks"], 1)
        self.assertEqual(counts["final_requests_per_second"], 2.0)


if __name__ == "__main__":
    unittest.main()
