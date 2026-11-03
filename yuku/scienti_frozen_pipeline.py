"""Rebuild derived ScienTI entities while retaining the original HTML snapshot."""

from pymongo import ReplaceOne

from yuku.scienti_pipeline import ScientiFullPipeline, STAGES
from yuku.scienti_release import ScientiFinalReleaseManager
from yuku.minciencias_publication import (
    EntityRetention, adopt_published_history, promote_release,
)


class FrozenScientiPipeline(ScientiFullPipeline):
    """Reuse proven acquisition inputs; execute every derived stage afresh."""

    def __init__(self, yuku, config, progress=None):
        super().__init__(yuku, config)
        self.frozen = self.config['frozen_sources']
        self.progress = progress or (lambda *args: None)

    def _source(self, name):
        if name not in self.db.list_collection_names():
            raise RuntimeError(f'Frozen source is missing: {name}')
        count = self.db[name].count_documents({})
        expected = self.frozen['counts'][name]
        if count != expected:
            raise RuntimeError(f'Frozen source count changed: {name}: {count}/{expected}')
        return {'status': 'reused_frozen_source', 'collection': name, 'documents': count}

    def preflight(self):
        snapshot = self.db.scienti_cvlac_priority_runs.find_one(
            {'_id': self.frozen['cvlac_run']}) or {}
        if snapshot.get('status') != 'complete':
            raise RuntimeError('CvLAC frozen snapshot is not complete')
        config = snapshot.get('config', {})
        if (config.get('raw_collection') != self.frozen['cvlac_raw']
                or config.get('manifest_collection') != self.frozen['cvlac_manifest']
                or snapshot.get('manifest_sha256') != self.frozen['manifest_sha256']):
            raise RuntimeError('CvLAC frozen snapshot provenance changed')
        current = self.db.scienti_final_release_publications.find_one({'_id': 'current'}) or {}
        allowed = {self.frozen['source_release'], f"release_{self.config['snapshot_tag']}_kahi"}
        if current.get('current_release') not in allowed:
            raise RuntimeError('Another release became current; refuse to replace it')
        return {'run_name': self.run_name, 'sources': [self._source(n) for n in self.frozen['counts']],
                'source_snapshot': self.frozen['cvlac_run'], 'collections': self.names,
                'stages': list(STAGES)}

    def _run_stage(self, name, action):
        self.progress('running', name)
        result = super()._run_stage(name, action)
        self.progress('running', name + ':complete')
        return result

    def _open_data(self, key):
        return self._source(self.config['open_data']['datasets'][key]['data_collection'])

    def _convocations(self):
        return {'status': 'reused_resolved_convocations',
                'researchers': self._source(self.names['recognized_researchers']),
                'groups': self._source(self.names['recognized_groups'])}

    def _directory(self):
        return self._source(self.names['all_researchers'])

    def _resolve_researchers(self):
        return self._source(self.names['recognized_researchers'])

    def _resolve_groups(self):
        return self._source(self.names['recognized_groups'])

    def _copy_source(self, source, destination):
        """Copy bounded batches; retries safely replace the same source IDs."""
        evidence = self._source(source)
        controls = self.db.scienti_rebuild_source_copies
        previous = controls.find_one({'_id': destination}) or {}
        if previous and previous.get('source') != source:
            raise RuntimeError('Source-copy destination belongs to another source')
        if previous.get('status') == 'complete':
            if self.db[destination].count_documents({}) != evidence['documents']:
                raise RuntimeError('Completed source copy changed its document count')
            return previous
        if not previous and destination in self.db.list_collection_names():
            raise RuntimeError('Source-copy destination exists without ownership proof')
        controls.update_one({'_id': destination}, {'$set': {
            'source': source, 'run_name': self.run_name, 'status': 'copying'}}, upsert=True)
        batch = []
        for row in self.db[source].find({}, batch_size=100):
            batch.append(ReplaceOne({'_id': row['_id']}, row, upsert=True))
            if len(batch) >= 100:
                self.db[destination].bulk_write(batch, ordered=False)
                batch.clear()
        if batch:
            self.db[destination].bulk_write(batch, ordered=False)
        if self.db[destination].count_documents({}) != evidence['documents']:
            raise RuntimeError('Frozen source copy is incomplete')
        result = {'_id': destination, 'source': source, 'run_name': self.run_name,
                  'status': 'complete', 'documents': evidence['documents']}
        controls.replace_one({'_id': destination}, result)
        return result

    def _gruplac_download(self):
        raw, state = self.frozen['gruplac_raw'], self.frozen['gruplac_state']
        if (self.names['gruplac_raw'], self.names['gruplac_downloads']) == (raw, state):
            # Read the proven HTML in place: no copy, no portal refresh.
            if self.config['processing'].get('verify_incomplete_gruplac', True):
                raise RuntimeError('Frozen GrupLAC sources are read-only; '
                                   'set processing.verify_incomplete_gruplac to false')
            return {'status': 'reused_frozen_source',
                    'raw': self._source(raw), 'state': self._source(state)}
        # Legacy sessions verify incomplete groups against the portal, which can
        # rewrite their inputs; they keep working on copies of the originals.
        return {'status': 'copied_frozen_source',
                'raw': self._copy_source(self.frozen['gruplac_raw'], self.names['gruplac_raw']),
                'state': self._copy_source(self.frozen['gruplac_state'], self.names['gruplac_downloads'])}

    def _cvlac_download(self):
        return {'status': 'reused_frozen_snapshot', 'run_name': self.frozen['cvlac_run'],
                'raw': self._source(self.frozen['cvlac_raw']),
                'manifest': self._source(self.frozen['cvlac_manifest'])}

    def _cvlac_normalize(self):
        processing = self.config['processing']
        return self.yuku.normalize_scienti_cvlac_snapshot(
            run_name=f'{self.run_name}_cvlac_normalize',
            source_snapshot_run_name=self.frozen['cvlac_run'],
            destination_collection=self.names['cvlac_normalized'],
            source_state_collection=self.frozen['cvlac_state'],
            batch_size=int(processing['normalization_batch_size']),
            progress_every=10000, workers=int(processing['cvlac_workers']))

    def _base_graph(self):
        processing = self.config['processing']
        return self.yuku.create_scienti_full_graph(
            run_name=self.frozen['cvlac_run'], graph_run_name=f'{self.run_name}_base_graph',
            collection=self.names['base_graph'],
            cvlac_related_collection=self._normalized_source('cvlac'),
            cvlac_audit_name=self._normalized_audit('cvlac'),
            gruplac_raw_collection=self.names['gruplac_raw'],
            gruplac_related_collection=self._normalized_source('gruplac'),
            gruplac_audit_name=self._normalized_audit('gruplac'),
            recognized_groups_collection=self.names['recognized_groups'],
            batch_size=int(processing['graph_batch_size']),
            candidate_partitions=int(processing['graph_partitions']), publish_pointer=False)

    def _persons_final(self):
        tag = self.config['snapshot_tag']
        return self.yuku.materialize_scienti_persons(
            run_name=f'{self.run_name}_persons_final', manifest_run_name=self.frozen['cvlac_run'],
            manifest_collection=self.frozen['cvlac_manifest'],
            directory_collection=self.names['all_researchers'],
            recognized_collection=self.names['recognized_researchers'],
            open_data_collection=self.config['open_data']['datasets']['researchers']['data_collection'],
            cvlac_collection=self._normalized_source('cvlac'), cvlac_audit_name=self._normalized_audit('cvlac'),
            gruplac_collection=self._normalized_source('gruplac'), gruplac_audit_name=self._normalized_audit('gruplac'),
            affiliation_run_name=f'{self.run_name}_affiliations_final',
            affiliation_collection=self.names['affiliations_final'],
            final_release_name=f'release_{tag}', final_release_audit_name=f'release_{tag}_audit',
            target_collection=self.names['persons_final'], publish_pointer=False)

    def _cleanup(self):
        release_name = f"release_{self.config['snapshot_tag']}_kahi"
        release = self.db.scienti_final_release_publications.find_one({'_id': release_name}) or {}
        current = self.db.scienti_final_release_publications.find_one({'_id': 'current'}) or {}
        if current.get('current_release') not in {self.frozen['source_release'], release_name}:
            raise RuntimeError('Another release became current before publication')
        finals = set(release.get('collections', {}).values()) | {self.names['identified_works']}
        protected = set(self.frozen['counts']) | finals | {'minciencias_doi_resolution_history'}
        obsolete = set(self.frozen['retire_finals'])
        intermediates = {self.names[k] for k in (
            'base_graph', 'projects_graph', 'patents_graph', 'events_snapshot', 'measured_products',
            'measurement_links', 'projects_measurement_links', 'patents_measurement_links',
            'events_measurement_links')}
        if (obsolete | intermediates) & protected:
            raise RuntimeError('Cleanup includes a preserved source or new final collection')
        with EntityRetention(self.db).lock():
            adopt_published_history(self.db)
            pointer = promote_release(self.db, release, identified=self.names['identified_works'])
            cleanup = ScientiFinalReleaseManager(self.db).cleanup(
                cleanup_name=f'{self.run_name}_retire', release_name=release_name,
                candidates=sorted(obsolete | intermediates), protected=sorted(protected),
                reset_entity_publication=False)
        return {'status': 'complete', 'current': pointer, 'cleanup': cleanup}
