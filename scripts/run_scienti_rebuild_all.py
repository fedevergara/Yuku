#!/usr/bin/env python3
"""Back up, rebuild and publish all six ScienTI entities from frozen sources."""

import argparse
from datetime import datetime
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import traceback
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def now():
    return datetime.now(ZoneInfo('America/Bogota')).isoformat(timespec='seconds')


def save(path, value):
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')
    temp.replace(path)


def snapshot_code(directory):
    import kahi_impactu_type_catalog
    destination = directory / 'code'
    if destination.exists():
        return destination
    destination.mkdir()
    for name in ('yuku', 'tests', 'scripts', 'docs', 'config', 'bin'):
        if (ROOT / name).is_dir():
            shutil.copytree(ROOT / name, destination / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in ('README.md', 'setup.py', 'pytest.ini', 'setup.cfg'):
        if (ROOT / name).is_file():
            shutil.copy2(ROOT / name, destination / name)
    # Freeze the editorial authority together with Yuku: a later catalog edit
    # must not change the rules of an already-running reconstruction.
    shutil.copytree(Path(kahi_impactu_type_catalog.__file__).resolve().parent,
                    destination / 'kahi_impactu_type_catalog',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    hashes = {str(p.relative_to(destination)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in destination.rglob('*') if p.is_file()}
    save(directory / 'code.sha256.json', hashes)
    return destination


def backup(db, directory, expected, uri, status, update):
    proof = directory / 'backup.result.json'
    if proof.exists():
        return json.loads(proof.read_text())
    update('running', 'backup')
    available = set(db.list_collection_names())
    included = set(expected)
    included.update(n for n in available if n.endswith(('_runs', '_audits', '_publications', '_graph_review')))
    included.update({'scienti_doi_identity_checks'} & available)
    included.update({'minciencias_doi_resolution_history'} & available)
    counts_before = {n: db[n].count_documents({}) for n in included}
    for name, count in expected.items():
        if counts_before.get(name) != count:
            raise RuntimeError(f'Final collection changed before backup: {name}')
    command = ['mongodump', '--uri', uri, '--db', db.name, '--gzip',
               '--numParallelCollections', '2', '--out', str(directory / 'dump')]
    for name in sorted(available - included):
        command.extend(['--excludeCollection', name])
    with (directory / 'backup.log').open('a') as output:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        (directory / 'backup.pid').write_text(str(process.pid) + '\n')
        status['backup_pid'] = process.pid
        update('running', 'backup')
        result = process.wait()
    if result:
        raise RuntimeError(f'mongodump failed with exit code {result}; reconstruction not started')
    counts_after = {n: db[n].count_documents({}) for n in included}
    if counts_after != counts_before:
        raise RuntimeError('Source collections changed during backup; reconstruction not started')
    files = []
    for name in sorted(included):
        path = directory / 'dump' / db.name / (name + '.bson.gz')
        meta = directory / 'dump' / db.name / (name + '.metadata.json.gz')
        if not path.is_file() or not meta.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f'Backup files missing for {name}')
        files.append({'collection': name, 'documents': counts_before[name],
                      'bson_gzip_bytes': path.stat().st_size})
    report = {'status': 'complete', 'exit_code': result, 'finished_at': now(),
              'path': str(directory / 'dump'), 'collections': files,
              'counts_unchanged': True, 'restoration_tested': False}
    save(proof, report)
    return report


def worker(args, directory):
    lock = os.fdopen(int(os.environ.pop('YUKU_REBUILD_ALL_LOCK_FD')), 'a')
    code = directory / 'code'
    sys.path.insert(0, str(code))
    os.environ['PYTHONPATH'] = str(code)
    from pymongo import MongoClient
    from yuku.Yuku import Yuku
    from yuku.scienti_frozen_pipeline import FrozenScientiPipeline
    from yuku.scienti_pipeline import load_pipeline_config
    from yuku.scienti_doi_history import DOIResolutionHistory

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    state = {'status': 'running', 'pid': os.getpid(), 'started_at': now(), 'run_name': args.run_name}

    def update(status, phase):
        state.update(status=status, phase=phase, updated_at=now())
        save(directory / 'status.json', state)
        logging.info('status=%s phase=%s', status, phase)

    uri = os.environ.get('YUKU_MONGO_URI', 'mongodb://localhost:27017/')
    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    try:
        update('running', 'verification')
        verification = directory / 'verification.result.json'
        if not verification.exists():
            tests = subprocess.run([sys.executable, '-m', 'pytest', '-q', '--disable-warnings',
                                    '--junitxml=' + str(directory / 'tests.xml'), str(code / 'tests')],
                                   cwd=code, stdin=subprocess.DEVNULL,
                                   env={**os.environ, 'YUKU_IDENTIFIED_TEST_URI': uri})
            if tests.returncode:
                raise RuntimeError('Verification suite failed; no backup or reconstruction started')
            save(verification, {'status': 'passed', 'exit_code': tests.returncode, 'finished_at': now()})
        db = client[args.database]
        config = load_pipeline_config(directory / 'pipeline.json')
        # Import only the pinned source snapshot, never live repository modules.
        yuku = Yuku(mongo_db=args.database, mongodb_uri=uri)
        pipeline = FrozenScientiPipeline(yuku, config, progress=update)
        preflight = pipeline.preflight()
        save(directory / 'preflight.result.json', preflight)
        expected = config['frozen_sources']['retire_counts']
        backup_report = backup(db, directory, expected, uri, state, update)
        update('running', 'doi_history_migration')
        history = DOIResolutionHistory(db, 'scienti_doi_identity_checks')
        migrated = history.bootstrap()
        save(directory / 'history.result.json', {'status': 'complete', 'migrated': migrated,
                                               'metrics': dict(history.metrics), 'http_requests': 0})
        update('running', 'reconstruction')
        result = pipeline.run()
        if result.get('status') != 'complete':
            raise RuntimeError('Full pipeline did not complete')
        current = db.scienti_final_release_publications.find_one({'_id': 'current'}) or {}
        filtered = db.scienti_identified_work_publications.find_one({'_id': 'current'}) or {}
        available = set(db.list_collection_names())
        obsolete = set(config['frozen_sources']['retire_finals'])
        if obsolete & available:
            raise RuntimeError('Some superseded final collections were not retired')
        counts = {role: db[name].count_documents({}) for role, name in current['collections'].items()}
        counts['works_filtered'] = db[filtered['collection']].count_documents({})
        report = {'status': 'complete', 'database': args.database, 'finished_at': now(),
                  'run_name': args.run_name, 'current': current, 'filtered': filtered,
                  'counts': counts, 'retired_final_collections': sorted(obsolete),
                  'preserved_original_sources': list(config['frozen_sources']['counts']),
                  'doi_history': {'collection': 'minciencias_doi_resolution_history',
                                 'documents': db.minciencias_doi_resolution_history.count_documents({})},
                  'backup': backup_report, 'pipeline': result, 'kahi_databases_modified': False}
        save(directory / 'report.json', report)
        state['report'] = str(directory / 'report.json')
        update('complete', 'published_and_retired')
        return 0
    except Exception as error:
        traceback.print_exc()
        state['error'] = f'{type(error).__name__}: {error}'
        update('failed', state.get('phase', 'initialization'))
        return 1
    finally:
        client.close()
        lock.close()


def prepare(args, directory):
    sys.path.insert(0, str(ROOT))
    from pymongo import MongoClient
    from yuku.minciencias_publication import resolve_outputs, parse_name
    path = directory / 'pipeline.json'
    if path.exists():
        config = json.loads(path.read_text())
        if config['run_name'] != args.run_name:
            raise RuntimeError('Output directory belongs to another run')
        return
    client = MongoClient(os.environ.get('YUKU_MONGO_URI', 'mongodb://localhost:27017/'), serverSelectionTimeoutMS=10000)
    try:
        db = client[args.database]
        current = db.scienti_final_release_publications.find_one({'_id': 'current'}) or {}
        if current.get('current_release') != args.source_release:
            raise RuntimeError('The specified source release is not current')
        source = db.scienti_final_release_publications.find_one({'_id': args.source_release}) or {}
        person = db.scienti_person_materialization_runs.find_one(
            {'_id': source['materialization_runs']['persons']}) or {}
        p = person['config']
        snapshot = db.scienti_cvlac_priority_runs.find_one({'_id': p['manifest_run_name']}) or {}
        raw = snapshot['config']
        group_audit = db.scienti_gruplac_normalization_audits.find_one({'_id': p['gruplac_audit_name']}) or {}
        gr = group_audit['config']
        frozen = {'source_release': args.source_release, 'cvlac_run': p['manifest_run_name'],
                  'cvlac_raw': raw['raw_collection'], 'cvlac_manifest': raw['manifest_collection'],
                  'cvlac_state': raw['state_collection'], 'manifest_sha256': snapshot['manifest_sha256'],
                  'gruplac_raw': gr['raw_collection'], 'gruplac_state': gr['state_collection']}
        sources = [frozen[k] for k in ('cvlac_raw', 'cvlac_manifest', 'cvlac_state', 'gruplac_raw', 'gruplac_state')]
        sources += [p[k] for k in ('directory_collection', 'recognized_collection', 'open_data_collection')]
        sources += ['recognized_groups', 'gruplac_groups_data', 'gruplac_production_data']
        frozen['counts'] = {n: db[n].count_documents({}) for n in dict.fromkeys(sources)}
        old = set(source['collections'].values())
        filtered = db.scienti_identified_work_publications.find_one({'_id': 'current'}) or {}
        old.add(filtered['collection'])
        # Include only identity finals proved by their publication registry.
        for publication in db.scienti_person_publications.find({'status': 'published'}):
            if publication.get('collection') in db.list_collection_names():
                old.add(publication['collection'])
        frozen['retire_finals'] = sorted(old)
        frozen['retire_counts'] = {n: db[n].count_documents({}) for n in sorted(old)}
        destinations = resolve_outputs(db, date=args.publication_date)
        versions = {entity: parse_name(name)['version'] for entity, name in destinations.items()}
        config = {'run_name': args.run_name, 'snapshot_tag': args.run_name,
                  'publication': {'date': args.publication_date, 'versions': versions},
                  'convocations': {'mode': 'json',
                      'researchers_json': '/home/fvergara/projects/colav/data/minciencias/investigadores.json',
                      'groups_json': '/home/fvergara/projects/colav/data/minciencias/grupos.json'},
                  'processing': {'confirm_conflicting_dois': True, 'generate_identified_works': True,
                                 'cleanup_transient': True, 'normalization_batch_size': 50,
                                 'graph_batch_size': 500, 'graph_partitions': 64,
                                 'measurement_batch_size': 2000, 'entity_batch_size': 2000,
                                 'progress_every': 10000, 'doi_request_delay': 1.0,
                                 'cvlac_workers': 8, 'gruplac_workers': 8,
                                 # Frozen rebuilds never query the portal.
                                 'verify_incomplete_gruplac': False},
                  'collections': {'all_researchers': p['directory_collection'],
                                  'recognized_researchers': p['recognized_collection'],
                                  'recognized_groups': 'recognized_groups',
                                  'cvlac_raw': frozen['cvlac_raw'],
                                  # Read the frozen GrupLAC HTML in place, without copies.
                                  'gruplac_raw': frozen['gruplac_raw'],
                                  'gruplac_downloads': frozen['gruplac_state']},
                  'frozen_sources': frozen}
        save(path, config)
        save(directory / 'planned.outputs.json', {**destinations, 'works_filtered': destinations['works'] + '_filtered'})
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default='dam')
    parser.add_argument('--source-release', required=True)
    parser.add_argument('--run-name', required=True)
    parser.add_argument('--publication-date', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    directory = args.output_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if args.worker:
        return worker(args, directory)
    lock = (directory / 'job.lock').open('a')
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        prepare(args, directory)
        snapshot_code(directory)
        if args.prepare_only:
            print(json.dumps({'status': 'prepared', 'config': str(directory / 'pipeline.json')}))
            return 0
        save(directory / 'status.json', {'status': 'starting', 'run_name': args.run_name, 'started_at': now()})
        argv = [v for v in sys.argv[1:] if v != '--prepare-only']
        with (directory / 'job.log').open('a') as output:
            process = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve()), *argv, '--worker'],
                                       stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                       start_new_session=True, pass_fds=(lock.fileno(),),
                                       env={**os.environ, 'YUKU_REBUILD_ALL_LOCK_FD': str(lock.fileno())})
        (directory / 'job.pid').write_text(str(process.pid) + '\n')
        print(json.dumps({'pid': process.pid, 'log': str(directory / 'job.log'),
                          'status': str(directory / 'status.json'), 'report': str(directory / 'report.json')}))
        return 0
    finally:
        lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
