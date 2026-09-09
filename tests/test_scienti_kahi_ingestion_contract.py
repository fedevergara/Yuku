import json
from pathlib import Path


CONTRACT_PATH = (
    Path(__file__).parents[1]
    / "docs"
    / "scienti_kahi_ingestion_contract_v1.json"
)


def load_contract():
    with CONTRACT_PATH.open(encoding="utf-8") as stream:
        return json.load(stream)


def test_contract_pins_the_audited_v11_release():
    contract = load_contract()
    source = contract["source"]

    assert contract["contract_version"] == "scienti-kahi-ingestion-v1"
    assert source["release_name"] == (
        "scienti_final_release_publishers_v11_20260902"
    )
    assert source["required_audit_status"] == "passed"
    assert source["required_critical_anomalies"] == 0
    assert source["collections"] == {
        "works": {
            "name": "scienti_works_final_publishers_v11_20260902",
            "documents": 3837870,
        },
        "projects": {
            "name": "scienti_projects_final_v5_20260901",
            "documents": 642519,
        },
        "patents": {
            "name": "scienti_patents_final_v5_20260901",
            "documents": 9789,
        },
        "events": {
            "name": "scienti_events_final_v5_20260901",
            "documents": 1466152,
        },
    }


def test_contract_never_reparses_final_entities_as_open_data_rows():
    policy = load_contract()["import_policy"]

    assert policy["source_documents_are_pre_normalized"] is True
    assert policy["reparse_as_gruplac_production_data"] is False
    assert policy["allow_new_destination_fields"] is False
    assert policy["mutate_source_database"] is False
    assert policy["allow_name_only_author_resolution"] is False


def test_contract_keeps_only_compact_doi_identity_anchors():
    persons = load_contract()["planned_identity_entities"]["persons"]
    related = persons["related_works"]

    assert related["allowed_source"] == "doi"
    assert related["require_canonical_doi"] is True
    assert related["include_products_without_supported_identifier"] is False
    assert related["include_title_or_full_product_metadata"] is False
    quality = load_contract()["six_entity_publication"]["source_quality"]
    assert quality["exclude_invalid_work_doi_from_identity_evidence"] is True
    assert quality["report_excluded_invalid_work_doi"] is True


def test_contract_marks_graph_evidence_as_source_only():
    entities = load_contract()["entities"]

    assert "bibliographic_info" in entities["works"]["fields"]
    assert "authorship_status" not in entities["works"]["fields"]
    assert entities["works"]["source_only_fields"] == [
        "source_metadata",
        "authorship_status",
    ]
    for entity in ("projects", "patents", "events"):
        assert entities[entity]["source_only_fields"] == ["source_metadata"]


def test_bibliographic_info_reuses_the_existing_kahi_container():
    works = load_contract()["entities"]["works"]
    policy = works["bibliographic_info_policy"]

    assert policy["reuse_existing_destination_field"] is True
    assert policy["allow_new_top_level_fields"] is False
    assert policy["merge_only_nonempty_values"] is True
    assert policy["preserve_existing_nonempty_values"] is True
    assert policy["minciencias_evidence_path"] == (
        "bibliographic_info.minciencias"
    )
    assert len(works["fields"]) == len(set(works["fields"]))


def test_identity_baselines_cover_the_full_public_snapshot():
    planned = load_contract()["planned_identity_entities"]

    assert planned["persons"]["frozen_manifest_valid_ids"] == 437606
    assert planned["persons"]["identity_universe"] == {
        "include_frozen_cvlac_manifest": True,
        "include_valid_audited_gruplac_members": True,
        "allow_name_only_identity": False,
    }
    assert planned["affiliations"]["expected_group_union_baseline"] == 9537
    assert planned["affiliations"]["institution_resolution_owner"] == "kahi"


def test_final_kahi_publication_is_atomic_across_six_entities():
    publication = load_contract()["six_entity_publication"]

    assert publication["entities"] == [
        "works", "projects", "patents", "events", "persons", "affiliations",
    ]
    assert all(publication["direct_reference_validation"].values())
    assert publication["allow_pointer_switch_with_critical_anomalies"] is False
