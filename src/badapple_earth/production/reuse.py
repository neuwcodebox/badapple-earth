"""Reuse immutable local inputs and image caches when revising production."""

import os
from pathlib import Path
import shutil

from ..collection.bundle import read_manifest


def reuse(root, config):
    if not config.get("reuse_run"):
        return
    previous = Path(config["reuse_run"])
    if previous.resolve() == root.resolve():
        raise ValueError("reuse_run must be a separate completed run")
    saved = read_manifest(previous)
    if saved["status"] != "complete":
        raise ValueError("Reuse source must be a completed production")
    if any(saved["config"][key] != config[key] for key in ("input", "crop", "frame_mode", "index", "top_k")):
        raise ValueError("Reuse source has different targets, index or retrieval settings")
    # Hard links are safe because generated tables/manifests are published by
    # replacement, never modified through the linked inode.
    for subdir in ("targets", "search", "assets/thumbs", "assets/selected"):
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
