"""Reuse a previous native crop when it fully contains a new real-photo viewport."""

from collections import defaultdict
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image
import pyarrow.parquet as pq

from ...evidence import write_json
from ...matching.affine import image_view,VERSION as TRANSFORM_VERSION
from ..assets import save_image
from ..silhouette.control import check_pause
from .geometry import SIZE
from .selection import matrix


def relative_view(previous,current):
    old = np.vstack([np.asarray(matrix(previous)).reshape(2,3),[0,0,1]])
    new = np.vstack([np.asarray(matrix(current)).reshape(2,3),[0,0,1]])
    relative = np.linalg.solve(old,new)
    w,h = SIZE
    points = np.array([[0,0,1],[w,0,1],[w,h,1],[0,h,1]])@relative.T
    if (np.any(points[:,:2]<.5) or np.any(points[:,0]>w-.5) or np.any(points[:,1]>h-.5)):
        return None
    return relative[:2].ravel().tolist()


def reuse_native(root,baseline,selected,additional_sources=()):
    root,baseline = Path(root),Path(baseline)
    file = root/"native-reuse.json"
    if file.exists():return
    previous = defaultdict(list)
    seen = set()
    for source_run in (baseline,*map(Path,additional_sources)):
        for row in pq.read_table(source_run/"selected.parquet").to_pylist():
            key = str(source_run),row["view_id"]
            if key in seen:continue
            seen.add(key)
            path = source_run/"assets/selected"/(row["view_id"]+".png")
            if path.exists():previous[row["tile_id"]].append((row,path,source_run))
    destination = root/"assets/selected"
    destination.mkdir(parents=True,exist_ok=True)
    records = []
    done = set()
    @lru_cache(maxsize=8)
    def load(path):
        with Image.open(path) as image:return image.convert("RGB")
    for row in selected:
        if row["view_id"] in done or row.get("registration_parameters") is None:continue
        done.add(row["view_id"])
        check_pause(root/"pause.request")
        path = destination/(row["view_id"]+".png")
        for saved,source,source_run in previous[row["tile_id"]]:
            relative = relative_view(saved,row)
            if relative is None:continue
            if not path.exists():
                w,h = SIZE
                a,_,_,d,_,_ = matrix(row)
                _,_,sw,sh = row["window"]
                width = min(1444,max(1,round(np.hypot(a*sw,d*w*sh/h))))
                size = width,max(1,round(width*h/w))
                view = {"transform_version":TRANSFORM_VERSION,"view_affine":relative,
                        "transform_size":list(SIZE),"view_resampling":"bilinear"}
                save_image(image_view(load(source),view,size),path)
            records.append({"view_id":row["view_id"],"previous_view_id":saved["view_id"],
                            "previous_run":str(source_run),
                            "relative_affine":relative,"resampling":"bilinear"})
            break
    write_json(file,{"source_run":str(baseline),"reused_views":len(records),"records":records})
