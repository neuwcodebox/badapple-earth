"""Bounded file-group jobs; source RGB and target features are shared across poses."""

from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
import pyarrow.parquet as pq

from ...matching.photos import read_rgb_rows
from ...matching.views import render_view
from ..silhouette.features import photo_features,evaluate
from ..silhouette.target_cache import TargetCache
from ..silhouette.temporal import luminance_descriptor
from ..silhouette.control import check_pause
from ...collection.bundle import read_manifest
from ...evidence import write_json
from .align import coarse_search,refine,template,set_target_cache
from .geometry import metadata


def initialize(run,shape,config,mode):
    global TARGETS,CONFIG,CACHE,METRIC,MODE
    run,shape = Path(run),Path(shape)
    target_directory = (run/"targets").resolve()
    TARGETS = {r["slot"]:str(target_directory/r["frame"])
               for r in pq.read_table(run/"targets/targets.parquet",columns=["slot","frame"]).to_pylist()}
    CONFIG = config
    MODE = mode
    cached = Path(config.get("target_pixel_cache",Path(config["output"])/"target-pixels.npy"))
    if cached.exists():set_target_cache(cached)
    METRIC = json.loads((shape/"manifest.json").read_text(encoding="utf-8"))["config"]
    CACHE = TargetCache(shape/"target-features") if mode in ("fine","track") else None
    cv2.setNumThreads(1)


def prepare_targets(run,output,count):
    """Share exact decoded target PNG pixels; avoid per-candidate filesystem reads."""
    run,output = Path(run),Path(output)
    output.mkdir(parents=True,exist_ok=True)
    directory = (run/"targets").resolve()
    identifier = read_manifest(directory)["target_set_id"]
    file,temporary,manifest = output/"target-pixels.npy",output/"target-pixels.tmp.npy",output/"target-pixels.json"
    state = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {
        "target_set_id":identifier,"count":count,"next_slot":0,"status":"building"}
    if state["target_set_id"]!=identifier or state["count"]!=count:raise ValueError("Target pixels belong to other input frames")
    if file.exists():
        saved = np.load(file,mmap_mode="r",allow_pickle=False)
        if saved.shape!=(count,191,256) or saved.dtype!=np.uint8 or state["next_slot"]!=count:
            raise ValueError("Published target pixel cache is inconsistent")
        state["status"] = "complete"
        write_json(manifest,state)
        return
    if temporary.exists() and manifest.exists():values = np.load(temporary,mmap_mode="r+",allow_pickle=False)
    else:
        state.update(next_slot=0,status="building")
        values = np.lib.format.open_memmap(temporary,mode="w+",dtype=np.uint8,shape=(count,191,256))
    rows = pq.read_table(directory/"targets.parquet",columns=["slot","frame"]).to_pylist()
    def read(slot):
        with Image.open(directory/rows[slot]["frame"]) as image:return np.asarray(image.convert("L"),np.uint8)
    with ThreadPoolExecutor(max_workers=8) as pool:
        for begin in range(state["next_slot"],count,256):
            check_pause(output/"pause.request")
            end = min(begin+256,count)
            for slot,pixels in zip(range(begin,end),pool.map(read,range(begin,end))):values[slot] = pixels
            values.flush()
            state["next_slot"] = end
            write_json(manifest,state)
    del values
    temporary.replace(file)
    state["status"] = "complete"
    write_json(manifest,state)


@lru_cache(maxsize=128)
def scene(filename):return json.loads(Path(filename).read_text(encoding="utf-8"))


def coarse_job(groups):
    result = []
    for filename,jobs in groups:
        photos = read_rgb_rows(filename,[offset for offset,_,_,_ in jobs])
        for offset,address,angle,slots in jobs:
            gray = cv2.cvtColor(photos[offset],cv2.COLOR_RGB2GRAY).astype(np.float32)/255
            for slot in slots:
                score,parameters = coarse_search(gray,TARGETS[slot],angle,CONFIG)
                result.append({"slot":slot,"seed_dense_row":address,"coarse_score":score,"parameters":parameters.tolist()})
    return result


def fine_job(groups):
    result = []
    for filename,jobs in groups:
        photos = read_rgb_rows(filename,[offset for offset,_,_ in jobs])
        for offset,row,frames in jobs:
            rgb = Image.fromarray(photos[offset])
            gray = cv2.cvtColor(photos[offset],cv2.COLOR_RGB2GRAY).astype(np.float32)/255
            source_scene = scene(row["_scene_filename"])
            for slot,initial in frames:
                target = CACHE[slot]
                mask = template(TARGETS[slot],64,48)[0]>.5
                fitted = refine(gray,TARGETS[slot],np.asarray(initial,np.float64),
                                prior_weight=CONFIG["tracking_prior_weight"] if MODE=="track" else 0.,local=MODE=="track")
                seen = set()
                for parameters in (initial,fitted):
                    view = metadata(row,source_scene,parameters)
                    if view["view_id"] in seen:continue
                    seen.add(view["view_id"])
                    image = render_view(rgb,view,(256,191))
                    measured = np.asarray(image.convert("L"),np.float32)/255
                    metrics = evaluate(target,photo_features(measured),measured,METRIC)
                    result.append({**{k:v for k,v in view.items() if not k.startswith("_")},**metrics,
                        "slot":slot,"luminance":luminance_descriptor(image,mask).tolist(),
                        "appearance":np.asarray(image.convert("L").resize((32,24),Image.Resampling.BOX)).ravel().tolist()})
    return result


def appearance_job(groups):
    result = []
    for filename,jobs in groups:
        photos = read_rgb_rows(filename,[offset for offset,_ in jobs])
        for offset,row in jobs:
            image = render_view(Image.fromarray(photos[offset]),row,(256,191))
            result.append({"view_id":row["view_id"],"appearance":np.asarray(image.convert("L").resize((32,24),Image.Resampling.BOX)).ravel().tolist()})
    return result
