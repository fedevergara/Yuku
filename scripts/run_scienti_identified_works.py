#!/usr/bin/env python3
"""Detach an exact count followed by audited identified-work materialization."""

import argparse
from datetime import datetime, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import traceback


def save(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
    temporary.replace(path)


def compact(result):
    return {key: result[key] for key in (
        "_id", "status", "config", "stats", "selection_digest", "documents_digest",
        "created_at", "completed_at",
    ) if key in result}


def worker(args, directory):
    # The inherited descriptor retains the launcher's exclusive job-directory
    # lock. Closing the launcher doesn't release it while this process lives.
    descriptor = int(os.environ.pop("YUKU_IDENTIFIED_JOB_LOCK_FD"))
    lock = os.fdopen(descriptor, "a")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pymongo import MongoClient
    from yuku.scienti_identified_works import ScientiIdentifiedWorkSelector
    from yuku.minciencias_publication import EntityRetention, filtered_target, publish_filtered_current

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    status_path = directory / "status.json"
    state = {
        "status": "running", "phase": "count", "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "release": args.release, "database": args.database,
        "count_run": args.run_name + "_count", "build_run": args.run_name,
        "target": args.target,
    }
    save(status_path, state)
    client = MongoClient(os.environ.get("YUKU_MONGO_URI", "mongodb://localhost:27017/"),
                         serverSelectionTimeoutMS=10000)
    try:
        db = client[args.database]
        args.target = filtered_target(db, args.release, args.target)
        state["target"] = args.target
        save(status_path, state)
        common = {"release_name": args.release, "batch_size": args.batch_size,
                  "progress_every": args.progress_every}
        logging.info("Starting exact count: %s", state["count_run"])
        count = ScientiIdentifiedWorkSelector(
            db, run_name=state["count_run"], count_only=True, **common,
        ).run()
        save(directory / "count.result.json", compact(count))
        state.update({"phase": "materialization", "exact_selected": count["stats"]["selected"]})
        save(status_path, state)
        logging.info("Exact count complete: %s eligible / %s source works",
                     count["stats"]["selected"], count["stats"]["processed"])
        built = ScientiIdentifiedWorkSelector(
            db, run_name=args.run_name, target_collection=args.target,
            expected_count_run=state["count_run"], **common,
        ).run()
        with EntityRetention(db).lock():
            current_updated = publish_filtered_current(db, args.release, args.target)
        report = {
            "status": "complete", "database": args.database, "collection": args.target,
            "source_works": count["stats"]["processed"],
            "selected_works": built["stats"]["selected"],
            "excluded_works": built["stats"]["excluded"],
            "count": compact(count), "materialization": compact(built),
            "current_updated": current_updated,
        }
        save(directory / "report.json", report)
        state.update({"status": "complete", "phase": "published",
                      "finished_at": datetime.now(timezone.utc).isoformat()})
        save(status_path, state)
        logging.info("Published %s.%s: %s works", args.database, args.target, report["selected_works"])
        return 0
    except Exception as error:
        traceback.print_exc()
        state.update({"status": "failed", "error": repr(error),
                      "finished_at": datetime.now(timezone.utc).isoformat()})
        save(status_path, state)
        return 1
    finally:
        client.close()
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="dam")
    parser.add_argument("--release", required=True)
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--target", default="", help="Defaults to the pinned full works name plus _filtered")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--progress-every", type=int, default=10000)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.target == "current":
        parser.error("current is reserved for the work-output publication pointer")
    directory = args.output_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if args.worker:
        return worker(args, directory)
    if args.batch_size < 1 or args.progress_every < 1:
        parser.error("batch-size and progress-every must be positive")
    # Validate dependencies, names and the template before launching; database
    # validation/counting happens in the detached worker and is reported there.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from yuku.scienti_identified_works import WorkIdentifierPolicy
    policy = WorkIdentifierPolicy()
    import re
    if not all(re.fullmatch(r"[A-Za-z0-9_-]+", name)
               for name in (args.release, args.run_name, *([args.target] if args.target else []))):
        parser.error("release, run-name and target must be explicit safe names")
    lock = (directory / "job.lock").open("a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("A worker already owns this output directory")
    job_config = {"release": args.release, "database": args.database,
                  "run_name": args.run_name, "target": args.target,
                  "policy": policy.fingerprint}
    config_path = directory / "config.json"
    if config_path.exists() and json.loads(config_path.read_text()) != job_config:
        parser.error("This output directory belongs to another configuration; use a new directory")
    save(config_path, job_config)
    save(directory / "status.json", {
        "status": "starting", "release": args.release, "database": args.database,
        "target": args.target, "run_name": args.run_name,
    })
    try:
        with (directory / "job.log").open("a") as log:
            process = subprocess.Popen(
                [sys.executable, "-u", str(Path(__file__).resolve()), *sys.argv[1:], "--worker"],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, pass_fds=(lock.fileno(),),
                env={**os.environ, "YUKU_IDENTIFIED_JOB_LOCK_FD": str(lock.fileno())},
            )
        (directory / "job.pid").write_text(str(process.pid) + "\n")
    except Exception as error:
        save(directory / "status.json", {"status": "failed", "error": repr(error)})
        raise
    finally:
        lock.close()
    print(json.dumps({"pid": process.pid, "log": str(directory / "job.log"),
                      "status": str(directory / "status.json"),
                      "report": str(directory / "report.json")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
