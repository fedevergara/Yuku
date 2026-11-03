from copy import deepcopy
import json

import mongomock
from openpyxl import load_workbook
import pytest

from yuku.scraping_type_catalog import ScrapingTypeCatalog, get_scraping_catalog, observation_id
from yuku.scraping_type_review import consolidate, export_audit_excel as export_excel, import_excel, inventory
from yuku.scienti_types import classify_native, materialize_types, type_anomalies
from yuku.scienti_routing import route_cvlac, route_gruplac


def test_scraping_does_not_claim_dumps_origin_and_revalidation_is_stable():
    native = "Producción bibliográfica - Artículo - Publicado en revista especializada"
    doc = {"types": [{"source": "cvlac", "provenance": "scienti", "type": native, "parent": "Articulos", "level": 1},
                      {"source": "impactu", "provenance": "scienti", "type": "Artículo de revista"}]}
    materialize_types(doc)
    assert {e["source"] for e in doc["types"]} == {"minciencias", "impactu"}
    assert all(e["provenance"] == "minciencias" for e in doc["types"])
    assert type_anomalies(doc["types"]) == 0
    assert materialize_types(deepcopy(doc)) == doc


def test_official_divulgation_and_scraped_journal_keep_distinct_meanings():
    official = {"source": "minciencias", "type": "Artículos", "parent": "Apropiación social del conocimiento y divulgación pública de la ciencia", "level": 1}
    assert materialize_types({"types": [official]})["types"] == [official]
    catalog = get_scraping_catalog()
    assert catalog.inherit(official["type"], official["parent"])["mapping"]["type_impactu"] == "Publicaciones editoriales no especializadas"
    assert classify_native("Publicado en revista especializada:", "Artículos publicados") == "Artículo de revista"


def test_ambiguous_label_without_context_is_not_assigned_other():
    result = get_scraping_catalog().inherit("Congreso")
    assert result["status"] == "ambiguous" and result["mapping"] is None
    assert classify_native("Otra") == ""


def test_unknown_subtype_cannot_inherit_only_from_its_section():
    assert classify_native("Tipo desconocido nunca catalogado", "Articulos") == ""
    doc = materialize_types({"types": [{"source": "minciencias", "type": "Tipo desconocido nunca catalogado", "parent": "Articulos", "level": 1}]})
    assert not doc["types"] and doc["type_audit"]["findings"]


def test_other_preserves_its_context_and_entity():
    assert classify_native("Otra", "Softwares") == "Código fuente"
    assert classify_native("Otro", "Eventos Científicos") == "Evento"
    assert get_scraping_catalog().inherit("Marcas", "Signos distintivos")["mapping"] == {"entity": "patents", "type_impactu": "Otro"}


def test_project_channel_is_independent_of_artistic_canonical_label():
    result = route_cvlac("projects", {"project_type": "Investigación-Creación"})
    assert result["entity"] == "projects" and result["impactu_type"] == "Proyecto"
    assert route_gruplac({"source_section": "Eventos Científicos", "product_type": "Otro"})["entity"] == "events"


def payload():
    result = deepcopy(get_scraping_catalog().payload)
    result["catalog_version"] = "1.0.0"
    entry = {"id": observation_id("Artículos publicados", "Publicado en revista especializada:"),
             "parent": "Artículos publicados", "type": "Publicado en revista especializada:",
             "status": "inherited", "entity": "works", "type_impactu": "Artículo de revista",
             "justification": "Exact catalog path", "reviewer": "", "frequency": 3,
             "cvlac": 0, "gruplac": 3, "evidence": [],
             "examples": [{"title": "=A1", "source_id": "COL1", "url": "https://example.test"}]}
    result["entries"] = [entry]
    result["rejected_candidates"] = [{"parent": "Articulos", "candidate": "Texto\x02con control", "frequency": 1, "reason": "title", "example": {}}]
    return result


def test_excel_roundtrip_preserves_identities_literals_and_raw_evidence(tmp_path):
    original = payload(); path = tmp_path / "review.xlsx"
    export_excel(original, path)
    book = load_workbook(path)
    assert book["Ejemplos"]["G2"].value == "=A1" and book["Ejemplos"]["G2"].data_type == "s"
    assert "\\u0002" in book["Candidatos_rechazados"]["B2"].value
    book.close()
    result = import_excel(path, original, "1.0.1")
    assert result["entries"] == original["entries"]
    assert result["rejected_candidates"] == original["rejected_candidates"]


def test_changed_excel_assignment_requires_explicit_review(tmp_path):
    original = payload(); path = tmp_path / "review.xlsx"
    export_excel(original, path)
    book = load_workbook(path); sheet = book["Catalogo"]
    sheet["F2"] = "Libro"; book.save(path); book.close()
    with pytest.raises(ValueError, match="require"):
        import_excel(path, original, "1.0.1")
    book = load_workbook(path); sheet = book["Catalogo"]
    sheet["D2"] = "approved"; sheet["G2"] = "Correction from original HTML"; sheet["H2"] = "Project reviewer"
    book.save(path); book.close()
    result = import_excel(path, original, "1.0.1")
    assert ScrapingTypeCatalog(result).resolve(original["entries"][0]["type"], original["entries"][0]["parent"])["mapping"]["type_impactu"] == "Libro"


def test_excel_rejects_noncanonical_mapping_and_missing_rows(tmp_path):
    original = payload(); path = tmp_path / "review.xlsx"
    export_excel(original, path)
    book = load_workbook(path); sheet = book["Catalogo"]
    sheet["D2"] = "approved"; sheet["F2"] = "Un tipo inventado"; sheet["G2"] = "Review"; sheet["H2"] = "Reviewer"
    book.save(path); book.close()
    with pytest.raises(ValueError, match="canonical"):
        import_excel(path, original, "1.0.1")
    book = load_workbook(path); book["Catalogo"].delete_rows(2); book.save(path); book.close()
    with pytest.raises(ValueError, match="missing"):
        import_excel(path, original, "1.0.1")


def test_inventory_counts_occurrences_and_preserves_examples_without_writes():
    db = mongomock.MongoClient().dam
    db.cv.insert_one({"_id": "1", "projects": [{"project_type": "Investigación-Creación", "title": "Proyecto uno"}]})
    db.gr.insert_one({"_id": "COL1", "production": [{"product_type": "Otro", "source_section": "Eventos Científicos", "title": "Evento uno"}]})
    before = {n: list(db[n].find()) for n in db.list_collection_names()}
    result = inventory(db, {"cvlac": "cv", "gruplac": "gr"})
    assert result["totals"] == {"cvlac_documents": 1, "cvlac_occurrences": 1, "gruplac_documents": 1, "gruplac_occurrences": 1}
    assert {e["entity"] for e in result["entries"]} == {"events", "projects"}
    assert before == {n: list(db[n].find()) for n in db.list_collection_names()}


def test_pending_catalog_entry_cannot_make_a_title_a_valid_type():
    data = payload(); entry = data["entries"][0]
    entry.update(status="pending", entity="", type_impactu="")
    result = ScrapingTypeCatalog(data).resolve(entry["type"], entry["parent"])
    assert not result["native"] and not result["mapping"]


def test_excel_can_promote_a_new_verified_heading(tmp_path):
    original = payload(); path = tmp_path / "review.xlsx"
    export_excel(original, path)
    book = load_workbook(path)
    book["Catalogo"].append([None, "Sección nueva", "Tipo nuevo verificado", "approved", "works",
                             "Libro", "Verified against the preserved HTML", "Reviewer"])
    book.save(path); book.close()
    result = import_excel(path, original, "1.0.1")
    entry = result["entries"][-1]
    assert entry["id"] == observation_id("Sección nueva", "Tipo nuevo verificado")
    assert ScrapingTypeCatalog(result).resolve(entry["type"], entry["parent"])["mapping"]["type_impactu"] == "Libro"


def test_revised_catalog_entity_controls_routing(monkeypatch):
    from yuku import scienti_routing
    monkeypatch.setattr(scienti_routing, "native_type", lambda *args: {
        "mapping": {"entity": "patents", "type_impactu": "Otro"}})
    route = scienti_routing.route_cvlac("production", {"product_type": "Tipo aprobado de propiedad intelectual"})
    assert route["entity"] == "patents" and route["impactu_type"] == "Otro"


def test_behavior_fingerprint_is_independent_of_rejected_titles_and_frequencies():
    original = payload(); changed = deepcopy(original)
    changed["rejected_candidates"] = []
    changed["entries"][0]["frequency"] += 100
    assert ScrapingTypeCatalog(original).fingerprint == ScrapingTypeCatalog(changed).fingerprint
    changed["entries"][0]["type_impactu"] = "Libro"
    assert ScrapingTypeCatalog(original).fingerprint != ScrapingTypeCatalog(changed).fingerprint


def test_shared_assignment_overrides_local_observation_target(monkeypatch):
    from kahi_impactu_type_catalog.catalog import ImpactuTypeCatalog
    from yuku import scraping_type_catalog as module
    central = deepcopy(module.get_impactu_catalog().payload)
    entry = central["scraping_mappings"][0]
    entry.update(entity="works", type_impactu="Libro")
    shared = ImpactuTypeCatalog(central)
    module.get_scraping_catalog.cache_clear()
    try:
        monkeypatch.setattr(module, "get_impactu_catalog", lambda: shared)
        catalog = module.get_scraping_catalog()
        assert catalog.resolve(entry["type"], entry["parent"])["mapping"] == {
            "entity": "works", "type_impactu": "Libro"}
        assert catalog.payload["shared_catalog"]["version"] == shared.version
    finally:
        module.get_scraping_catalog.cache_clear()


def test_empty_shared_scraping_index_does_not_resurrect_local_assignments(monkeypatch):
    from kahi_impactu_type_catalog.catalog import ImpactuTypeCatalog
    from yuku import scraping_type_catalog as module
    central = deepcopy(module.get_impactu_catalog().payload)
    central["scraping_mappings"] = []
    central["source"]["scraping_rows"] = 0
    shared = ImpactuTypeCatalog(central)
    module.get_scraping_catalog.cache_clear()
    try:
        monkeypatch.setattr(module, "get_impactu_catalog", lambda: shared)
        assert module.get_scraping_catalog().payload["entries"] == []
    finally:
        module.get_scraping_catalog.cache_clear()


def test_work_graph_checkpoint_refuses_changed_catalog(monkeypatch):
    from yuku import cvlac_work_graph as graphs
    db = mongomock.MongoClient().dam
    builder = graphs.CheckpointedNormalizedWorkGraphBuilder(
        db, collection="graph", source_collection="cv", group_source_collection="gr", gate={})
    builder._prepare_run("catalog_checkpoint")
    old_config = deepcopy(builder.config)
    changed = deepcopy(old_config["scraping_type_catalog"])
    changed["sha256"] = "changed-by-reviewed-catalog"
    monkeypatch.setattr(graphs, "catalog_identity", lambda: changed)
    with pytest.raises(ValueError, match="different config"):
        builder._prepare_run("catalog_checkpoint")
    assert db.graph_graph_runs.find_one({"_id": "catalog_checkpoint"})["config"] == old_config


def test_consolidation_preserves_counts_when_presentation_aliases_merge():
    data = payload()
    base = data["entries"][0]
    base.update(parent="Redes de Conocimiento Especializado", type="Red de conocimiento especializado",
                frequency=4, cvlac=0, gruplac=4, channels={"production": 4})
    other = deepcopy(base); other.update(type="Redes de Conocimiento Especializado", frequency=7, gruplac=7,
                                         channels={"production": 7})
    data.update(entries=[base, other], rejected_candidates=[], totals={"gruplac_occurrences": 11})
    result = consolidate(data)
    observed = [e for e in result["entries"] if e["frequency"]]
    assert len(observed) == 1 and observed[0]["frequency"] == 11
    assert sum(v["frequency"] for v in observed[0]["variants"]) == 11


@pytest.mark.parametrize("corrupt", [False, True])
def test_v4_to_v5_comparison_accepts_new_provider_but_rejects_invalid_types(corrupt):
    from yuku.scienti_entities import ENTITY_NORMALIZER_VERSION, ENTITY_RUNS, normalize_occurrence
    from yuku.scienti_entity_comparison import ScientiEntityVersionComparator
    from yuku.scienti_routing import ENTITY_ROUTER_VERSION
    db = mongomock.MongoClient().dam
    examples = {
        "works": ("production", {"product_type": "Producción bibliográfica - Artículo - Caso clínico", "source_section": "Articulos"}),
        "projects": ("projects", {"project_type": "Investigación-Creación"}),
        "patents": ("patents", {"product_type": "Patente de invención"}),
        "events": ("events", {"event_type": "Congreso"}),
    }
    destinations = {version: {e: version + "_" + e for e in examples} for version in ("old", "new")}
    for entity, (channel, fields) in examples.items():
        record = {"title": "Título verificable de " + entity, **fields}
        route = route_cvlac(channel, record)
        document = normalize_occurrence(route, record, source_kind="cvlac", source_id="0000000001",
                                        source_collection="profiles", source_url="", record_index=0)
        old = deepcopy(document)
        old["source_metadata"].update(normalizer_version="scienti-entity-normalizer-v4", router_version="scienti-exact-router-v6")
        old["source_metadata"].pop("scraping_type_catalog")
        db[destinations["old"][entity]].insert_one(old)
        if corrupt and entity == "projects":
            document["types"].append({"source": "impactu", "type": "Este texto no es un tipo"})
        db[destinations["new"][entity]].insert_one(document)
    for version, normalizer, router in (("old", "scienti-entity-normalizer-v4", "scienti-exact-router-v6"),
                                        ("new", ENTITY_NORMALIZER_VERSION, ENTITY_ROUTER_VERSION)):
        db[ENTITY_RUNS].insert_one({"_id": version, "status": "complete", "config_hash": version,
                                   "config": {"destinations": destinations[version], "normalizer_version": normalizer,
                                              "router_version": router}})
    result = ScientiEntityVersionComparator(db, comparison_name="type_catalog_transition",
                                           old_run_name="old", new_run_name="new").run()
    assert result["transition_profile"] == "snapshot_refresh"
    assert result["status"] == ("failed" if corrupt else "passed")
