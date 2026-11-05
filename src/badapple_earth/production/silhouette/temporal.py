"""Choose a color-consistent path through already measured silhouette candidates."""

from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import get_context
from pathlib import Path
import argparse
import json
import time

import numpy as np
from PIL import Image
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ...collection.bundle import members,read_manifest
from ...collection.storage import writer_lock
from ...collection.windows import identity
from ...evidence import write_json
from ...matching.output import source_credits
from ...matching.photos import read_rgb_rows
from ...matching.views import render_view
from ..assets import cached_photos,old_rgb_refs
from ..render import encoder
from ..variants.pipeline import write_table
from .pipeline import comparison_frame,interval_frames

VERSION = "shape-band-region-chroma-path-v1"


def color_descriptor(image,mask):
    """Equal foreground/background palette weights, with reduced luma weight."""
    pixels = np.asarray(image.convert("YCbCr").resize((64,48),Image.Resampling.BOX),np.float32)/255
    mask = np.asarray(mask,bool)
    parts = []
    for region in (mask,~mask):
        values = pixels[region] if region.any() else pixels.reshape(-1,3)
        # Mean Y has one quarter the sensitivity of chroma; chroma histograms
        # distinguish palettes with equal means. Region size cannot dominate.
        mean = values.mean(axis=0,dtype=np.float64)*np.array([.25,1,1],np.float32)
        bins = np.minimum((values[:,1:]*6).astype(int),5)
        hist = np.bincount(bins[:,0]*6+bins[:,1],minlength=36)/len(values)
        parts.extend((np.sqrt(.1)*mean,np.sqrt(.4)*np.sqrt(hist)))
    return np.concatenate(parts).astype(np.float32)


def candidate_pool(rows,baseline,tolerance,limit):
    if baseline.get("fallback"):
        return [baseline]
    valid = [r for r in rows if r["visible"]]
    best = max(r["shape_score"] for r in valid)
    valid = sorted((r for r in valid if r["shape_score"]>=best-tolerance),
                   key=lambda r:(-r["shape_score"],r["balanced_mse"],r["view_id"]))[:limit]
    if not any(r["dense_row"]==baseline["dense_row"] for r in valid):
        valid[-1] = baseline
    return valid


def path(pools,descriptors,weight):
    """Exact dynamic program on the bounded per-frame candidate sets."""
    costs,backs = None,[]
    for rows,features in zip(pools,descriptors):
        best = max(r["shape_score"] for r in rows)
        unary = np.array([best-r["shape_score"] for r in rows],np.float64)
        if costs is None:
            costs = unary
            backs.append(None)
        else:
            differences = previous[:,None,:]-features[None,:,:]
            edge = np.sum(differences*differences,axis=2,dtype=np.float64)
            total = costs[:,None]+weight*edge
            parent = np.argmin(total,axis=0)
            costs = unary+total[parent,np.arange(len(rows))]
            backs.append(parent)
        previous = features
    selected = [int(np.argmin(costs))]
    for back in reversed(backs[1:]):
        selected.append(int(back[selected[-1]]))
    return list(reversed(selected))


def describe_groups(groups,targets):
    masks = {}
    result = []
    for filename,jobs in groups:
        photos = read_rgb_rows(filename,[i for i,_,_ in jobs])
        for position,row,slots in jobs:
            image = render_view(Image.fromarray(photos[position]),row,(256,191))
            for slot in slots:
                if slot not in masks:
                    with Image.open(targets[slot]) as target:
                        masks[slot] = np.asarray(target.convert("L").resize((64,48),Image.Resampling.BOX))>127
                result.append({"slot":slot,"dense_row":row["dense_row"],
                               "color":color_descriptor(image,masks[slot]).tolist()})
    return result


def descriptors(root,run,metadata,pools,targets,workers):
    source = read_manifest(run)
    index = Path(source["config"]["index"])
    directories = {m["index_id"]:p for p,m,_ in members(index,read_manifest(index))}
    refs = old_rgb_refs(Path(source["config"]["extra_rgb_cache"]),
                        {r["tile_id"] for r in metadata.values() if not r.get("rgb_cache_ref")})
    wanted = defaultdict(list)
    for slot,rows in pools.items():
        for row in rows:
            wanted[row["dense_row"]].append(slot)
    grouped = defaultdict(list)
    for position,slots in sorted(wanted.items()):
        row = metadata[position]
        if row["tile_id"] in refs:
            filename,offset = refs[row["tile_id"]]
        else:
            filename,offset = directories[row["index_member"]]/row["rgb_cache_ref"],row["rgb_cache_row"]
        view = {k:row.get(k) for k in ("dense_row","rotation_degrees","rotation_crop",
                                     "transform_version","view_affine","transform_size")}
        grouped[str(filename.resolve())].append((offset,view,slots))
    groups = sorted(grouped.items())
    chunks = [groups[i:i+24] for i in range(0,len(groups),24)]
    folder = root/"colors"
    folder.mkdir(exist_ok=True)
    paths = [folder/f"{i:05d}.parquet" for i in range(len(chunks))]
    with ProcessPoolExecutor(max_workers=workers,mp_context=get_context("spawn")) as pool:
        pending = {pool.submit(describe_groups,chunk,targets):paths[i]
                   for i,chunk in enumerate(chunks) if not paths[i].exists()}
        done = len(paths)-len(pending)
        for future in as_completed(pending):
            write_table(pending[future],future.result())
            done += 1
            print(f"Color descriptors: {done}/{len(paths)} batches",flush=True)
    return {(r["slot"],r["dense_row"]):np.array(r["color"],np.float32)
            for p in paths for r in pq.read_table(p).to_pylist()}


def compare(config,output):
    source_root,root = Path(config["source"]),Path(output)
    original = read_manifest(source_root)
    if original["status"]!="complete":
        raise ValueError("Color continuity requires completed silhouette scores")
    if not 0<=config["shape_tolerance"]<=1 or config["max_candidates"]<1 or config["color_weight"]<0:
        raise ValueError("Invalid color continuity settings")
    run = Path(original["source_run"])
    source = read_manifest(run)
    experiment_id = identity([VERSION,original["experiment_id"],config])
    with writer_lock(root):
        if (root/"manifest.json").exists():
            saved = read_manifest(root)
            if saved["experiment_id"]!=experiment_id:
                raise ValueError("Use another color output for changed settings")
            if saved["status"]=="complete":return saved
        started = time.perf_counter()
        manifest = {"experiment_id":experiment_id,"version":VERSION,"config":config,"status":"colors",
                    "network_reads":0,"search_reused":True,"shape_scores_reused":True}
        write_json(root/"manifest.json",manifest)
        targets = pq.read_table(run/"targets/targets.parquet").to_pylist()
        baseline = {r["slot"]:r for r in pq.read_table(source_root/"selected.parquet").to_pylist()
                    if r["method"]=="visible"}
        by_slot = defaultdict(list)
        for row in pq.read_table(source_root/"evaluations.parquet").to_pylist():
            by_slot[row["slot"]].append(row)
        # View IDs are only needed for deterministic tie order; filter Arrow
        # before converting the retained full provenance records to Python.
        ids = dict(zip(pq.read_table(source_root/"candidates.parquet",columns=["dense_row"])["dense_row"].to_pylist(),
                       pq.read_table(source_root/"candidates.parquet",columns=["view_id"])["view_id"].to_pylist()))
        pools = {slot:candidate_pool([{**r,"view_id":ids[r["dense_row"]]} for r in rows],baseline[slot],
                                      config["shape_tolerance"],config["max_candidates"])
                 for slot,rows in by_slot.items()}
        del by_slot
        positions = sorted({r["dense_row"] for rows in pools.values() for r in rows})
        table = pq.read_table(source_root/"candidates.parquet")
        table = table.filter(pc.is_in(table["dense_row"],value_set=pa.array(positions,type=pa.int64())))
        metadata = {r["dense_row"]:r for r in table.to_pylist()}
        manifest.update(candidate_views=len(metadata),frame_candidate_pairs=sum(map(len,pools.values())))
        write_json(root/"manifest.json",manifest)
        print(f"Color pool: {len(metadata):,} views; {manifest['frame_candidate_pairs']:,} pairs",flush=True)
        target_paths = {r["slot"]:str((run/"targets"/r["frame"]).resolve()) for r in targets if r["slot"] in pools}
        colors = descriptors(root,run,metadata,pools,target_paths,config["workers"])
        chosen,records,videos = {},[],[]
        for clip in original["config"]["intervals"]:
            frames = interval_frames(targets,clip)
            candidates = [pools[f["slot"]] for f in frames]
            features = [np.stack([colors[f["slot"],r["dense_row"]] for r in rows]) for f,rows in zip(frames,candidates)]
            indices = path(candidates,features,config["color_weight"])
            for frame,rows,index in zip(frames,candidates,indices):
                slot = frame["slot"]
                row = rows[index]
                chosen[slot] = {**metadata[row["dense_row"]],**row,"fallback":baseline[slot].get("fallback")}
                records.append({**chosen[slot],"slot":slot,"method":"color-continuous", "source_pts":frame["source_pts"],
                                "source_time_base":frame["source_time_base"]})
            jump = {}
            transitions = {}
            for method,rows in (("independent",[baseline[f["slot"]] for f in frames]),
                                ("continuous",[chosen[f["slot"]] for f in frames])):
                palette = [colors[f["slot"],r["dense_row"]] for f,r in zip(frames,rows)]
                jump[method] = float(np.mean([np.sum((a-b)**2) for a,b in zip(palette,palette[1:])]))
                transitions[method] = sum(a["view_id"]!=b["view_id"] for a,b in zip(rows,rows[1:]))
            videos.append({**clip,"frames":len(frames),"video":clip["name"]+".mp4","mean_color_jump":jump,
                           "transitions":transitions,"mean_shape_loss":float(np.mean([
                               baseline[f["slot"]]["shape_score"]-chosen[f["slot"]]["shape_score"] for f in frames]))})
        write_table(root/"selected.parquet",records)
        photos = {r["view_id"]:r for r in [*records,*baseline.values()]}
        manifest["photos"] = cached_photos(source_root,root,photos.values(),source["config"],(256,191))
        credits,references = source_credits(photos.values())
        write_json(root/"credits.json",{"source":credits,"references":references})
        manifest.update(status="render",videos=videos)
        write_json(root/"manifest.json",manifest)
        for clip in videos:
            name = clip["name"]
            temporary = root/(name+".tmp.mp4")
            with (root/(name+"-encode.log")).open("wb") as log:
                process = encoder(temporary,1920,1080,"30000/1001",{"encoder_threads":4,"preset":"medium","crf":18},log)
                try:
                    for frame in interval_frames(targets,clip):
                        slot = frame["slot"]
                        image = comparison_frame(frame,{"current":baseline[slot],"visible":chosen[slot]},root,run,
                                                 source["config"],len(targets),0,"pair",
                                                 ("SHAPE / INDEPENDENT","SHAPE + COLOR CONTINUITY"),
                                                 f"Shape {baseline[slot]['shape_score']:.4f}")
                        process.stdin.write(image.tobytes())
                    process.stdin.close()
                    if process.wait()!=0:raise ValueError(f"Encoding failed; see {name}-encode.log")
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait()
            temporary.replace(root/(name+".mp4"))
        manifest.update(status="complete",elapsed_seconds=round(time.perf_counter()-started,3),playback_review="not_performed")
        write_json(root/"manifest.json",manifest)
        return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",required=True)
    parser.add_argument("--output",required=True)
    args = parser.parse_args()
    result = compare(json.loads(Path(args.config).read_text(encoding="utf-8")),args.output)
    print(json.dumps({"status":result["status"],"videos":result["videos"]}),flush=True)
