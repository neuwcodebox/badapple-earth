"""Share exact fine target features between processes through one read-only mmap."""

import os
from pathlib import Path
import numpy as np

from ...collection.bundle import read_manifest
from ...collection.windows import identity
from ...evidence import write_json
from .features import VERSION,load_target
from .control import check_pause


def pack(value,stream):
    if isinstance(value,np.ndarray):
        array = np.ascontiguousarray(value)
        offset = stream.tell()
        stream.write(array.tobytes())
        return {"dtype":array.dtype.str,"shape":list(array.shape),"offset":offset}
    if isinstance(value,(tuple,list)):
        return [pack(item,stream) for item in value]
    return value


def unpack(value,buffer):
    if isinstance(value,dict):
        return np.ndarray(value["shape"],dtype=value["dtype"],buffer=buffer,offset=value["offset"])
    if isinstance(value,list):
        return [unpack(item,buffer) for item in value]
    return value


def build(run,root,targets,pause_file):
    root.mkdir(exist_ok=True)
    cache_id = identity([VERSION,read_manifest(run/"targets")["target_set_id"],[r["slot"] for r in targets]])
    state = read_manifest(root) if (root/"manifest.json").exists() else {
        "cache_id":cache_id,"status":"building","records":{},"end":0}
    if state["cache_id"]!=cache_id:raise ValueError("Target cache belongs to other frames or metric")
    if state["status"]=="complete":return state
    binary = root/"features.bin"
    with binary.open("r+b" if binary.exists() else "w+b") as stream:
        stream.seek(state["end"])
        stream.truncate()
        def checkpoint():
            stream.flush()
            os.fsync(stream.fileno())
            state["end"] = stream.tell()
            write_json(root/"manifest.json",state)
            check_pause(pause_file)
        for frame in targets:
            key = str(frame["slot"])
            if key in state["records"]:continue
            state["records"][key] = pack(load_target(str((run/"targets"/frame["frame"]).resolve())),stream)
            if len(state["records"])%64==0:checkpoint()
        checkpoint()
    state["status"] = "complete"
    write_json(root/"manifest.json",state)
    return state


class TargetCache:
    def __init__(self,root):
        root = Path(root)
        state = read_manifest(root)
        if state["status"]!="complete":raise ValueError("Target feature cache must be complete")
        self.records = state["records"]
        self.buffer = np.memmap(root/"features.bin",mode="r",dtype=np.uint8)

    def __getitem__(self,slot):
        return unpack(self.records[str(slot)],self.buffer)
