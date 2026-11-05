"""Same-frame four-panel comparison of current and silhouette-aware selection."""

from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path
import time

import numpy as np
from PIL import Image, ImageDraw
import pyarrow.parquet as pq

from ...collection.bundle import read_manifest
from ...collection.storage import writer_lock
from ...collection.windows import identity
from ...evidence import write_json
from ...matching.output import source_credits
from ..assets import cached_photos, frame_credit
from ..layout import BG, INK, MUTED, font, resized, timestamp
from ..render import encoder
from ..variants.pipeline import write_table
from .features import VERSION, load_target
from .search import search, sources
from .evaluation import resolve_candidates, score

LABELS = {"current":"CURRENT / MSE -> MS-SSIM","balanced":"AREA BALANCED",
          "visible":"SHAPE + VISIBLE CONTRAST"}


def intervals(config):
    result = config.get("intervals",[{"name":"comparison","start":config.get("start",0),
                                        "duration":config.get("duration",0)}])
    previous = Fraction(0)
    names = set()
    for item in result:
        begin = Fraction(str(item["start"]))
        end = begin+Fraction(str(item["duration"]))
        name = item["name"]
        if (begin<previous or begin>=end or end>Fraction("218.5")
                or not name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name)
                or name in names):
            raise ValueError("Use ordered nonoverlapping named intervals before the credits cutoff")
        previous = end
        names.add(name)
    if not result:
        raise ValueError("At least one silhouette comparison interval is required")
    return result


def interval_frames(targets,item):
    begin = Fraction(str(item["start"]))
    end = begin+Fraction(str(item["duration"]))
    return [r for r in targets if begin<=Fraction(r["source_pts"])*Fraction(r["source_time_base"])<end]


def choose(rows,baseline_position,flat=False):
    current = next(r for r in rows if r["dense_row"]==baseline_position)
    balanced = min(rows,key=lambda r:(r["balanced_mse"],r["view_id"]))
    eligible = [r for r in rows if r["visible"]]
    if flat or not eligible:
        visible,reason = current,"flat_target" if flat else "no_visible_candidate"
    else:
        visible = min(eligible,key=lambda r:(-r["shape_score"],r["balanced_mse"],r["view_id"]))
        reason = None
    return {"current":current,"balanced":balanced,"visible":visible},reason


def comparison_frame(target,chosen,root,run,config,total,baseline_score,layout="four-panel"):
    canvas = Image.new("RGB",(1920,1080),BG)
    draw = ImageDraw.Draw(canvas)
    if layout == "pair":
        draw.text((24,32),LABELS["current"],fill=INK,font=font(config["font_bold"],25))
        draw.text((1110,32),LABELS["visible"],fill=INK,font=font(config["font_bold"],25))
        canvas.paste(resized(str(run/"targets"/target["frame"]),192,143),(864,8))
        draw.text((920,156),"SOURCE",fill=MUTED,font=font(config["font_regular"],15))
        for i,method in enumerate(("current","visible")):
            row = chosen[method]
            x = 24+960*i
            canvas.paste(resized(str(root/"photos"/(row["view_id"]+".png")),912,680),(x,190))
            detail = (f"MS-SSIM {baseline_score:.4f} / MSE TOP 20" if method=="current"
                      else f"Shape {row['shape_score']:.4f} / boundary {row['boundary_coverage']:.0%}")
            if row.get("fallback"):
                detail += " / baseline fallback"
            draw.text((x,890),detail,fill=MUTED,font=font(config["font_regular"],18))
            draw.text((x,922),frame_credit(row),fill=MUTED,font=font(config["font_regular"],13))
        draw.text((24,1030),f"{timestamp(target['target_time'])}    {target['slot']+1:,} / {total:,}",
                  fill=MUTED,font=font(config["font_regular"],18))
        return canvas
    panels = [("SOURCE",run/"targets"/target["frame"],None,None)]
    for method,label in LABELS.items():
        row = chosen[method]
        if method=="current":
            detail = f"MS-SSIM {baseline_score:.4f} / MSE TOP 20"
        elif method=="balanced":
            detail = f"Balanced MSE {row['balanced_mse']:.4f} / lower is better"
        else:
            detail = f"Shape {row['shape_score']:.4f} / boundary {row['boundary_coverage']:.0%}"
            if row.get("fallback"):
                detail += " / baseline fallback"
        panels.append((label,root/"photos"/(row["view_id"]+".png"),detail,row))
    for i,(label,path,detail,row) in enumerate(panels):
        x,y = 24+(i%2)*960,12+(i//2)*522
        draw.text((x,y),label,fill=INK,font=font(config["font_bold"],22))
        canvas.paste(resized(str(path),608,454),(x+152,y+34))
        if detail:
            draw.text((x,y+490),detail,fill=MUTED,font=font(config["font_regular"],15))
            draw.text((x,y+510),frame_credit(row),fill=MUTED,font=font(config["font_regular"],10))
    draw.text((24,1060),f"{timestamp(target['target_time'])}    {target['slot']+1:,} / {total:,}",
              fill=MUTED,font=font(config["font_regular"],14))
    return canvas


def compare(config,output):
    run,root = Path(config["run"]),Path(output)
    source,target,baseline_search = read_manifest(run),read_manifest(run/"targets"),read_manifest(run/"search")
    if source["status"]!="complete" or baseline_search["status"]!="complete":
        raise ValueError("Silhouette comparison needs a completed source production")
    clips = intervals(config)
    if config.get("layout","four-panel") not in ("four-panel","pair"):
        raise ValueError("Unknown silhouette comparison layout")
    all_targets = pq.read_table(run/"targets/targets.parquet").to_pylist()
    targets = [r for clip in clips for r in interval_frames(all_targets,clip)]
    if any(not interval_frames(targets,clip) for clip in clips):
        raise ValueError("Each comparison interval must contain original frames")
    experiment_id = identity([config,source["production_id"],VERSION])
    specifications = sources(config)
    with writer_lock(root):
        if (root/"manifest.json").exists():
            saved = read_manifest(root)
            if saved["experiment_id"]!=experiment_id:
                raise ValueError("Use another output path for changed silhouette settings")
            if saved["status"]=="complete":
                return saved
        manifest = {"experiment_id":experiment_id,"status":"retrieval","config":config,"version":VERSION,
                    "source_frames":len(targets),"source_view_count":sum(s["rows"] for s in specifications),
                    "source_places":baseline_search["candidate_count"],"target_set_id":target["target_set_id"],
                    "first_slot":targets[0]["slot"],"last_slot":targets[-1]["slot"],"frame_rate":target["frame_rate"],
                    "audio_mode":"silent","network_reads":0,"tie_break":"score then frozen global dense row",
                    "source_run":str(run),"retrieval_routes":["balanced-mse-32","signed-gradient-16+32"]}
        write_json(root/"manifest.json",manifest)
        started = time.perf_counter()
        retrieved = search(run,root,config,targets,specifications)
        manifest.update(status="metadata",retrieval_seconds=retrieved["elapsed_seconds"],unique_queries=retrieved["unique_queries"])
        write_json(root/"manifest.json",manifest)
        inverse = np.load(run/"search/query_inverse.npy",allow_pickle=False)
        queries = sorted({int(inverse[r["slot"]]) for r in targets})
        original = pq.read_table(run/"search/matches.parquet",columns=["query","rank","dense_row"],
                                  filters=[("query","in",queries)]).to_pylist()
        base = defaultdict(list)
        for row in original:
            base[row["query"]].append(row)
        shortlists = []
        for i,frame in enumerate(targets):
            union = {}
            for route,name in enumerate(("region","outline")):
                for rank,position in enumerate(retrieved["positions"][route,i],1):
                    row = union.setdefault(int(position),{"slot":frame["slot"],"dense_row":int(position),
                                                        "rank_region":None,"rank_outline":None,"baseline_rank":None})
                    row["rank_"+name] = rank
            for original in base[int(inverse[frame["slot"]])]:
                row = union.setdefault(original["dense_row"],{"slot":frame["slot"],"dense_row":original["dense_row"],
                                                             "rank_region":None,"rank_outline":None,"baseline_rank":None})
                row["baseline_rank"] = original["rank"]
            shortlists.extend(union.values())
        write_table(root/"shortlists.parquet",shortlists)
        positions = {r["dense_row"] for r in shortlists}
        metadata = resolve_candidates(root,run,specifications,positions,config["workers"])
        manifest.update(status="fine-scores",candidate_views=len(metadata),frame_candidate_pairs=len(shortlists))
        write_json(root/"manifest.json",manifest)
        print(f"Silhouette shortlist: {len(shortlists):,} frame/view pairs; {len(metadata):,} unique views",flush=True)
        measured = score(root,run,targets,metadata,shortlists,config)
        by_slot = defaultdict(list)
        for row in measured:
            by_slot[row["slot"]].append({**metadata[row["dense_row"]],**row})
        control = {r["slot"]:r for r in pq.read_table(run/"selected.parquet").to_pylist()}
        choices,selected,reasons = {},[],{}
        for frame in targets:
            slot = frame["slot"]
            flat = load_target(str((run/"targets"/frame["frame"]).resolve()))[-1]
            chosen,reason = choose(by_slot[slot],control[slot]["dense_row"],flat)
            reasons[slot] = reason
            choices[slot] = {method:{**row,"fallback":reason if method=="visible" else None}
                             for method,row in chosen.items()}
            if choices[slot]["current"]["view_id"] != control[slot]["view_id"]:
                raise ValueError("Silhouette current control must reproduce the completed video")
            for method,row in choices[slot].items():
                selected.append({**row,"method":method,"slot":slot,"source_pts":frame["source_pts"],
                                 "source_time_base":frame["source_time_base"]})
        write_table(root/"selected.parquet",selected)
        photos = {r["view_id"]:r for r in selected}
        manifest["photos"] = cached_photos(run,root,photos.values(),source["config"],tuple(target["canonical_size"]))
        credits,references = source_credits(photos.values())
        write_json(root/"credits.json",{"source":credits,"references":references,"selected":"selected.parquet"})
        manifest["summary"] = {method:{"changed_frames":sum(choices[f["slot"]][method]["view_id"]!=control[f["slot"]]["view_id"] for f in targets),
                       "mean_balanced_mse":float(np.mean([choices[f["slot"]][method]["balanced_mse"] for f in targets])),
                       "mean_boundary_coverage":float(np.mean([choices[f["slot"]][method]["boundary_coverage"] for f in targets])),
                       "selected_places":len({choices[f["slot"]][method]["tile_id"] for f in targets})}
                    for method in LABELS}
        manifest["fallback_frames"] = dict(Counter(reason for reason in reasons.values() if reason))
        manifest["status"] = "render"
        write_json(root/"manifest.json",manifest)
        videos = []
        for clip in clips:
            frames = interval_frames(targets,clip)
            name = clip["name"]
            temporary = root/(name+".tmp.mp4")
            with (root/(name+"-encode.log")).open("wb") as log:
                process = encoder(temporary,1920,1080,target["frame_rate"],
                              {"encoder_threads":4,"preset":"medium","crf":18},log)
                try:
                    for i,frame in enumerate(frames):
                        image = comparison_frame(frame,choices[frame["slot"]],root,run,source["config"],target["count"],control[frame["slot"]]["selection_score"],config.get("layout","four-panel"))
                        process.stdin.write(image.tobytes())
                        if i==len(frames)//2:
                            image.save(root/("preview.png" if name=="comparison" else name+"-preview.png"))
                    process.stdin.close()
                    if process.wait()!=0:
                        raise ValueError(f"Silhouette comparison encoding failed; see {name}-encode.log")
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait()
            temporary.replace(root/(name+".mp4"))
            ids = [f["slot"] for f in frames]
            clip_selected = {method:[choices[i][method] for i in ids] for method in LABELS}
            videos.append({**clip,"video":name+".mp4","frames":len(frames),
                           "video_duration_seconds":float(len(frames)/Fraction(target["frame_rate"])),
                           "fallback_frames":dict(Counter(reasons[i] for i in ids if reasons[i])),
                           "transitions":{method:sum(a["view_id"]!=b["view_id"] for a,b in zip(rows,rows[1:]))
                                          for method,rows in clip_selected.items()}})
        manifest.update(status="complete",videos=videos,video=videos[0]["video"],
                        video_duration_seconds=videos[0]["video_duration_seconds"],
                        total_video_duration_seconds=sum(v["video_duration_seconds"] for v in videos),
                        elapsed_seconds_last_run=round(time.perf_counter()-started,3),playback_review="not_performed",
                        interpretation="experimental weights and visibility thresholds; metrics do not establish perceptual improvement")
        write_json(root/"manifest.json",manifest)
        return manifest
