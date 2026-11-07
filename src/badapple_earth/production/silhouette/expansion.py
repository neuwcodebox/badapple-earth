"""Reuse a frozen baseline, search appended prepared chunks, and render luminance video."""

import argparse
import json
from pathlib import Path
import shutil

from ...collection.bundle import read_manifest
from ...collection.windows import identity
from ...evidence import write_json
from .full import produce as shape_candidates
from .monochrome import produce as luminance_video


def prepare_input(baseline,index,output):
    """Keep baseline artifacts immutable; share files without copying image data."""
    baseline,index,output = Path(baseline),Path(index),Path(output)
    original = read_manifest(baseline)
    identifier = identity(["silhouette-input-reference-v1",original["production_id"],read_manifest(index)["index_id"]])
    if (output/"manifest.json").exists():
        if read_manifest(output)["production_id"]!=identifier:raise ValueError("Input reference belongs to another index")
        return
    files = [baseline/name for name in ("selected.parquet","candidates.parquet")]
    files += list((baseline/"targets").rglob("*"))
    files += [baseline/"search"/name for name in ("manifest.json","query_inverse.npy","matches.parquet")]
    for source in files:
        if not source.is_file() or source.name=="writer.lock" or source.suffix==".tmp":continue
        target = output/source.relative_to(baseline)
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():
            try:target.hardlink_to(source)
            except OSError:shutil.copyfile(source,target)
    write_json(output/"manifest.json",{
        "schema":"silhouette-input-reference-v1","production_id":identifier,"status":"ready",
        "baseline_run":str(baseline),"baseline_production_id":original["production_id"],
        "config":{**original["config"],"index":str(index)},
        "purpose":"Shared targets and baseline candidates; appended views are searched in the new silhouette run"})


def produce(config,resume=False):
    if resume:
        for key in ("shape_output","output"):
            (Path(config[key])/"pause.request").unlink(missing_ok=True)
    shape = json.loads(Path(config["shape_config"]).read_text(encoding="utf-8"))
    prepare_input(config["baseline_run"],config["index"],shape["run"])
    result = shape_candidates(shape,config["shape_output"],resume=resume)
    if result["status"]!="candidates_ready":return result
    luminance = json.loads(Path(config["luminance_config"]).read_text(encoding="utf-8"))
    # A pause requested during candidate reduction also blocks the next stage.
    if (Path(config["shape_output"])/"pause.request").exists():return {"status":"paused"}
    return luminance_video(luminance,config["output"],resume=resume)


if __name__=="__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",default="config/produce-expand.json")
    parser.add_argument("--resume",action="store_true")
    parser.add_argument("--pause",action="store_true")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.pause:
        for key in ("shape_output","output"):
            root = Path(config[key]);root.mkdir(parents=True,exist_ok=True)
            (root/"pause.request").touch()
        print("Pause requested; current batches will be saved",flush=True)
    else:
        print(json.dumps({"status":produce(config,args.resume)["status"]}),flush=True)
