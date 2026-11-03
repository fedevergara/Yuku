"""Regressions from the 2026-10-18 project/patent metadata audit."""
from copy import deepcopy
from datetime import date, timedelta
from itertools import permutations

import mongomock
import pytest

from yuku.minciencias_measurements import (
    MincienciasMeasurementPipeline, enrich_graph_work, normalize_measured_product,
    standalone_official_entity,
)
from yuku.scienti_entities import (
    ENTITY_AUDITS, ENTITY_RUNS, empty_kahi_patent, empty_kahi_project,
    iter_cvlac_entities, merge_entity_documents, normalize_occurrence,
)
from yuku.scienti_entity_graph import ScientiExactEntityGraphBuilder
from yuku.scienti_product_quality import (
    actual_date, clean_product_text, date_consensus, placeholder_title,
    product_quality_findings, reconcile_project_dates,
)
from yuku.scienti_routing import route_cvlac
from yuku.scienti_types import materialize_types


def occurrence(entity="projects", index=0, **fields):
    record = {
        "title": "Coherencia y pertinencia del modelo pedagógico universitario",
        "project_type": "Investigación y desarrollo", "start_date": "Marzo 2019",
        "profile_name": "Autora Verificada", "profile_id": "0001481067",
        **fields,
    }
    if entity == "patents":
        record.setdefault("product_type", "Patente de invención")
    return normalize_occurrence(route_cvlac(entity, record), record,
        source_kind="cvlac", source_id="0001481067", source_collection="profiles",
        source_url="", record_index=index, normalized_at=1)


def row(title="Estructura rígida articulada", created="2018-05-01T00:00:00.000", **fields):
    return {
        "id_producto_pd": "PIV-0001481067-1", "id_persona_pd": "0001481067",
        "nme_clase_pd": "Nuevo conocimiento", "nme_tipologia_pd": "Patente de invención",
        "nme_producto_pd": title, "fcreacion_pd": created,
        "id_convocatoria": "18", "ano_convo": "2015-10-15T00:00:00.000",
        **fields,
    }


@pytest.mark.parametrize("value", ["2103/10", "Octubre 2103", "1900/1", "1900-01-01T00:00:00", "2019/0", "2019/2/30", "2019/13", "Junio", "2001 basura"])
def test_reject_invalid_real_dates_without_guessing(value):
    d=occurrence(end_date=value)
    assert d["date_end"] is None and d["year_end"] is None
    assert d["source_metadata"]["occurrences"][0]["metadata"]["end_date"] == value
    assert not product_quality_findings(d, "projects")


def test_future_real_dates_rejected_but_current_year_precision_allowed():
    today=date(2026,10,18)
    assert actual_date(today.isoformat(),as_of=today)
    assert actual_date((today+timedelta(days=1)).isoformat(),as_of=today) is None
    assert actual_date("2026",as_of=today)=={"year":2026,"month":None,"day":None}


def test_month_disagreement_is_order_independent_and_keeps_only_year():
    docs=[occurrence(index=i,start_date=f"{month} 2019",end_date=f"{end} 2023")
          for i,(month,end) in enumerate((("Marzo","Julio"),("Abril","Diciembre"),("Mayo","Julio")))]
    for order in permutations(docs):
        merged=deepcopy(order[0])
        for d in order[1:]:
            other=deepcopy(d);other["_id"]=merged["_id"]
            merged=merge_entity_documents(merged,other)
        assert (merged["date_init"],merged["year_init"]) == (None,2019)
        assert (merged["date_end"],merged["year_end"]) == (None,2023)
        assert len(merged["source_metadata"]["occurrences"])==3
        assert not product_quality_findings(merged,"projects")


def test_disagreeing_years_and_year_only_dates_do_not_invent_timestamps():
    assert date_consensus(["2018/1","2019/1"])==(None,None)
    assert date_consensus(["2019"])==(None,2019)
    d=occurrence(start_date="2019",projected_end_date="2030/1")
    assert d["date_init"] is None and d["year_init"]==2019
    assert d["date_end"] is None and d["year_end"] is None


@pytest.mark.parametrize("entity",["projects","patents"])
def test_unknown_language_is_empty_and_explicit_declaration_is_respected(entity):
    assert occurrence(entity,title="METHOD FOR PRODUCING COLLOIDOSOME MICROCAPSULES")["titles"][0]["lang"]==""
    assert occurrence(entity,language="English")["titles"][0]["lang"]=="en"


def test_decodes_nested_entities_and_composes_accents_before_identity():
    a=occurrence(title="Estructura RI&amp;#769;GIDA articulada para protección corporal")
    b=occurrence(title="Estructura RÍGIDA articulada para protección corporal",index=1)
    assert a["titles"][0]["title"]==b["titles"][0]["title"]
    assert a["_id"]==b["_id"]
    official=normalize_measured_product([row(title="ESTRUCTURA RI&#769;GIDA ARTICULADA")])
    assert official["titles"][0]["title"]=="ESTRUCTURA RÍGIDA ARTICULADA"
    assert clean_product_text("a\u00a0b\x00") == "a b"


@pytest.mark.parametrize("title",["Prueba", " PATENTE. ","test","Sin título"])
def test_excludes_exact_placeholder_titles_from_scraping_and_official_routes(title):
    assert occurrence(title=title) is None
    official=normalize_measured_product([row(title=title)])
    assert official["titles"]==[]
    assert official["bibliographic_info"]["minciencias"]["target_entity"]=="excluded"
    profile={"_id":"0001481067","projects":[{"title":title,"project_type":"Investigación y desarrollo"}]}
    entries=list(iter_cvlac_entities(profile,"profiles",1))
    assert entries[0][0]=="excluded_incomplete"
    assert entries[0][2]["reason"]=="placeholder_title"


def test_short_names_and_titles_containing_prueba_are_not_excluded():
    assert not placeholder_title("GEIO")
    assert not placeholder_title("Prueba de concepto para el control de plagas")
    assert occurrence(title="GEIO") is not None


def test_official_invalid_date_preserved_as_evidence_not_as_a_valid_measurement():
    official=normalize_measured_product([row(created="1900-01-01T00:00:00.000"),row(created="2011-02-22T00:00:00.000")])
    ms=official["bibliographic_info"]["minciencias"]["measurements"]
    assert {m["presentation_year"] for m in ms}=={2011,None}
    bad=next(m for m in ms if m["presentation_year"] is None)
    assert bad["presentation_date"] is None
    assert bad["normalization_findings"][0]["raw_value"].startswith("1900")
    d=standalone_official_entity(official,{},target_entity="patents",materialized_at=1)
    assert d["authors"]==[] and d["author_count"]==0
    assert not product_quality_findings(d,"patents")


def test_recovers_explicit_cvlac_coauthors_but_does_not_invent_group_members():
    d=occurrence("patents",authors=[{"full_name":"Otra Autora","cod_rh":"0000000002"}])
    assert {a["id"] for a in d["authors"]}=={"0001481067","0000000002"}


def test_does_not_create_nameless_authors_or_duplicate_owner_aliases():
    d=occurrence("patents",authors=[{"id":"0000000002"},
        {"full_name":"Alias de autora","cod_rh":"0001481067"}])
    assert d["author_count"]==1
    assert d["authors"][0]["full_name"]=="Autora Verificada"


def test_impactu_redundancy_removed_without_collapsing_native_contexts():
    d={"types":[
        {"source":"impactu","type":"Patente","provenance":"minciencias"},
        {"source":"impactu","type":"Patente","provenance":"minciencias","level":0,"parent":None},
        {"source":"minciencias","type":"Patente de invención","parent":"Patentes","level":1},
        {"source":"minciencias","type":"Patente de invención","parent":"Nuevo conocimiento","level":1},
    ]}
    materialize_types(d,entity="patents")
    assert len(d["types"])==3
    assert len([t for t in d["types"] if t["source"]=="impactu"])==1
    assert len({t["parent"] for t in d["types"] if t["source"]=="minciencias"})==2


def graph(db, docs, name="regression"):
    db.source.insert_many(docs)
    db[ENTITY_RUNS].insert_one({"_id":"normalized","status":"complete","config":{"destinations":{"patents":"source"}}})
    db[ENTITY_AUDITS].insert_one({"_id":"normalized","status":"passed","critical_anomalies":0})
    db.scienti_entity_publications.insert_one({"_id":"normalized","status":"current"})
    builder=ScientiExactEntityGraphBuilder(db,entity="patents",run_name=name,
        source_collection="source",target_collection="final",entity_run_name="normalized")
    return builder.run()


def test_real_short_title_duplicate_merges_and_preserves_both_declarations():
    docs=[occurrence("patents",index=i,title="Calcetin protector de pierna",
        registration_number="NC2018/0004655",country="Colombia",presentation_date="2018-05-01") for i in (0,1)]
    db=mongomock.MongoClient().dam
    result=graph(db,docs)
    assert result["components"]==1
    d=db.final.find_one()
    assert len(d["source_metadata"]["occurrences"])==2
    assert d["author_count"]==1
    assert "exact_short_title_registration_country_author_compatible_dates" in d["source_metadata"]["entity_graph"]["link_rules"]


@pytest.mark.parametrize("field,value",[("country","Brasil"),("registration_number","OTRO/987654"),("presentation_date","2018-06-01"),("profile_id","0000000002")])
def test_strong_title_matching_does_not_bridge_contradictions(field,value):
    values=dict(title="Calcetin protector de pierna",registration_number="NC2018/0004655",country="Colombia",presentation_date="2018-05-01")
    a=occurrence("patents",index=0,**values)
    b=occurrence("patents",index=1,**(values|{field:value}))
    db=mongomock.MongoClient().dam
    assert graph(db,[a,b])["components"]==2


def test_pending_registration_is_not_a_matching_anchor():
    d=occurrence("patents",registration_number="Pendiente",presentation_date="1900-01-01")
    o=d["source_metadata"]["occurrences"][0]
    assert "registration_number" not in o["metadata"]
    assert "presentation_date" not in o["metadata"]
    assert len(o["normalization_findings"])==2


def test_enrichment_canonicalizes_types_and_keeps_base_field_order():
    d=occurrence("patents")
    product=normalize_measured_product([row()])
    enriched=enrich_graph_work(d,[product],[],enriched_at=2,target_entity="patents")
    assert len([t for t in enriched["types"] if t["source"]=="impactu"])==1
    assert list(k for k in enriched if k not in ("_id","source_metadata"))==list(empty_kahi_patent())
    project=occurrence()
    assert list(k for k in project if k not in ("_id","source_metadata"))==list(empty_kahi_project())


def test_publication_quality_check_rejects_stale_bad_metadata():
    d=occurrence()
    d.update(date_end=4220658000,year_end=None)
    assert "inconsistent_actual_date" in product_quality_findings(d,"projects")
    assert "unusable_title" in product_quality_findings({"titles":[{"title":"Prueba"}]},"patents")
    d=occurrence("patents")
    d["titles"][0]["lang"]="es"
    assert "unsupported_title_language" in product_quality_findings(d,"patents")


def test_publication_guard_keeps_previous_current_when_metadata_is_invalid():
    from yuku.scienti_release import ScientiFinalReleaseManager
    db=mongomock.MongoClient().dam
    collections={e:'output_'+e for e in ('works','projects','patents','events')}
    runs={e:'run_'+e for e in collections}
    for e,col in collections.items():
        d=occurrence(e) if e in {'projects','patents'} else {'_id':e,'types':[]}
        if e=='projects':d.update(date_end=4220658000,year_end=None)
        db[col].insert_one(d)
        db.minciencias_measurement_runs.insert_one({'_id':runs[e],'status':'complete',
            'summary':{'target_entity':e,'collection':col,'documents':1}})
    db.scienti_final_release_publications.insert_one({'_id':'current','current_release':'previous'})
    with pytest.raises(RuntimeError,match='final release audit failed'):
        ScientiFinalReleaseManager(db).publish(release_name='bad_release',collections=collections,materialization_runs=runs)
    assert db.scienti_final_release_publications.find_one({'_id':'current'})['current_release']=='previous'


def test_graph_refuses_stale_normalizer_before_building():
    db=mongomock.MongoClient().dam
    db.source.insert_one(occurrence('patents'))
    db[ENTITY_RUNS].insert_one({'_id':'old','status':'complete','config':{
        'destinations':{'patents':'source'},'normalizer_version':'scienti-entity-normalizer-v5'}})
    builder=ScientiExactEntityGraphBuilder(db,entity='patents',run_name='fresh_graph',
        source_collection='source',target_collection='output',entity_run_name='old')
    with pytest.raises(RuntimeError,match='older normalizer'):
        builder.run()


def test_publication_projection_preserves_explicit_language_evidence():
    from yuku.scienti_release import ScientiFinalReleaseManager
    db=mongomock.MongoClient().dam
    collections={e:'output_'+e for e in ('works','projects','patents','events')}
    runs={e:'run_'+e for e in collections}
    for e,col in collections.items():
        d=occurrence(e,language='English') if e in {'projects','patents'} else {'_id':e,'types':[]}
        db[col].insert_one(d)
        db.minciencias_measurement_runs.insert_one({'_id':runs[e],'status':'complete',
            'summary':{'target_entity':e,'collection':col,'documents':1}})
    publication=ScientiFinalReleaseManager(db).publish(release_name='valid_release',collections=collections,materialization_runs=runs)
    assert publication['status']=='published'


def test_pipeline_requires_new_session_after_quality_policy_changes(tmp_path):
    import json
    from types import SimpleNamespace
    from yuku.scienti_pipeline import ScientiFullPipeline, load_pipeline_config, PIPELINE_RUNS
    db=mongomock.MongoClient().dam
    file=tmp_path/'pipeline.json'
    file.write_text(json.dumps({'run_name':'quality_rebuild','snapshot_tag':'20261018',
        'convocations':{'researchers_json':str(tmp_path/'people.json'),'groups_json':str(tmp_path/'groups.json')}}))
    config=load_pipeline_config(file)
    facade=SimpleNamespace(db=db)
    first=ScientiFullPipeline(facade,config)
    old=deepcopy(first.config);old.pop('product_quality_version')
    db[PIPELINE_RUNS].insert_one({'_id':first.run_name,'input_config_hash':first.input_config_hash,'config':old})
    with pytest.raises(ValueError,match='quality policy changed'):
        ScientiFullPipeline(facade,config)
