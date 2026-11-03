from copy import deepcopy
import json
from pathlib import Path

from bs4 import BeautifulSoup
import pytest

from yuku.cvlac_related_works import clean_text, parse_event_row
from yuku.scienti_entities import normalize_occurrence
from yuku.scienti_events import normalize_event_type
from yuku.scienti_routing import route_cvlac
from yuku.scienti_types import classify_native, materialize_types, type_anomalies

FIXTURES = Path(__file__).parent / "fixtures" / "cvlac_event_types"
ORIGINALS = json.loads((FIXTURES / "original-records.txt").read_text())
CASES = [("0000836630", "Congreso"), ("0000086456", "Congreso"), ("0000587290", "Otro"),
         ("0000205818", "Congreso"), ("0001114905", "Congreso"), ("0000100602", "Encuentro"),
         ("0000186861", "Seminario"), ("0000013927", "Simposio"), ("0000689254", "Simposio")]


@pytest.mark.parametrize("identifier,expected", CASES)
def test_real_html_repeated_event_labels_preserve_every_other_field(identifier, expected):
    row = BeautifulSoup((FIXTURES / (identifier + ".html")).read_text(), "lxml").tr
    event = parse_event_row(identifier, row, profile_author_name="Fixture author")
    original = deepcopy(ORIGINALS[identifier])
    assert "Tipo de evento:" in original["event_type"]
    original["event_type"] = expected
    assert event == original
    assert event["raw_text"] == clean_text(row.get_text(" ", strip=True))
    route = route_cvlac("events", event)
    assert route["entity"] == "events" and route["impactu_type"] == "Evento"


@pytest.mark.parametrize("identifier,expected", CASES)
def test_legacy_normalized_records_also_publish_clean_native_types(identifier, expected):
    original = deepcopy(ORIGINALS[identifier])
    original["title"] = "Título original verificable " + identifier
    raw_type = original["event_type"]
    assert classify_native(raw_type, "Eventos Científicos") == "Evento"
    route = route_cvlac("events", original)
    document = normalize_occurrence(route, original, source_kind="cvlac", source_id=identifier,
                                    source_collection="normalized_test", source_url="", record_index=0)
    assert document["types"] == [
        {"provenance": "minciencias", "source": "minciencias", "type": expected, "level": 1, "parent": "Eventos Científicos"},
        {"provenance": "minciencias", "source": "impactu", "type": "Evento"}]
    assert type_anomalies(document["types"], entity="events") == 0
    assert materialize_types(deepcopy(document), entity="events") == document
    occurrence = document["source_metadata"]["occurrences"][0]
    assert occurrence["metadata"]["event_type"] == expected
    assert any(f["kind"] == "source_event_type_normalized" for f in occurrence["normalization_findings"])
    assert original["event_type"] == raw_type


@pytest.mark.parametrize("value", ["Congreso médico internacional", "Congreso Tipo de evento: Categoría nueva",
                                    "Una investigación sobre congresos", "Tipo de producto: Congreso"])
def test_unknown_or_unstructured_event_values_are_not_guessed(value):
    assert normalize_event_type(value) == value


def test_last_explicit_declaration_wins_over_free_text_prefix():
    assert normalize_event_type("Congreso Tipo de evento: Otro") == "Otro"
    assert normalize_event_type("Seminario Tipo  de  evento:  SEMINARIO ") == "Seminario"


@pytest.mark.parametrize("parent,value", [("Espacios de Participación Ciudadana", "Programa radial"),
                                           ("Redes de Conocimiento Especializado", "Iniciación Científica"),
                                           ("Regulaciones y Normas", "Regulaciones y Normas")])
def test_provisional_other_assignments_are_context_specific(parent, value):
    assert classify_native(value, parent) == "Otro"
    entry = {"source": "minciencias", "provenance": "minciencias", "type": value, "parent": parent, "level": 1}
    assert materialize_types({"types": [entry]})["types"] == [entry]
    assert classify_native("Iniciación Científica", "Eventos Científicos") == ""
