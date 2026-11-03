#!/usr/bin/env python3
"""Detached inventory controller with persistent PID, log and final status."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    status = output / "status.json"
    def write(value):
        temp = status.with_suffix(".tmp")
        temp.write_text(json.dumps(value, indent=2) + "\n"); temp.replace(status)
    if not args.worker:
        if status.exists() and json.loads(status.read_text()).get("status") == "running":
            raise RuntimeError("inventory already running; inspect status/PID before restarting")
        with (output / "inventory.log").open("a") as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--output", str(output), "--worker"],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        (output / "pid").write_text(str(process.pid) + "\n")
        print(json.dumps({"pid": process.pid, "log": str(output / "inventory.log")}))
        return
    import os
    write({"status": "running", "pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()})
    try:
        code = subprocess.call([sys.executable, str(Path(__file__).with_name("manage_scraping_type_catalog.py")), "inventory", "--output", str(output)], stdin=subprocess.DEVNULL)
    except BaseException as error:
        write({"status": "failed", "error": str(error)}); raise
    write({"status": "complete" if code == 0 else "failed", "exit_code": code,
           "finished_at": datetime.now(timezone.utc).isoformat()})
    raise SystemExit(code)


if __name__ == "__main__":
    main()
