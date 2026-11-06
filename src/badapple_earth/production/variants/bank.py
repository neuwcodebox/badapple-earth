"""A compact fine-feature bank; source RGB and metadata remain referenced."""

from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
from functools import lru_cache
import json
from multiprocessing import get_context
import os
from pathlib import Path
import shutil
import time

import numpy as np
from PIL import Image
import pyarrow as pa
import pyarrow.parquet as pq

from ...collection.bundle import members, read_manifest
from ...collection.windows import identity
from ...collection.storage import writer_lock
from ...evidence import write_json
from ...matching.affine import VERSION, variants, recipe, transform, metadata
from ...matching.photos import read_rgb_rows
from ..silhouette.control import check_pause

_OLD_RGB = {}


def initialize(refs):
    global _OLD_RGB
    rows = pq.read_table(refs).to_pylist()
    _OLD_RGB = {r["tile_id"]:(r["path"],r["row"]) for r in rows}


@lru_cache(maxsize=8)
def row_map(path):
    return np.load(path, mmap_mode="r", allow_pickle=False)


def build_group(job, specification, size):
    count = (job[-1]["stop"]-job[0]["start"])*len(specification)
    result = np.empty((count,24,32),np.uint8)
    coefficients = [recipe(size,v["angle"],v["scale"]) for v in specification]
    cursor = 0
    for block in job:
        offsets = row_map(block["row_map"])[block["packed_start"]:block["packed_stop"]]
        parquet = pq.ParquetFile(block["metadata"])
        rows = parquet.read(columns=[key for key in ("tile_id", "rgb_cache_ref", "rgb_cache_row")
                                     if key in parquet.schema_arrow.names], use_threads=False
                            ).take(pa.array(offsets)).to_pylist()
        groups = defaultdict(list)
        for i,row in enumerate(rows):
            if row.get("rgb_cache_ref"):
                file = str(Path(block["source"])/row["rgb_cache_ref"])
                position = row["rgb_cache_row"]
            else:
                if row["tile_id"] not in _OLD_RGB:
                    raise ValueError("A base photo has no local RGB; no automatic remote fallback")
                file,position = _OLD_RGB[row["tile_id"]]
            groups[file].append((i,position))
        images = {}
        for file,entries in groups.items():
            values = read_rgb_rows(file,[p for _,p in entries])
            images.update({i:values[p] for i,p in entries})
        for i in range(len(rows)):
            if images[i].shape != (size[1],size[0],3):
                raise ValueError("Local canonical RGB shape differs from the frozen index")
            # Nearest mapping commutes exactly with RGB -> Pillow luminance.
            # This produces the same pixels as RGB warp -> L -> BOX, at lower cost.
            gray = Image.fromarray(images[i]).convert("L")
            for coefficients_one in coefficients:
                value = transform(gray,coefficients_one,size).resize((32,24),Image.Resampling.BOX)
                result[cursor] = np.asarray(value)
                cursor += 1
    return job[0]["start"],job[-1]["stop"],result


class Bank:
    def __init__(self,root,config):
        self.root = Path(root)
        self.root.mkdir(parents=True,exist_ok=True)
        self.parent_index = Path(config["index"])
        parent = read_manifest(self.parent_index)
        self.specification = variants(config["angle_step"],config["crop_scale"])
        self.size = tuple(parent["canonical_size"])
        caches = [Path(p) for p in config["parent_packs"]]
        packed = [read_manifest(p) for p in caches]
        bank_id = identity([VERSION,parent["index_id"],[p["cache_id"] for p in packed],self.specification])
        path = self.root/"manifest.json"
        if path.exists():
            self.state = read_manifest_file(path)
            if self.state["bank_id"] != bank_id:
                raise ValueError("View bank inputs changed; use another bank path")
        else:
            sources = {m["index_id"]:(p,m) for p,m,_ in members(self.parent_index,parent)}
            descriptors,ids,missing = [],[],set()
            cursor = 0
            for cache,manifest in zip(caches,packed):
                if manifest["status"] != "complete":
                    raise ValueError("A parent packing is incomplete")
                values = np.load(cache/"ids.npy",mmap_mode="r",allow_pickle=False)
                for block in manifest["blocks"]:
                    if block["is_view"]:
                        continue
                    source,member = sources[block["member"]]
                    identifiers = np.asarray(values[block["start"]:block["stop"]],dtype="S64")
                    ids.append(identifiers)
                    stop = cursor+len(identifiers)
                    descriptors.append({"start":cursor,"stop":stop,"member":block["member"],
                                        "source":str(source.resolve()),
                                        "metadata":str((cache/block["path"]).resolve()),
                                        "row_map":str((cache/"source_rows.npy").resolve()),
                                        "packed_start":block["start"],"packed_stop":block["stop"]})
                    if not member.get("config",{}).get("cache_rgb"):
                        missing.update(x.decode() for x in identifiers)
                    cursor = stop
            all_ids = np.concatenate(ids)
            if cursor != parent["rows"]:
                raise ValueError("Parent packing must cover the frozen photo rows")
            np.save(self.root/"parent_ids.npy",all_ids,allow_pickle=False)
            ranks = np.empty(cursor,np.int32)
            ranks[np.argsort(all_ids,kind="stable")] = np.arange(cursor,dtype=np.int32)
            rows = cursor*len(self.specification)
            needed = rows*(768+4)+all_ids.nbytes
            if shutil.disk_usage(self.root).free < needed+config["min_free_disk_gib"]*1024**3:
                raise ValueError("Insufficient free disk for the complete view bank and reserve")
            features = np.lib.format.open_memmap(self.root/"gray_fine.npy",mode="w+",dtype=np.uint8,shape=(rows,24,32))
            del features
            tie = np.lib.format.open_memmap(self.root/"ties.npy",mode="w+",dtype=np.int32,shape=(rows,))
            for begin in range(0,cursor,65536):
                end = min(cursor,begin+65536)
                tie[begin*len(self.specification):end*len(self.specification)] = (
                    ranks[begin:end,None]*len(self.specification)+np.arange(len(self.specification),dtype=np.int32)).reshape(-1)
            tie.flush()
            del tie
            from ..assets import old_rgb_refs
            refs = old_rgb_refs(Path(config["extra_rgb_cache"]),missing)
            if set(refs) != missing:
                raise ValueError(f"Missing local RGB for {len(missing-set(refs))} existing places")
            records = [{"tile_id":key,"path":str(value[0].resolve()),"row":value[1]} for key,value in refs.items()]
            pq.write_table(pa.Table.from_pylist(records),self.root/"legacy-rgb.parquet",compression="zstd")
            self.state = {"schema":"fine-affine-view-bank-v1","bank_id":bank_id,
                          "parent_index_id":parent["index_id"],"parents":cursor,"rows":rows,
                          "size":list(self.size),"shape":[24,32],"variants":self.specification,
                          "descriptors":descriptors,"completed":[],"status":"building",
                          "network_reads":0,"transform_version":VERSION}
            write_json(path,self.state)
        # Immutable provenance is large; checkpoints contain only progress.
        # Migrate the existing paused bank atomically before removing the copy.
        inventory = self.root/"inventory.json"
        if "descriptors" in self.state:
            write_json(inventory, {"bank_id":bank_id,"descriptors":self.state.pop("descriptors")})
            write_json(path,self.state)
        listing = read_manifest_file(inventory)
        if listing["bank_id"] != bank_id:
            raise ValueError("View bank inventory belongs to different inputs")
        self.descriptors = listing["descriptors"]
        self.jobs = [self.descriptors[i:i+16] for i in range(0,len(self.descriptors),16)]
        self.values = np.load(self.root/"gray_fine.npy",mmap_mode="r+",allow_pickle=False)
        self.ties = np.load(self.root/"ties.npy",mmap_mode="r",allow_pickle=False)
        self.ids = np.load(self.root/"parent_ids.npy",mmap_mode="r",allow_pickle=False)

    def save_group(self,number,start,stop,values):
        if (start, stop) != (self.jobs[number][0]["start"], self.jobs[number][-1]["stop"]):
            raise ValueError("Feature group does not match its checkpoint range")
        begin,end = start*len(self.specification),stop*len(self.specification)
        if values.shape != (end-begin,24,32):
            raise ValueError("Feature group has the wrong number of views")
        self.values[begin:end] = values
        self.values.flush()
        if number not in self.state["completed"]:
            self.state["completed"].append(number)
        self.state["completed_views"] = sum((self.jobs[i][-1]["stop"]-self.jobs[i][0]["start"])*len(self.specification)
                                              for i in self.state["completed"])
        if len(self.state["completed"]) == len(self.jobs):
            self.state["status"] = "complete"
        else:
            self.state["status"] = "building"
        write_json(self.root/"manifest.json",self.state)

    def resolve(self,positions,offset):
        stops = np.array([d["stop"] for d in self.descriptors])
        groups = defaultdict(list)
        for position in sorted(set(int(p) for p in positions)):
            parent = position//len(self.specification)
            groups[int(np.searchsorted(stops,parent,side="right"))].append((position,parent))
        result,scenes = {},{}
        for number,entries in groups.items():
            block = self.descriptors[number]
            mapping = row_map(block["row_map"])
            indices = sorted({p-block["start"] for _,p in entries})
            original = [int(mapping[block["packed_start"]+i]) for i in indices]
            records = pq.read_table(block["metadata"]).take(pa.array(original)).to_pylist()
            records = dict(zip(indices,records))
            for position,parent in entries:
                row = records[parent-block["start"]]
                if row["tile_id"] != self.ids[parent].decode():
                    raise ValueError("Parent feature and provenance mappings disagree")
                key = block["source"],row["scene_id"]
                if key not in scenes:
                    scenes[key] = read_manifest_file(Path(block["source"])/"catalog"/(row["scene_id"]+".json"))
                variant = self.specification[position%len(self.specification)]
                result[position+offset] = {**metadata(row,scenes[key],self.size,variant),
                                           "index_member":block["member"],"dense_row":position+offset}
        return result


def read_manifest_file(path):
    # Windows may briefly deny opening the name while its writer atomically
    # replaces it. Retry only this transient access error, with a finite bound.
    for attempt in range(8):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(min(.05*2**attempt,.5))


def build(config):
    """Resume feature generation with bounded worker results and published groups."""
    workers = config["workers"]
    if workers < 1:
        raise ValueError("Feature generation needs at least one worker")
    pause_file = config.get("pause_file")
    check_pause(pause_file)
    names = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "NUMBA_NUM_THREADS")
    previous = {key:os.environ.get(key) for key in names}
    root = Path(config["bank"])
    try:
        for key in names:
            os.environ[key] = "1"
        with writer_lock(root):
            bank = Bank(root,config)
            if len(bank.state["completed"]) == len(bank.jobs):
                return bank.state
            bank.state.update(status="building",workers=workers,started_at=datetime.now(timezone.utc).isoformat())
            for key in ("paused_at", "pause_reason"):
                bank.state.pop(key,None)
            write_json(root/"manifest.json",bank.state)
            tasks = iter(i for i in range(len(bank.jobs)) if i not in bank.state["completed"])
            started, initial = time.perf_counter(), bank.state.get("completed_views",0)
            try:
                with ProcessPoolExecutor(max_workers=workers,mp_context=get_context("spawn"),
                        initializer=initialize,initargs=(str((root/"legacy-rgb.parquet").resolve()),)) as pool:
                    pending = {}
                    for _ in range(workers):
                        number = None if pause_file and Path(pause_file).exists() else next(tasks,None)
                        if number is not None:
                            pending[pool.submit(build_group,bank.jobs[number],bank.specification,bank.size)] = number
                    while pending:
                        done,_ = wait(pending,return_when=FIRST_COMPLETED)
                        for future in done:
                            number = pending.pop(future)
                            begin,end,values = future.result()
                            bank.save_group(number,begin,end,values)
                            elapsed = time.perf_counter()-started
                            rate = (bank.state["completed_views"]-initial)/max(elapsed,.001)
                            write_json(root/"build-progress.json",{
                                "completed_views":bank.state["completed_views"],"total_views":bank.state["rows"],
                                "views_per_second":round(rate,2),"remaining_seconds":round((bank.state["rows"]-bank.state["completed_views"])/rate),
                                "updated_at":datetime.now(timezone.utc).isoformat()})
                            print(f"Affine features: {bank.state['completed_views']:,}/{bank.state['rows']:,}; {rate:,.0f} views/s",flush=True)
                            number = None if pause_file and Path(pause_file).exists() else next(tasks,None)
                            if number is not None:
                                pending[pool.submit(build_group,bank.jobs[number],bank.specification,bank.size)] = number
                check_pause(pause_file)
                bank.state["finished_at"] = datetime.now(timezone.utc).isoformat()
                write_json(root/"manifest.json",bank.state)
                return bank.state
            except BaseException as error:
                bank.state.update(status="paused" if isinstance(error,(KeyboardInterrupt,InterruptedError)) else "failed",
                                  error_type=type(error).__name__)
                write_json(root/"manifest.json",bank.state)
                raise
    finally:
        for key,value in previous.items():
            if value is None:
                os.environ.pop(key,None)
            else:
                os.environ[key] = value
