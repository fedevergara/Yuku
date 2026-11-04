import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import mongomock
import pytest

from yuku.scienti_frozen_pipeline import FrozenScientiPipeline
from yuku.scienti_pipeline import load_pipeline_config


def build_pipeline(tmp_path, collections=None, processing=None):
    db = mongomock.MongoClient().dam
    sources = ('raw_cvlac', 'manifest', 'state_cvlac', 'raw_gruplac', 'state_gruplac',
               'all_researchers', 'recognized_researchers', 'recognized_groups',
               'cvlac_data', 'gruplac_groups_data', 'gruplac_production_data')
    for name in sources:
        db[name].insert_one({'_id': 'source', 'value': 'original'})
    db.scienti_cvlac_priority_runs.insert_one({
        '_id': 'frozen_cvlac', 'status': 'complete', 'manifest_sha256': 'proof',
        'config': {'raw_collection': 'raw_cvlac', 'manifest_collection': 'manifest'}})
    db.scienti_final_release_publications.insert_one({'_id': 'current', 'current_release': 'old_release'})
    config = {'run_name': 'frozen_rebuild', 'snapshot_tag': 'frozen_rebuild',
              'publication': {'date': '20261009'},
              'convocations': {'researchers_json': str(tmp_path / 'people.json'),
                              'groups_json': str(tmp_path / 'groups.json')},
              'processing': {'confirm_conflicting_dois': True, **(processing or {})},
              'collections': {'cvlac_raw': 'raw_cvlac', **(collections or {})},
              'frozen_sources': {'source_release': 'old_release', 'cvlac_run': 'frozen_cvlac',
                  'cvlac_raw': 'raw_cvlac', 'cvlac_manifest': 'manifest', 'cvlac_state': 'state_cvlac',
                  'gruplac_raw': 'raw_gruplac', 'gruplac_state': 'state_gruplac',
                  'manifest_sha256': 'proof', 'counts': {name: 1 for name in sources},
                  'retire_finals': ['old_work']}}
    file = tmp_path / 'config.json'
    file.write_text(json.dumps(config))
    facade = MagicMock()
    facade.db = db
    facade.client = None
    return FrozenScientiPipeline(facade, load_pipeline_config(file))


@pytest.fixture
def pipeline(tmp_path):
    return build_pipeline(tmp_path)


IN_PLACE = {'gruplac_raw': 'raw_gruplac', 'gruplac_downloads': 'state_gruplac'}


def test_gruplac_frozen_html_is_read_in_place_without_copies(tmp_path):
    pipeline = build_pipeline(tmp_path, IN_PLACE, {'verify_incomplete_gruplac': False})
    before = set(pipeline.db.list_collection_names())
    result = pipeline._gruplac_download()
    assert result['status'] == 'reused_frozen_source'
    assert result['raw']['collection'] == 'raw_gruplac'
    assert set(pipeline.db.list_collection_names()) == before
    assert pipeline.db.scienti_rebuild_source_copies.count_documents({}) == 0


def test_gruplac_frozen_html_in_place_refuses_portal_verification(tmp_path):
    pipeline = build_pipeline(tmp_path, IN_PLACE)
    with pytest.raises(RuntimeError, match='read-only'):
        pipeline._gruplac_download()
    assert pipeline.db.raw_gruplac.find_one({'_id': 'source'})['value'] == 'original'


def test_preflight_rejects_changed_frozen_manifest(pipeline):
    pipeline.db.scienti_cvlac_priority_runs.update_one(
        {'_id': 'frozen_cvlac'}, {'$set': {'manifest_sha256': 'other'}})
    with pytest.raises(RuntimeError, match='provenance'):
        pipeline.preflight()


def test_preflight_rejects_changed_sources(pipeline):
    pipeline.db.raw_cvlac.insert_one({'_id': 'unexpected'})
    with pytest.raises(RuntimeError, match='count changed'):
        pipeline.preflight()


def test_preflight_rejects_concurrent_promotion(pipeline):
    pipeline.db.scienti_final_release_publications.update_one(
        {'_id': 'current'}, {'$set': {'current_release': 'other'}})
    with pytest.raises(RuntimeError, match='Another release'):
        pipeline.preflight()


def test_reused_acquisition_calls_no_downloader(pipeline):
    pipeline.preflight()
    pipeline._open_data('researchers')
    pipeline._open_data('groups')
    pipeline._open_data('production')
    pipeline._convocations()
    pipeline._directory()
    pipeline._resolve_researchers()
    pipeline._resolve_groups()
    pipeline._cvlac_download()
    assert not pipeline.yuku.mock_calls


def test_all_three_consumers_use_the_proven_snapshot(pipeline):
    pipeline._cvlac_normalize()
    assert pipeline.yuku.normalize_scienti_cvlac_snapshot.call_args.kwargs['source_snapshot_run_name'] == 'frozen_cvlac'
    pipeline._base_graph()
    assert pipeline.yuku.create_scienti_full_graph.call_args.kwargs['run_name'] == 'frozen_cvlac'
    assert pipeline.yuku.create_scienti_full_graph.call_args.kwargs['publish_pointer'] is False
    pipeline._persons_final()
    args = pipeline.yuku.materialize_scienti_persons.call_args.kwargs
    assert args['manifest_run_name'] == 'frozen_cvlac'
    assert args['manifest_collection'] == 'manifest'
    assert args['publish_pointer'] is False
    assert args['cvlac_collection'] == pipeline.names['cvlac_reconciled']


def test_cleanup_rejects_original_html_even_if_requested(pipeline):
    pipeline.frozen['retire_finals'].append('raw_cvlac')
    with pytest.raises(RuntimeError, match='preserved source'):
        pipeline._cleanup()
    assert pipeline.db.raw_cvlac.find_one({'_id': 'source'})['value'] == 'original'
    assert pipeline.db.scienti_final_release_publications.find_one({'_id': 'current'})['current_release'] == 'old_release'


def test_cleanup_rejects_concurrent_promotion(pipeline):
    pipeline.db.scienti_final_release_publications.update_one(
        {'_id': 'current'}, {'$set': {'current_release': 'other'}})
    with pytest.raises(RuntimeError, match='Another release'):
        pipeline._cleanup()


@pytest.mark.skipif(not os.getenv('YUKU_IDENTIFIED_TEST_URI'), reason='Opt-in isolated Mongo integration')
def test_gruplac_copy_keeps_original_and_can_resume(pipeline):
    from pymongo import MongoClient
    client = MongoClient(os.environ['YUKU_IDENTIFIED_TEST_URI'], serverSelectionTimeoutMS=5000)
    db = client['yuku_frozen_' + uuid4().hex]
    try:
        pipeline.db = db
        db.raw_gruplac.insert_one({'_id': 'source', 'html': 'original'})
        db.state_gruplac.insert_one({'_id': 'source', 'status': 'incomplete'})
        first = pipeline._gruplac_download()
        assert first['raw']['documents'] == 1
        assert db[pipeline.names['gruplac_raw']].find_one({'_id': 'source'})['html'] == 'original'
        db[pipeline.names['gruplac_raw']].update_one({'_id': 'source'}, {'$set': {'html': 'verified'}})
        pipeline._gruplac_download()
        assert db.raw_gruplac.find_one({'_id': 'source'})['html'] == 'original'
        assert db[pipeline.names['gruplac_raw']].find_one({'_id': 'source'})['html'] == 'verified'
        with pytest.raises(RuntimeError, match='another source'):
            pipeline._copy_source('state_gruplac', pipeline.names['gruplac_raw'])
    finally:
        client.drop_database(db.name)
        client.close()
