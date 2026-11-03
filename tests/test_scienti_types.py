"""Regressions for actual title leakage and source-specific vocabularies."""

from copy import deepcopy

from bs4 import BeautifulSoup
from kahi_impactu_type_catalog import get_impactu_catalog
import mongomock
import pytest

from yuku.cvlac_related_works import parse_blockquote
from yuku.gruplac_related_works import parse_product_row
from yuku.scienti_types import classify_native, materialize_types, validate_types

TITLE = "Women in Academia and Research: An Overview of the Challenges Toward Gender Equality in Colombia and How to Move Forward"


def test_bold_article_title_is_never_a_type_or_removed_from_title():
    html = f"<tr><td></td><td>1.- <strong>{TITLE}</strong> En: Colombia<br>2018, DOI: 10.3389/fspas.2018.00024</td></tr>"
    record = parse_product_row(BeautifulSoup(html, "html.parser").tr, "Artículos publicados")
    assert record["title"] == TITLE
    assert record["product_type"] != TITLE
    assert record["type_impactu"] == "Artículo de revista"
    assert record["type_findings"][0]["raw_type"] == TITLE
    assert TITLE in record["raw_text"]


def test_cvlac_previous_row_title_does_not_become_type():
    soup = BeautifulSoup(f'<table><tr><td><b>{TITLE}</b></td></tr><tr><td><blockquote>AUTOR, "Otro título verificable" . En: Colombia, 2018</blockquote></td></tr></table>', "html.parser")
    parsed = parse_blockquote("0000000001", "Articulos", soup.blockquote)
    assert parsed["product_type"] == "Articulos"
    assert parsed["type_impactu"] == "Artículo de revista"
    assert parsed["type_findings"][0]["raw_type"] == TITLE


@pytest.mark.parametrize("value", ["Capitulo de libro", "Capitulos de libro", "Capítulo de libro", "Capítulos de libro"])
def test_chapter_variants_have_one_canonical_impactu_label(value):
    assert classify_native(value, "Capítulos de libro publicados") == "Capítulo de libro"
    accepted, findings = validate_types([{"source": "gruplac", "type": value}])
    assert accepted == [{"source": "minciencias", "type": "Capítulo de libro"}]
    assert findings == []


@pytest.mark.parametrize("subtype,expected", [
    ("Caso clínico", "Artículo de revista"), ("Corto (Resumen)", "Artículo de revista"),
    ("Publicado en revista especializada", "Artículo de revista"), ("Revisión (Survey)", "Artículo de revista"),
    ("Carta al editor", "Artículo de revista"), ("Reseña de libro", "Artículo de revista"),
    ("Columna de opinión", "Publicaciones editoriales no especializadas"),
    ("Periódico de noticias", "Publicaciones editoriales no especializadas"),
    ("Revista de divulgación", "Publicaciones editoriales no especializadas"),
])
def test_native_article_subtypes_are_preserved_and_mapped(subtype, expected):
    parent = "Otro artículo publicado" if subtype in {"Carta al editor", "Reseña de libro", "Columna de opinión", "Periódico de noticias", "Revista de divulgación"} else "Artículo"
    native = f"Producción bibliográfica - {parent} - {subtype}"
    assert classify_native(native) == expected
    assert validate_types([{"source": "cvlac", "type": native}]) == ([{"source": "minciencias", "type": native}], [])


def test_all_catalog_scienti_paths_round_trip_without_losing_subtypes():
    for mapping in get_impactu_catalog().payload["mappings"]:
        if mapping["source"] == "scienti":
            assert classify_native(mapping["type"]) == mapping["type_impactu"], mapping
            accepted, findings = validate_types([{"source": "scienti", "type": mapping["type"]}], entity=mapping["entity"])
            assert accepted and not findings, mapping


def test_same_native_word_has_different_meaning_in_different_sections():
    assert classify_native("Libro", "Traducciones") == "Libro"
    assert classify_native("Libro", "Ediciones") == "Otro"
    assert classify_native("Otra") == ""  # Ambiguity must not silently become Otro.


def test_all_sources_are_validated_and_original_evidence_is_retained():
    entries = [{"source": "impactu", "type": "Articulo de revista"},
               {"source": "gruplac", "type": TITLE},
               {"source": "scienti", "type": TITLE},
               {"source": "minciencias", "type": TITLE, "level": 0},
               {"source": "minciencias", "type": "Artículos de investigación", "parent": "Nuevo conocimiento", "level": 1}]
    original = deepcopy(entries)
    document = materialize_types({"types": entries})
    assert len(document["types"]) == 2
    assert document["types"][0]["type"] == "Artículo de revista"
    assert len(document["type_audit"]["findings"]) == 3
    assert all(f["raw_type"] == TITLE for f in document["type_audit"]["findings"])
    assert entries == original


def test_publication_gate_rejects_invalid_types_and_keeps_current():
    from yuku.scienti_release import ScientiFinalReleaseManager
    db = mongomock.MongoClient().dam
    db.scienti_final_release_publications.insert_one({"_id": "current", "current_release": "previous"})
    collections = {e: "output_" + e for e in ("works", "projects", "patents", "events")}
    runs = {e: "run_" + e for e in collections}
    for entity in collections:
        db[collections[entity]].insert_one({"_id": "one", "types": [{"source": "impactu", "type": TITLE}] if entity == "works" else []})
        db.minciencias_measurement_runs.insert_one({"_id": runs[entity], "status": "complete", "summary": {"target_entity": entity, "collection": collections[entity], "documents": 1}})
    with pytest.raises(RuntimeError, match="final release audit failed"):
        ScientiFinalReleaseManager(db).publish(release_name="new", collections=collections, materialization_runs=runs)
    assert db.scienti_final_release_publications.find_one({"_id": "current"})["current_release"] == "previous"


def test_work_types_cannot_route_other_entities_into_works():
    accepted, findings = validate_types([{"source": "impactu", "type": "Patente"}])
    assert not accepted and findings[0]["reason"] == "type_belongs_to_other_entity"


def test_specialized_controller_reextracts_old_sources_before_doi_candidates(monkeypatch):
    from scripts.run_scienti_journal_doi import refresh_type_sources
    from yuku import scienti_normalization as normalizers
    db = mongomock.MongoClient().dam
    db[normalizers.CVLAC_AUDIT_RUNS].insert_one({"_id": "cv_audit", "status": "passed", "config": {
        "parser_version": "3.3.0", "normalization_run_name": "cv_run", "source_state_collection": "cv_state"}})
    db[normalizers.CVLAC_NORMALIZATION_RUNS].insert_one({"_id": "cv_run", "config": {"source_snapshot_run_name": "snapshot"}})
    db[normalizers.GRUPLAC_AUDIT_RUNS].insert_one({"_id": "gr_audit", "status": "passed", "config": {
        "expected_parser_version": "3.3.0", "raw_collection": "gr_raw", "state_collection": "gr_state",
        "recognized_groups_collection": "groups"}})
    calls = []
    class Operation:
        def __init__(self, db, **kwargs):
            calls.append(kwargs)
        def run(self):
            return {"status": "passed"}
    for name in ("CvlacNormalizationRun", "CvlacNormalizationAuditor", "GruplacNormalizationRun", "GruplacNormalizationAuditor"):
        monkeypatch.setattr(normalizers, name, Operation)
    phases = []
    def phase(name, function):
        phases.append(name)
        return function()
    sources, audits = refresh_type_sources(db, "new", {"cvlac": "cv_old", "gruplac": "gr_old"},
        {"cvlac": "cv_audit", "gruplac": "gr_audit"}, phase, 100)
    assert phases == ["cvlac_types_normalize", "cvlac_types_audit", "gruplac_types_normalize", "gruplac_types_audit"]
    assert sources == {"cvlac": "cvlac_types_new", "gruplac": "gruplac_types_new"}
    assert audits == {"cvlac": "new_cvlac_types_audit", "gruplac": "new_gruplac_types_audit"}
    assert calls[0]["source_snapshot_run_name"] == "snapshot" and calls[2]["source_raw_collection"] == "gr_raw"
