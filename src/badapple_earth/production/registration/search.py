"""Retrieve existing views by low-frequency Fourier magnitude, without shift expansion."""

import argparse
from datetime import datetime,timezone
from fractions import Fraction
import json
from pathlib import Path
import time

import numpy as np
from scipy.fft import rfft2

from ...collection.bundle import read_manifest
from ...collection.storage import writer_lock
from ...collection.windows import identity
from ...evidence import write_json
from ..silhouette.control import check_pause
from ..silhouette.search import keep

VERSION = "low-frequency-fourier-magnitude-r4-normalized-v1"


def descriptor(images):
    """Translation changes Fourier phase; retain low-frequency shape amplitudes."""
    values = np.asarray(images,np.float32)
    spectrum = rfft2(values,axes=(-2,-1),workers=1)
    yy,xx = np.meshgrid(np.arange(-4,5),np.arange(5),indexing="ij")
    active = (xx!=0)|(yy!=0)
    result = np.abs(spectrum[...,yy[active]%24,xx[active]]).astype(np.float32)
    result /= np.maximum(np.linalg.norm(result,axis=-1,keepdims=True),1e-7)
    return result


def retrieve(config,root):
    import pyarrow.parquet as pq
    root = Path(root)
    shape = Path(config["source"])
    source = read_manifest(shape)
    run = Path(source["config"]["run"])
    specifications = json.loads((shape/"retrieval/sources.json").read_text(encoding="utf-8"))
    targets = [r for r in pq.read_table(run/"targets/targets.parquet").to_pylist()
               if Fraction(r["source_pts"])*Fraction(r["source_time_base"])<Fraction(str(config["end_seconds"]))]
    slots = [r["slot"] for r in targets]
    pixels = np.load(run/"targets/gray_fine.npy",mmap_mode="r",allow_pickle=False)[slots]
    unique,inverse = np.unique(pixels.reshape(len(pixels),-1),axis=0,return_inverse=True)
    queries = descriptor(unique.reshape(-1,24,32))
    k = config["retrieval_k"]
    identifier = identity([VERSION,[s["identity"] for s in specifications],
                           read_manifest(run/"targets")["target_set_id"],slots,k])
    total = sum(s["rows"] for s in specifications)
    with writer_lock(root):
        file = root/"retrieval.npz"
        scores = np.full((len(unique),k),-np.inf,np.float32)
        positions = np.full(scores.shape,-1,np.int64)
        first,offset,previous = 0,0,0.
        if file.exists():
            with np.load(file,allow_pickle=False) as saved:
                if str(saved["search_id"])!=identifier:raise ValueError("Registration retrieval belongs to different frozen inputs")
                scores,positions = saved["scores"].copy(),saved["positions"].copy()
                first,offset,previous = int(saved["source"]),int(saved["offset"]),float(saved["elapsed_seconds"])
        if first==len(specifications):
            write_json(root/"manifest.json",{"status":"complete","search_id":identifier,"version":VERSION,
                       "view_count":total,"source_frames":len(targets),"unique_queries":len(unique),
                       "elapsed_seconds":round(previous,3)})
            return {"positions":positions[inverse],"inverse":inverse}
        initial = specifications[first]["start"]+offset
        started = time.perf_counter()
        def checkpoint(number,offset,completed):
            elapsed = previous+time.perf_counter()-started
            temporary = file.with_suffix(".tmp")
            with temporary.open("wb") as stream:
                np.savez(stream,scores=scores,positions=positions,inverse=inverse,search_id=identifier,
                         source=number,offset=offset,elapsed_seconds=elapsed)
            temporary.replace(file)
            seconds = time.perf_counter()-started
            progress = {"stage":"position-tolerant-retrieval","completed_views":completed,"total_views":total,
                        "unique_queries":len(unique),"elapsed_seconds":round(elapsed,3),
                        "remaining_seconds":round((total-completed)*seconds/max(completed-initial,1))}
            write_json(root/"progress.json",progress)
            print(f"Position-tolerant retrieval: {completed:,}/{total:,} views; {elapsed:.1f}s",flush=True)
            check_pause(Path(config["output"])/"pause.request")
        for number in range(first,len(specifications)):
            spec = specifications[number]
            values = np.load(spec["features"],mmap_mode="r",allow_pickle=False)
            if values.shape!=(spec["rows"],24,32) or values.dtype!=np.uint8:raise ValueError("Frozen view feature shape changed")
            begin = offset if number==first else 0
            for start in range(begin,len(values),config["candidate_batch"]):
                stop = min(start+config["candidate_batch"],len(values))
                features = descriptor(values[start:stop])
                addresses = np.arange(spec["start"]+start,spec["start"]+stop,dtype=np.int64)
                for q in range(0,len(queries),config["target_batch"]):
                    end = min(q+config["target_batch"],len(queries))
                    dot = queries[q:end]@features.T
                    keep(scores[q:end],positions[q:end],dot,addresses)
                if start==begin or stop==len(values) or (stop//config["candidate_batch"])%8==0:
                    next_source,next_offset = (number+1,0) if stop==len(values) else (number,stop)
                    checkpoint(next_source,next_offset,spec["start"]+stop)
        if np.any(positions<0):raise ValueError("Incomplete position-tolerant retrieval")
        write_json(root/"manifest.json",{"status":"complete","search_id":identifier,"version":VERSION,
                   "view_count":total,"source_frames":len(targets),"unique_queries":len(unique),
                   "finished_at":datetime.now(timezone.utc).isoformat()})
        return {"positions":positions[inverse],"inverse":inverse}


if __name__=="__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",default="config/registration-full.json")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    retrieve(config,Path(config["output"])/"retrieval")
