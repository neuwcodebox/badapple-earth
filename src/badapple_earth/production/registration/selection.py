"""Choose real photos and camera poses jointly, following motion in the source video."""

from collections import defaultdict

import cv2
import numpy as np

from ...matching.views import rotation_recipe
from .geometry import SIZE,image_points
from ..silhouette.features import balanced_weights


def matrix(row):
    if row.get("view_affine"):return row["view_affine"]
    angle = row["rotation_degrees"]
    w,h = SIZE
    left,top,right,bottom = rotation_recipe(SIZE,angle)
    sx,sy = (right-left)/w,(bottom-top)/h
    if angle==0:return [sx,0,left,0,sy,top]
    if angle==90:return [0,sy,top,-sx,0,h-left]
    if angle==180:return [-sx,0,w-left,0,-sy,h-top]
    return [0,-sy,w-top,sx,0,left]


def source_motion(pixels):
    motion,confidence = [np.zeros(2)],[0.]
    for previous,current in zip(pixels,pixels[1:]):
        if previous.std()<.01 or current.std()<.01:
            shift,response = (0.,0.),0.
        else:
            shift,response = cv2.phaseCorrelate(previous.astype(np.float32),current.astype(np.float32))
        displacement = np.asarray(shift)/np.array([pixels.shape[2],pixels.shape[1]])
        if not np.isfinite(displacement).all() or np.max(np.abs(displacement))>.35:
            displacement,response = np.zeros(2),0.
        motion.append(displacement)
        confidence.append(float(np.clip(response,0,1)))
    return np.asarray(motion),np.asarray(confidence)


def reduce_options(options,baselines,config):
    pools = defaultdict(dict)
    for row in [*baselines,*options]:
        slot = row["slot"]
        if not row.get("visible",False) and row["view_id"]!=baselines[slot]["view_id"]:continue
        old = pools[slot].get(row["view_id"])
        if old is None or row["shape_score"]>old["shape_score"]:pools[slot][row["view_id"]] = row
    result = []
    for baseline in baselines:
        rows = sorted(pools[baseline["slot"]].values(),key=lambda r:(-r["shape_score"],r["balanced_mse"],r["view_id"]))
        if baseline.get("fallback")=="flat_target":rows = [baseline]
        elif not baseline.get("visible",False):
            valid = [r for r in rows if r.get("visible",False)]
            if valid:
                cutoff = valid[0]["shape_score"]-config["shape_tolerance"]
                rows = [r for r in valid if r["shape_score"]>=cutoff][:config["max_candidates"]]
            else:rows = [baseline]
        else:
            cutoff = max(rows[0]["shape_score"]-config["shape_tolerance"],baseline["shape_score"])
            rows = [r for r in rows if r["shape_score"]>=cutoff][:config["max_candidates"]]
            if not any(r["view_id"]==baseline["view_id"] for r in rows):rows.append(baseline)
        result.append(rows)
    return result


def display_options(options,selected):
    """Show five distinct evaluated places, preserving the exact selected pose."""
    places = defaultdict(dict)
    key = lambda r:(-r["shape_score"],r["balanced_mse"],r["view_id"])
    for row in options:
        old = places[row["slot"]].get(row["tile_id"])
        if old is None or key(row)<key(old):places[row["slot"]][row["tile_id"]] = row
    result = []
    for choice in selected:
        candidates = places[choice["slot"]]
        rows = [r for r in candidates.values() if r["tile_id"]!=choice["tile_id"]]
        if len(rows)<4:
            raise ValueError(f"Five distinct evaluated places required at slot {choice['slot']}; change the layout if unavailable")
        result.append(sorted([choice,*sorted(rows,key=key)[:4]],key=key))
    return result


def choose(pools,pixels,config,motion_transforms=None,confidence=None):
    if motion_transforms is None:
        motion,confidence = source_motion(pixels)
        motion_transforms = np.tile(np.eye(3),(len(pixels),1,1))
        motion_transforms[:,:2,2] = motion
    backs,costs,previous = [],None,None
    for index,rows in enumerate(pools):
        features = np.asarray([r["luminance"] for r in rows],np.float32)
        appearance = np.asarray([r["appearance"] for r in rows],np.float32).reshape(-1,24,32)/255 if config.get("appearance_weight",0) else None
        poses = np.asarray([image_points(matrix(r)) for r in rows],np.float64)
        best = max(r["shape_score"] for r in rows)
        unary = np.asarray([best-r["shape_score"] for r in rows])
        if costs is None:
            costs = unary
            backs.append(None)
        else:
            differences = previous_features[:,None,:]-features[None,:,:]
            edges = config["continuity_weight"]*np.sum(differences*differences,axis=-1)
            same = np.asarray([[a["tile_id"]==b["tile_id"] for b in rows] for a in previous])
            forward = motion_transforms[index]
            predicted = previous_poses@forward[:2,:2].T+forward[:2,2]
            residual = poses[None,:,:,:]-predicted[:,None,:,:]
            edges += same*config["motion_weight"]*confidence[index]*np.mean(residual*residual,axis=(-1,-2))
            if appearance is not None and confidence[index]>0:
                scale = np.diag([32,24,1.])
                warp = (scale@forward@np.linalg.inv(scale))[:2].astype(np.float32)
                valid = cv2.warpAffine(np.ones((24,32),np.float32),warp,(32,24),flags=cv2.INTER_LINEAR)>.999
                weights = balanced_weights(pixels[index].astype(np.float32)/255)*valid.ravel()
                weights /= max(weights.sum(),1e-10)
                moving = np.stack([cv2.warpAffine(p,warp,(32,24),flags=cv2.INTER_LINEAR) for p in previous_appearance]).reshape(-1,768)
                current = appearance.reshape(-1,768)
                differences = (moving*moving)@weights[:,None]+((current*current)@weights)[None,:]-2*(moving*weights)@current.T
                edges += config["appearance_weight"]*confidence[index]*np.maximum(differences,0)
            total = costs[:,None]+edges
            parent = np.argmin(total,axis=0)
            costs = unary+total[parent,np.arange(len(rows))]
            backs.append(parent)
        previous,previous_features,previous_poses = rows,features,poses
        previous_appearance = appearance
    selected = [int(np.argmin(costs))]
    for back in reversed(backs[1:]):selected.append(int(back[selected[-1]]))
    return [rows[i] for rows,i in zip(pools,reversed(selected))]
