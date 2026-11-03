"""All-frame search, local assets, presentation and pure-photo output."""

from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

from ..collection.bundle import read_manifest
from ..collection.storage import writer_lock
from ..collection.windows import identity
from ..evidence import write_json
from ..matching.targets import targets
from .assets import prepare
from .render import render
from .search import search
from .reuse import reuse


def produce(config, output: Path):
    if (config["width"], config["height"], config["display_top_k"]) != (1920, 1080, 5):
        raise ValueError("This presentation uses a 1920x1080 canvas and five distinct places")
    if config["frame_mode"] != "all" or config["top_k"] < 20:
        raise ValueError("Production needs every source frame and at least 20 ranked views")
    if not config["display_top_k"] <= config.get("rerank_top_k",config["display_top_k"]) <= config["top_k"]:
        raise ValueError("Display count must fit within the reranking and retrieval pools")
    if config["crop"][2] % 2 or config["crop"][3] % 2:
        raise ValueError("The pure-photo H.264 output needs even dimensions")
    production_id = identity([config, "all-frame-production-v1"])
    with writer_lock(output):
        path = output / "manifest.json"
        previous = read_manifest(output) if path.exists() else None
        if previous and previous["production_id"] != production_id:
            raise ValueError("Production configuration changed; choose a new output path")
        reuse(output, config)
        target = targets({key: config[key] for key in ("input", "crop", "frame_mode")}, output / "targets")
        if previous and previous["status"] == "complete" and all((output / name).exists() for name in ("final-16x9.mp4", "satellite-full.mp4")):
            return previous
        state = {"schema": "all-frame-production-v1", "production_id": production_id,
                 "config": config, "status": "targets", "started_at": datetime.now(timezone.utc).isoformat()}
        write_json(path, state)
        try:
            state.update(status="search", source_frames=target["count"], target_set_id=target["target_set_id"])
            write_json(path, state)
            if config.get("search_grid", "coarse") == "fine":
                from .grid.search import production_search
                result = production_search(output / "targets", output / "search", config)
            elif config.get("search_grid", "coarse") == "coarse":
                result = search(output / "targets", Path(config["index"]), Path(config["packed_index"]), output / "search",
                                config["top_k"], config["target_batch"])
            else:
                raise ValueError("search_grid must be coarse or fine")
            state.update(status="assets", search=result)
            write_json(path, state)
            assets = prepare(output, config)
            state.update(status="render", assets={key: value for key, value in assets.items() if key != "native_results"})
            write_json(path, state)
            rows = pq.read_table(output / "frame-plan.parquet").to_pylist()
            result = render(output, config, rows)
            state.update(status="complete", output=result, finished_at=datetime.now(timezone.utc).isoformat(),
                         input_sha256=target["input_sha256"], source_index=state["search"]["index_id"],
                         index_id=state["search"]["index_id"],
                         ms_ssim_version=assets["selection_version"] if config.get("selection_method")=="ms-ssim" else None,
                         playback_review="not_performed", external_publication="not_performed")
            write_json(output / "credits.json", {"source": assets.get("credits", ["Contains modified Copernicus Sentinel data"]), "acquisition_years": assets["acquisition_years"],
                       "data_reference": assets.get("source_references", ["https://registry.opendata.aws/sentinel-2-l2a-cogs/"]),
                       "frame_provenance": "selected.parquet", "ranked_candidates": "search/matches.parquet",
                       "input": "User-provided local MP4; original video and audio preserved; no external upload"})
            write_json(path, state)
            return state
        except BaseException as error:
            state.update(status="needs_attention", error_type=type(error).__name__)
            write_json(path, state)
            raise
