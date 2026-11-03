#!/usr/bin/env python3
"""Detach verified journal extraction, DOI confirmation and a new work release."""

import argparse
from datetime import datetime, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]


def save(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
    temporary.replace(path)


def refresh_type_sources(db, run_name, sources, audits, phase, batch_size):
    """Reparse old HTML snapshots before reconciliation can reuse their rows."""
    from yuku.scienti_normalization import (
        CVLAC_AUDIT_RUNS, CVLAC_NORMALIZATION_RUNS, CVLAC_PARSER_VERSION,
        GRUPLAC_AUDIT_RUNS, GRUPLAC_PARSER_VERSION, CvlacNormalizationRun,
        CvlacNormalizationAuditor, GruplacNormalizationRun, GruplacNormalizationAuditor,
    )
    sources, audits = dict(sources), dict(audits)
    for kind, namespace, expected in (("cvlac", CVLAC_AUDIT_RUNS, CVLAC_PARSER_VERSION),
                                      ("gruplac", GRUPLAC_AUDIT_RUNS, GRUPLAC_PARSER_VERSION)):
        audit = db[namespace].find_one({"_id": audits[kind]}) or {}
        config = audit.get("config", {})
        if audit.get("status") != "passed":
            raise RuntimeError("re-extraction requires passed source audits")
        version = config.get("parser_version" if kind == "cvlac" else "expected_parser_version")
        if version == expected:
            continue
        normalization = run_name + "_" + kind + "_types"
        target = kind + "_types_" + run_name
        proof_name = normalization + "_audit"
        if kind == "cvlac":
            previous = db[CVLAC_NORMALIZATION_RUNS].find_one({"_id": config.get("normalization_run_name")}) or {}
            snapshot = previous.get("config", {}).get("source_snapshot_run_name")
            if not snapshot:
                sample = db[sources[kind]].find_one({}, {"source.snapshot_run_name": 1}) or {}
                snapshot = sample.get("source", {}).get("snapshot_run_name")
            if not snapshot:
                raise RuntimeError("CVLAC source snapshot proof is missing; cannot repair parsed titles")
            phase(kind + "_types_normalize", lambda: CvlacNormalizationRun(db,
                run_name=normalization, source_snapshot_run_name=snapshot,
                source_state_collection=config.get("source_state_collection"),
                destination_collection=target, batch_size=batch_size).run())
            proof = phase(kind + "_types_audit", lambda: CvlacNormalizationAuditor(db,
                audit_name=proof_name, normalization_run_name=normalization).run())
        else:
            phase(kind + "_types_normalize", lambda: GruplacNormalizationRun(db,
                run_name=normalization, source_raw_collection=config["raw_collection"],
                source_state_collection=config["state_collection"], destination_collection=target).run())
            proof = phase(kind + "_types_audit", lambda: GruplacNormalizationAuditor(db,
                audit_name=proof_name, recognized_groups_collection=config["recognized_groups_collection"],
                raw_collection=config["raw_collection"], state_collection=config["state_collection"],
                normalized_collection=target, verification_run_name=config.get("verification_run_name")).run())
        if proof.get("status") != "passed":
            raise RuntimeError("corrected extraction audit failed")
        sources[kind], audits[kind] = target, proof_name
    return sources, audits


def worker(args, directory):
    lock = os.fdopen(int(os.environ.pop("YUKU_JOURNAL_DOI_LOCK_FD")), "a")
    sys.path.insert(0, str(ROOT))
    from pymongo import MongoClient
    from yuku.cvlac_work_graph import CheckpointedNormalizedWorkGraphBuilder, validate_scienti_graph_gate
    from yuku.minciencias_measurements import MincienciasMeasurementPipeline
    from yuku.scienti_bibliographic_audit import ScientiBibliographicEnrichmentAuditor
    from yuku.scienti_doi_reconciliation import ScientiDOIReconciler
    from yuku.scienti_identified_works import ScientiIdentifiedWorkSelector
    from yuku.scienti_persons import ScientiPersonMaterializer
    from yuku.scienti_release import ScientiFinalReleaseManager
    from yuku.scienti_work_outputs import ScientiWorkOutputs
    from yuku.minciencias_publication import publication_date, resolve_target

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger(__name__)
    status = {"status": "running", "phase": "verification", "pid": os.getpid(),
              "started_at": datetime.now(timezone.utc).isoformat(), "run_name": args.run_name}
    results = {}
    uri = os.environ.get("YUKU_MONGO_URI", "mongodb://localhost:27017/")
    client = MongoClient(uri, serverSelectionTimeoutMS=10000)

    def phase(name, function):
        status.update(phase=name)
        save(directory / "status.json", status)
        log.info("Starting phase %s", name)
        result = function()
        results[name] = result
        save(directory / (name + ".result.json"), result)
        log.info("Completed phase %s", name)
        return result

    def finish(db, names):
        outputs = phase("work_outputs", lambda: ScientiWorkOutputs(db).finalize(
            cleanup_name=args.run_name + "_work_outputs", source_release=args.source_release,
            release_name=names["release"], identified=names["identified"],
            intermediates=[names[key] for key in ("base_graph", "measured", "links")]))
        stage = results["reconciliation"]
        count, filtered = results["identified_count"], results["identified_materialize"]
        report = {"status": "complete", "database": args.database,
                  "source_release": args.source_release, "release": names["release"],
                  "collection": args.target, "reconciliation": stage,
                  "source_works": count["stats"]["processed"], "selected_works": filtered["stats"]["selected"],
                  "fields_populated": filtered["stats"]["fields_populated"],
                  "decision_counts": stage["decision_counts"], "current_pointers_changed": True,
                  "work_collections": outputs["retained_work_collections"],
                  "cleanup": outputs["cleanup"], "registry_enrichment": False}
        save(directory / "report.json", report)
        status.update(status="complete", phase="published", report=str(directory / "report.json"))
        log.info("Published work pair %s; %s identified works", report["work_collections"], report["selected_works"])
        return 0

    try:
        save(directory / "status.json", status)
        verification = directory / "verification"
        verification.mkdir(exist_ok=True)
        # The verification and all dependent work run inside the detached
        # controller. A failed suite stops publication before reading inputs.
        test = subprocess.run([sys.executable, "-m", "pytest", "-q", "--disable-warnings",
                               "--junitxml=" + str(verification / "tests.xml")],
                              cwd=ROOT, stdin=subprocess.DEVNULL,
                              env={**os.environ, "YUKU_IDENTIFIED_TEST_URI": uri})
        save(verification / "tests.status.json", {"exit_code": test.returncode})
        if test.returncode:
            raise RuntimeError("verification suite failed; reconstruction was not started")
        db = client[args.database]
        # After selection succeeds, only promote/cleanup/report remain. Resume
        # them without trying to reopen an intentionally retired source graph.
        if (directory / "identified_materialize.result.json").exists():
            pinned = json.loads((directory / "pinned-inputs.json").read_text())
            if pinned["source_release"]["_id"] != args.source_release or (args.target and pinned["targets"]["identified"] != args.target):
                raise RuntimeError("completed selection belongs to another configuration")
            args.target = pinned["targets"]["identified"]
            for name in ("reconciliation", "identified_count", "identified_materialize"):
                results[name] = json.loads((directory / (name + ".result.json")).read_text())
            return finish(db, pinned["targets"])
        manager = ScientiFinalReleaseManager(db)
        source_release = db.scienti_final_release_publications.find_one({"_id": args.source_release}) or {}
        if source_release.get("status") != "published" or source_release.get("entity_count") != 6:
            raise RuntimeError("source must be an explicit published six-entity release")
        base = phase("preflight", lambda: manager._core_release(source_release["base_release_name"]))
        source_person_run = source_release["materialization_runs"]["persons"]
        source_person = db.scienti_person_materialization_runs.find_one({"_id": source_person_run}) or {}
        person_config = source_person.get("config", {})
        if source_person.get("status") != "complete":
            raise RuntimeError("source person materialization is not complete")
        kinds = ("cvlac", "gruplac")
        sources = {kind: person_config[kind + "_collection"] for kind in kinds}
        audits = {kind: person_config[kind + "_audit_name"] for kind in kinds}
        targets = {kind: kind + "_related_works_" + args.run_name for kind in kinds}
        reconciliation = args.run_name + "_reconcile"
        # Pin inputs for resume. Current pointers change only after both new
        # work outputs pass; obsolete work datasets are then retired.
        names = {"base_graph": "scienti_works_base_" + args.run_name,
                 "measured": "minciencias_measured_products_" + args.run_name,
                 "links": "minciencias_measured_product_links_" + args.run_name,
                 "core_release": "scienti_final_release_" + args.run_name,
                 "release": "scienti_kahi_release_" + args.run_name,
                 "identified": args.target}
        pinned_path = directory / "pinned-inputs.json"
        if pinned_path.exists():
            pinned = json.loads(pinned_path.read_text())
            if pinned["source_release"]["_id"] != args.source_release:
                raise RuntimeError("pinned source release differs")
            names = pinned["targets"]
        else:
            date = publication_date(args.publication_date)
            names["works"] = resolve_target(db, "works", version=args.works_version, date=date)
            names["persons"] = resolve_target(db, "person", version=args.person_version, date=date)
            names["identified"] = names["works"] + "_filtered"
        if args.target and args.target != names["identified"]:
            raise RuntimeError("filtered target must match the full works version and date")
        args.target = names["identified"]
        save(directory / "pinned-inputs.json", {"source_release": source_release,
            "normalization_sources": sources, "normalization_audits": audits,
            "targets": names, "confirm_conflicting_dois": args.confirm_conflicting_dois})
        sources, audits = refresh_type_sources(db, args.run_name, sources, audits, phase, args.batch_size)
        stage = phase("reconciliation", lambda: ScientiDOIReconciler(
            db, run_name=reconciliation, sources=sources, targets=targets, audits=audits,
            confirm_doi=args.confirm_conflicting_dois, batch_size=args.batch_size,
            request_delay=args.request_delay).run())
        view_audits = {kind: reconciliation + "_" + kind + "_audit" for kind in kinds}
        gr_audit = db.scienti_gruplac_normalization_audits.find_one({"_id": audits["gruplac"]})
        snapshot = person_config["manifest_run_name"]
        gate = phase("graph_gate", lambda: validate_scienti_graph_gate(db,
            snapshot_run_name=snapshot, cvlac_normalized_collection=targets["cvlac"],
            cvlac_audit_name=view_audits["cvlac"],
            recognized_groups_collection=gr_audit["config"]["recognized_groups_collection"],
            gruplac_raw_collection=gr_audit["config"]["raw_collection"],
            gruplac_normalized_collection=targets["gruplac"], gruplac_audit_name=view_audits["gruplac"]))
        phase("base_graph", lambda: CheckpointedNormalizedWorkGraphBuilder(db=db,
            collection=names["base_graph"], source_collection=targets["cvlac"],
            group_source_collection=targets["gruplac"], gate=gate, batch_size=args.batch_size,
            candidate_partitions=64, publish_pointer=False).build_checkpointed(args.run_name + "_graph"))
        measurement = MincienciasMeasurementPipeline(db)
        phase("measurements_normalize", lambda: measurement.normalize(
            run_name=args.run_name + "_measured", source_collection="gruplac_production_data",
            destination_collection=names["measured"], batch_size=2000))
        phase("measurements_link", lambda: measurement.link(
            run_name=args.run_name + "_links", measured_collection=names["measured"],
            graph_collection=names["base_graph"], links_collection=names["links"],
            target_entity="works", batch_size=2000))
        final_run = args.run_name + "_final"
        phase("works_materialize", lambda: measurement.materialize(
            run_name=final_run, graph_collection=names["base_graph"], measured_collection=names["measured"],
            links_collection=names["links"], target_collection=names["works"],
            target_entity="works", batch_size=2000, publish_pointer=False))
        phase("bibliographic_audit", lambda: ScientiBibliographicEnrichmentAuditor(db).audit(
            audit_name=args.run_name + "_bibliographic_audit", run_name=args.run_name,
            base_collection=names["base_graph"], final_collection=names["works"],
            materialization_run_name=final_run))
        if results["bibliographic_audit"].get("critical_anomalies"):
            raise RuntimeError("bibliographic audit failed")
        collections = {**base["collections"], "works": names["works"]}
        runs = {**base["materialization_runs"], "works": final_run}
        core = phase("core_release", lambda: manager.publish(
            release_name=names["core_release"], collections=collections,
            materialization_runs=runs, publish_pointer=False))
        person_args = {key: person_config[key] for key in (
            "manifest_run_name", "manifest_collection", "directory_collection", "recognized_collection",
            "open_data_collection", "affiliation_run_name", "affiliation_collection")}
        new_person_run = args.run_name + "_persons"
        phase("persons_materialize", lambda: ScientiPersonMaterializer(db,
            run_name=new_person_run, **person_args, cvlac_collection=targets["cvlac"],
            cvlac_audit_name=view_audits["cvlac"], gruplac_collection=targets["gruplac"],
            gruplac_audit_name=view_audits["gruplac"], final_release_name=core["_id"],
            final_release_audit_name=core["audit"], target_collection=names["persons"],
            batch_size=args.batch_size, publish_pointer=False).run())
        release = phase("release", lambda: manager.publish_with_identities(
            release_name=names["release"], base_release_name=core["_id"],
            person_run_name=new_person_run, affiliation_run_name=person_args["affiliation_run_name"],
            publish_pointer=False))
        count_run = args.run_name + "_identified_count"
        phase("identified_count", lambda: ScientiIdentifiedWorkSelector(db,
            release_name=release["_id"], run_name=count_run, count_only=True,
            batch_size=args.batch_size).run())
        phase("identified_materialize", lambda: ScientiIdentifiedWorkSelector(db,
            release_name=release["_id"], run_name=args.run_name + "_identified",
            target_collection=args.target, expected_count_run=count_run,
            batch_size=args.batch_size).run())
        return finish(db, names)
    except Exception as error:
        traceback.print_exc()
        status.update(status="failed", error=repr(error))
        return 1
    finally:
        status["finished_at"] = datetime.now(timezone.utc).isoformat()
        save(directory / "status.json", status)
        client.close()
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="dam")
    parser.add_argument("--source-release", required=True)
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--target", default="", help="Optional assertion of the derived filtered collection name")
    parser.add_argument("--publication-date", default=None, help="aaaammdd; default: launch date in Bogota")
    parser.add_argument("--works-version", default="auto", help="Independent works version, vXX or auto")
    parser.add_argument("--person-version", default="auto", help="Independent person version, vXX or auto")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--confirm-conflicting-dois", action="store_true")
    parser.add_argument("--request-delay", type=float, default=1.0)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,60}", value)
               for value in (args.database, args.run_name, args.source_release, *([args.target] if args.target else []))):
        parser.error("database/run/target/release must be explicit safe names")
    if args.batch_size < 1 or args.request_delay < 0:
        parser.error("invalid batch size or delay")
    if args.target == "current":
        parser.error("current is reserved for the work-output publication pointer")
    directory = args.output_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if args.worker:
        return worker(args, directory)
    sys.path.insert(0, str(ROOT))
    from yuku.scienti_identified_works import WorkIdentifierPolicy
    from yuku.minciencias_publication import publication_date, parse_name, version_number
    if args.publication_date is None and (directory / "config.json").exists():
        args.publication_date = json.loads((directory / "config.json").read_text()).get("publication_date")
    args.publication_date = publication_date(args.publication_date)
    for version in (args.works_version, args.person_version):
        if version != "auto":
            version_number(version)
    if args.target:
        spec = parse_name(args.target, required=True)
        if spec["entity"] != "works" or not spec["filtered"]:
            parser.error("target must be a canonical filtered works collection")
    WorkIdentifierPolicy()
    lock = (directory / "job.lock").open("a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("this output directory already has an active controller")
    config = {key: value for key, value in vars(args).items() if key not in {"worker", "output_dir"}}
    path = directory / "config.json"
    if path.exists() and json.loads(path.read_text()) != config:
        parser.error("output directory belongs to another configuration")
    save(path, config)
    save(directory / "status.json", {"status": "starting", "run_name": args.run_name})
    try:
        with (directory / "job.log").open("a") as output:
            process = subprocess.Popen(
                [sys.executable, "-u", str(Path(__file__).resolve()), *sys.argv[1:],
                 "--publication-date", args.publication_date, "--worker"],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                start_new_session=True, pass_fds=(lock.fileno(),),
                env={**os.environ, "YUKU_JOURNAL_DOI_LOCK_FD": str(lock.fileno())},
            )
        (directory / "job.pid").write_text(str(process.pid) + "\n")
    except Exception as error:
        save(directory / "status.json", {"status": "failed", "error": repr(error)})
        raise
    finally:
        lock.close()
    print(json.dumps({"pid": process.pid, "log": str(directory / "job.log"),
                      "status": str(directory / "status.json"), "report": str(directory / "report.json")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
