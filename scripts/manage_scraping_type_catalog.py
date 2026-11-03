#!/usr/bin/env python3
"""Inventory Mongo sources, export a review workbook, or import a reviewed one."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yuku.scraping_type_review import consolidate, export_excel, import_excel, inventory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("inventory")
    scan.add_argument("--database", default="dam")
    scan.add_argument("--output", type=Path, required=True)
    export = sub.add_parser("export")
    export.add_argument("--catalog", type=Path, required=True)
    export.add_argument("--excel", type=Path, required=True)
    merge = sub.add_parser("consolidate")
    merge.add_argument("--inventory", type=Path, required=True)
    merge.add_argument("--output", type=Path, required=True)
    review = sub.add_parser("import")
    review.add_argument("--catalog", type=Path, required=True)
    review.add_argument("--excel", type=Path, required=True)
    review.add_argument("--version", required=True)
    review.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "inventory":
        from pymongo import MongoClient
        args.output.mkdir(parents=True, exist_ok=True)
        with MongoClient(os.environ.get("YUKU_MONGO_URI", "mongodb://localhost:27017/"), serverSelectionTimeoutMS=10000) as client:
            db = client[args.database]
            current = db.scienti_entity_publications.find_one({"_id": "current"}) or {}
            run = db.scienti_entity_normalization_runs.find_one({"_id": current.get("current_run_name")}) or {}
            config = run.get("config") or {}
            sources = {kind: config.get(kind + "_collection") for kind in ("cvlac", "gruplac")}
            if not all(sources.values()):
                raise RuntimeError("cannot pin current normalization sources")
            print(json.dumps({"stage": "sources_pinned", "sources": sources}), flush=True)
            payload = inventory(db, sources, lambda v: print(json.dumps(v), flush=True))
        (args.output / "catalog.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        export_excel(payload, args.output / "Catalogo_tipos_scraping_minciencias.xlsx")
        counts = Counter(e["status"] for e in payload["entries"])
        report = {"status": "complete", "sources": sources, "totals": payload["totals"],
                  "classifications": len(payload["entries"]), "statuses": dict(counts),
                  "occurrences_by_status": dict(Counter({s: sum(e["frequency"] for e in payload["entries"] if e["status"] == s) for s in counts})),
                  "rejected_candidate_labels": len(payload["rejected_candidates"])}
        (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report), flush=True)
    elif args.command == "consolidate":
        payload = consolidate(json.loads(args.inventory.read_text()))
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "catalog.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        export_excel(payload, args.output / "Catalogo_tipos_scraping_minciencias.xlsx")
        counts = Counter(e["status"] for e in payload["entries"])
        report = {"status": "complete", "totals": payload["totals"], "classifications": len(payload["entries"]),
                  "statuses": dict(counts), "occurrences_by_status": {
                      s: sum(e["frequency"] for e in payload["entries"] if e["status"] == s) for s in counts},
                  "recovered_candidate_labels": sum(e["candidate_frequency"] > 0 for e in payload["entries"]),
                  "rejected_candidate_labels": len(payload["rejected_candidates"])}
        (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report), flush=True)
    elif args.command == "export":
        export_excel(json.loads(args.catalog.read_text()), args.excel)
    else:
        result = import_excel(args.excel, json.loads(args.catalog.read_text()), args.version)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"status": "validated", "output": str(args.output), "version": args.version}))


if __name__ == "__main__":
    main()
