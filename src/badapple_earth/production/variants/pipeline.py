"""Compare quarter turns, 30 degree steps and a smaller crop on identical frames."""

from collections import defaultdict
from fractions import Fraction
from pathlib import Path
import time

import numpy as np
from PIL import Image, ImageDraw
import pyarrow as pa
import pyarrow.parquet as pq

from ...collection.bundle import read_manifest
from ...collection.storage import writer_lock
from ...collection.windows import identity
from ...evidence import write_json
from ..assets import cached_photos, frame_credit
from ..grid.rerank import evaluate
from ..layout import BG, INK, MUTED, font, resized, timestamp
from ..render import encoder
from ..rerank import VERSION as SSIM_VERSION
from .bank import Bank
from .search import search, VERSION

LABELS = {"quarter":"90 DEGREE BASELINE", "angle30":"30 DEGREE STEPS",
          "crop75":"30 DEGREE + 75% CROP"}


def write_table(path, rows):
    columns = sorted({key for row in rows for key in row})
    temporary = path.with_suffix(".tmp.parquet")
    pq.write_table(pa.Table.from_pylist([{key:r.get(key) for key in columns} for r in rows]),
                   temporary,compression="zstd")
    temporary.replace(path)


def merge(base, additional, k):
    pool = {r["view_id"]:r for r in base+additional}
    return [{**row, "rank":i+1} for i,row in enumerate(
        sorted(pool.values(), key=lambda r:(r["sse"],r["view_id"]))[:k])]


def comparison_frame(frame, chosen, root, run, config, total):
    image = Image.new("RGB", (1920,1080), BG)
    draw = ImageDraw.Draw(image)
    panels = [("SOURCE", run/"targets"/frame["frame"], None)]
    panels.extend((label, root/"photos"/(chosen[method]["view_id"]+".png"),chosen[method])
                  for method,label in LABELS.items())
    for i,(label,path,row) in enumerate(panels):
        x,y = 24+(i%2)*960, 12+(i//2)*522
        draw.text((x,y),label,fill=INK,font=font(config["font_bold"],22))
        image.paste(resized(str(path),608,454),(x+152,y+34))
        if row:
            detail = f"MS-SSIM {row['ms_ssim']:.5f}    {row['rotation_degrees']} deg    crop {row.get('crop_scale') or 1:.2f}"
            draw.text((x,y+490),detail,fill=MUTED,font=font(config["font_regular"],16))
            draw.text((x,y+510),frame_credit(row),fill=MUTED,font=font(config["font_regular"],10))
    draw.text((24,1060),f"{timestamp(frame['target_time'])}    {frame['slot']+1:,} / {total:,}",
              fill=MUTED,font=font(config["font_regular"],14))
    return image


def compare(config, output):
    run, root = Path(config["baseline_run"]), Path(output)
    source, target, baseline = read_manifest(run), read_manifest(run/"targets"), read_manifest(run/"search")
    if (source["status"] != "complete" or baseline["status"] != "complete"
            or baseline.get("grid") != [32,24] or baseline["top_k"] != config["top_k"]
            or baseline.get("rotations") != [0,90,180,270]):
        raise ValueError("Affine comparison requires a completed quarter-turn fine-grid baseline")
    begin = Fraction(str(config["start_seconds"]))
    end = begin+Fraction(str(config["duration_seconds"]))
    targets = [r for r in pq.read_table(run/"targets/targets.parquet").to_pylist()
               if begin <= Fraction(r["source_pts"])*Fraction(r["source_time_base"]) < end]
    if not targets or begin < 0 or float(end) > target["duration_seconds"]+1e-6:
        raise ValueError("Comparison must use a nonempty original-frame interval")
    bank = Bank(config["bank"],config)
    if baseline["index_id"] != bank.state["parent_index_id"]:
        raise ValueError("Affine and baseline searches need the exact same geographic parents")
    experiment_id = identity([VERSION,SSIM_VERSION,baseline["run_id"],bank.state["bank_id"],config])
    with writer_lock(root):
        if (root/"manifest.json").exists():
            saved = read_manifest(root)
            if saved["experiment_id"] != experiment_id:
                raise ValueError("Use another directory for changed affine comparison settings")
            if saved["status"] == "complete" and (root/"comparison.mp4").exists():
                return saved
        manifest = {"experiment_id":experiment_id,"status":"search","config":config,
                    "index_id":baseline["index_id"],"view_space_id":bank.state["bank_id"],
                    "target_set_id":target["target_set_id"],"ms_ssim_version":SSIM_VERSION,
                    "candidate_places":bank.state["parents"],"new_views":bank.state["rows"],
                    "source_frames":len(targets),"first_slot":targets[0]["slot"],"last_slot":targets[-1]["slot"],
                    "frame_rate":target["frame_rate"],"audio_mode":"silent","network_reads":0}
        write_json(root/"manifest.json",manifest)
        started = time.perf_counter()
        pixels = np.load(run/"targets/gray_fine.npy",mmap_mode="r",allow_pickle=False)
        slots = [r["slot"] for r in targets]
        retrieved = search(bank, pixels[slots], slots, root, config)
        positions = sorted(set(retrieved["positions"].reshape(-1).tolist()))
        offset = baseline["view_count"]
        metadata = bank.resolve(positions,offset)
        original_inverse = np.load(run/"search/query_inverse.npy",allow_pickle=False)
        wanted = sorted({int(original_inverse[i]) for i in slots})
        old = pq.read_table(run/"search/matches.parquet", filters=[("query","in",wanted)]).to_pylist()
        by_query = defaultdict(list)
        for row in old:
            by_query[row["query"]].append(row)
            metadata[row["dense_row"]] = row
        pools, shortlists = {}, []
        for i,frame in enumerate(targets):
            q = int(retrieved["inverse"][i])
            control = sorted(by_query[int(original_inverse[frame["slot"]])],key=lambda r:r["rank"])
            routes = {"quarter":control}
            for route,method in enumerate(("angle30","crop75")):
                extra = [{**metadata[int(p)+offset],"sse":int(sse),"raw_mse":int(sse)/(768*255**2)}
                         for p,sse in zip(retrieved["positions"][route,q],retrieved["scores"][route,q])]
                routes[method] = merge(control,extra,config["top_k"])
            pools[frame["slot"]] = routes
            union = {}
            for method,rows in routes.items():
                for row in rows:
                    entry = union.setdefault(row["dense_row"],{"slot":frame["slot"],"dense_row":row["dense_row"],
                                               **{f"rank_{name}":None for name in LABELS}})
                    entry[f"rank_{method}"] = row["rank"]
            shortlists.extend(union.values())
        used = {r["dense_row"] for r in shortlists}
        metadata = {p:metadata[p] for p in used}
        write_table(root/"candidates.parquet",list(metadata.values()))
        write_table(root/"shortlists.parquet",shortlists)
        context = {"view_space_id":bank.state["bank_id"],"baseline_index_id":baseline["index_id"],
                   "baseline_dense_rows_preserved":True,"baseline_view_count":offset}
        manifest.update(status="scores",retrieval_seconds=retrieved["elapsed_seconds"],
                        unique_queries=retrieved["unique_queries"],frame_candidate_pairs=len(shortlists))
        write_json(root/"manifest.json",manifest)
        scored,audit = evaluate(root,run,source,targets,metadata,shortlists,
                               {"workers":config["search_threads"],"reuse_scores":[str(run)]},search_manifest=context)
        scores = {(r["slot"],r["dense_row"]):r["ms_ssim"] for r in scored}
        selected,choices = [],{}
        control = {r["slot"]:r["view_id"] for r in pq.read_table(run/"selected.parquet").to_pylist()}
        for frame in targets:
            slot = frame["slot"]
            choices[slot] = {}
            for method in LABELS:
                pool = [{**r,"ms_ssim":scores[slot,r["dense_row"]]} for r in pools[slot][method]]
                row = min(pool,key=lambda r:(-r["ms_ssim"],r["rank"],r["view_id"]))
                if method == "quarter" and row["view_id"] != control[slot]:
                    raise ValueError("Quarter-turn comparison must reproduce the baseline selection")
                choices[slot][method] = row
                selected.append({**row,"method":method,"slot":slot,"source_pts":frame["source_pts"],
                                 "source_time_base":frame["source_time_base"],"credit":frame_credit(row)})
        write_table(root/"selected.parquet",selected)
        photos = {r["view_id"]:r for r in selected}
        manifest["photos"] = cached_photos(run,root,photos.values(),source["config"],tuple(target["canonical_size"]))
        manifest["score_cache"] = audit
        manifest["summary"] = {method:{"changed_frames":sum(choices[f["slot"]][method]["view_id"] != control[f["slot"]] for f in targets),
                                      "mean_ms_ssim":float(np.mean([choices[f["slot"]][method]["ms_ssim"] for f in targets])),
                                      "affine_frames":sum(bool(choices[f["slot"]][method].get("transform_version")) for f in targets)}
                               for method in LABELS}
        manifest["status"] = "render"
        write_json(root/"manifest.json",manifest)
        temporary = root/"comparison.tmp.mp4"
        with (root/"encode.log").open("wb") as log:
            process = encoder(temporary,1920,1080,target["frame_rate"],
                              {"encoder_threads":4,"preset":"medium","crf":18},log)
            try:
                for i,frame in enumerate(targets):
                    image = comparison_frame(frame,choices[frame["slot"]],root,run,source["config"],target["count"])
                    process.stdin.write(image.tobytes())
                    if i == len(targets)//2:
                        image.save(root/"preview.png")
                process.stdin.close()
                if process.wait() != 0:
                    raise ValueError("Affine comparison encoding failed; see encode.log")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait()
        temporary.replace(root/"comparison.mp4")
        manifest.update(status="complete",video="comparison.mp4",playback_review="not_performed",
                        elapsed_seconds_last_run=round(time.perf_counter()-started,3),
                        video_duration_seconds=float(len(targets)/Fraction(target["frame_rate"])))
        write_json(root/"manifest.json",manifest)
        return manifest
