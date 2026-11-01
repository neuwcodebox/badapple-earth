"""Parallel collectors with independent writers, resumable indexes and logs."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from ..evidence import write_json
from .pipeline import validate_config
from .storage import writer_lock
from .windows import allocate, identity


def partition(config, limit, workers):
    validate_config(config)
    if type(workers) is not int or not 1 <= workers <= min(16, len(config["areas"])):
        raise ValueError("Workers must be between 1 and 16 and not exceed the AOI count")
    if type(limit) is not int or limit < workers:
        raise ValueError("Candidate limit must be at least the worker count")
    groups = sorted({area["terrain_group"] for area in config["areas"]})
    parts = [[] for _ in range(workers)]
    offset = 0
    for group in groups:
        areas = [area for area in config["areas"] if area["terrain_group"] == group]
        for number, area in enumerate(areas):
            parts[(offset + number) % workers].append(area)
        offset = (offset + len(areas)) % workers
    limits = allocate(limit, list(range(workers)))
    return [({**config, "areas": areas}, limits[number]) for number, areas in enumerate(parts)]


def collect_parallel(config: dict, root: Path, limit: int, workers=4):
    jobs = partition(config, limit, workers)
    with writer_lock(root):
        state_path = root / "collection.json"
        batch_id = identity([config, limit, workers])
        if state_path.exists():
            previous = json.loads(state_path.read_text(encoding="utf-8"))
            if previous["batch_id"] != batch_id:
                raise ValueError("Parallel collection configuration differs; use a new output path")
        state = {"schema": "collection-batch-v1", "batch_id": batch_id, "status": "running",
                 "requested": limit, "workers": workers, "started_at": datetime.now(timezone.utc).isoformat(),
                 "parts": [{"path": f"part-{number + 1:02d}", "requested": quota, "status": "running"}
                           for number, (_, quota) in enumerate(jobs)]}
        write_json(state_path, state)
        environment = {**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
                       "PYTHONUNBUFFERED": "1"}
        processes, logs = [], []
        try:
            for number, (settings, quota) in enumerate(jobs):
                part = state["parts"][number]
                config_path = root / f"{part['path']}.json"
                write_json(config_path, settings)
                log = (root / f"{part['path']}.log").open("a", encoding="utf-8")
                logs.append(log)
                log.write(f"\nCollection invocation {state['started_at']}\n")
                log.flush()
                command = [sys.executable, "-m", "badapple_earth", "collect", "--config", str(config_path),
                           "--output", str(root / part["path"]), "--limit", str(quota), "--redistribute"]
                processes.append(subprocess.Popen(command, stdout=log, stderr=log, env=environment))
            print(f"Started {workers} collectors for {limit:,} additional places; logs: {root}", flush=True)
            # Block on process completion rather than repeatedly scanning large indexes.
            with ThreadPoolExecutor(max_workers=workers) as pool:
                pending = {pool.submit(process.wait): number for number, process in enumerate(processes)}
                for future in as_completed(pending):
                    number = pending[future]
                    part = state["parts"][number]
                    part["exit_code"] = future.result()
                    path = root / part["path"] / "manifest.json"
                    manifest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                    part.update(rows=manifest.get("rows", 0), status=manifest.get("status", "failed"))
                    write_json(state_path, state)
                    print(f"{part['path']}: {part['status']}, {part['rows']:,}/{part['requested']:,}", flush=True)
            state["rows"] = sum(part["rows"] for part in state["parts"])
            state["status"] = "complete" if all(part["status"] == "complete" and part["exit_code"] == 0
                                                   for part in state["parts"]) else "partial"
            state["finished_at"] = datetime.now(timezone.utc).isoformat()
            write_json(state_path, state)
            return state
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    process.wait()
            for log in logs:
                log.close()
