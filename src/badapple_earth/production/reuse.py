"""Reuse immutable local inputs and image caches when revising production."""

import os
from pathlib import Path
import shutil

from ..collection.bundle import read_manifest


def reuse(root, config):
    reuse_run = config.get("reuse_run") or config.get("incremental_base_run")
    if not reuse_run:
        return
    previous = Path(reuse_run)
    if previous.resolve() == root.resolve():
        raise ValueError("reuse_run must be a separate completed run")
    saved = read_manifest(previous)
    if saved["status"] != "complete":
        raise ValueError("Reuse source must be a completed production")
    if any(saved["config"][key] != config[key] for key in ("input", "crop", "frame_mode")):
        raise ValueError("Reuse source has different targets, index or retrieval settings")
    same_index = saved["config"]["index"] == config["index"]
    if not same_index and config.get("incremental_base_run") != reuse_run:
        raise ValueError("Reuse source has a different index without incremental retrieval")
    # Hard links are safe because generated tables/manifests are published by
    # replacement, never modified through the linked inode.
    # Expanded pools reuse only the photos actually displayed or selected later.
    subdirs = ["targets"] if not same_index else ["targets", "assets/thumbs", "assets/selected", "photos"]
    if (same_index and saved["config"].get("search_grid", "coarse") == config.get("search_grid", "coarse")
            and saved["config"]["top_k"] == config["top_k"]):
        subdirs.append("search")
    for subdir in subdirs:
        source = previous / subdir
        for path in source.rglob("*"):
            if not path.is_file() or path.name == "writer.lock":
                continue
            destination = root / subdir / path.relative_to(source)
            if destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(path, destination)
            except OSError:
                shutil.copy2(path, destination)
