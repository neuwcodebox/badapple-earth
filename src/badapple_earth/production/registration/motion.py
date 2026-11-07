"""Estimate source-video similarity motion for pose propagation and appearance costs."""

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

from ...evidence import write_json
from ..silhouette.control import check_pause

VERSION = "phase-initialized-ecc-similarity-source-motion-v1"


def estimate(previous,current):
    h,w = previous.shape
    normalizer = np.diag([w,h,1.])
    if previous.std()<1e-3 or current.std()<1e-3:return np.eye(3),0.
    shift,response = cv2.phaseCorrelate(previous,current)
    if not np.isfinite(shift).all():return np.eye(3),0.
    warp = np.array([[1,0,shift[0]],[0,1,shift[1]]],np.float32)
    if np.max(np.abs(np.asarray(shift)/[w,h]))>.25:return np.eye(3),0.
    try:
        score,affine = cv2.findTransformECC(previous,current,warp.copy(),cv2.MOTION_AFFINE,
                                          (cv2.TERM_CRITERIA_COUNT|cv2.TERM_CRITERIA_EPS,25,1e-3),None,3)
        u,s,v = np.linalg.svd(affine[:,:2])
        linear = (u@v)*np.mean(s)
        angle = abs(np.degrees(np.arctan2(linear[1,0],linear[0,0])))
        scale = np.sqrt(abs(np.linalg.det(linear)))
        center = np.array([w/2,h/2])
        translation = affine[:,:2]@center+affine[:,2]-linear@center
        projected = np.column_stack([linear,translation]).astype(np.float32)
        aligned = cv2.warpAffine(current,projected,(w,h),flags=cv2.INTER_LINEAR|cv2.WARP_INVERSE_MAP)
        valid = cv2.warpAffine(np.ones((h,w),np.uint8),projected,(w,h),flags=cv2.INTER_NEAREST|cv2.WARP_INVERSE_MAP)
        similarity_score = cv2.computeECC(previous,aligned,valid)
        if (.9<scale<1.1 and angle<10 and np.linalg.det(linear)>0 and similarity_score>.7):
            warp,response = projected,float(similarity_score)
    except (cv2.error,np.linalg.LinAlgError):pass
    matrix = np.vstack([warp,[0,0,1]])
    return np.linalg.inv(normalizer)@matrix@normalizer,float(np.clip(response,0,1))


def build(pixel_file,output,count,pause_file=None):
    output = Path(output)
    output.mkdir(parents=True,exist_ok=True)
    path = output/"source-motion.npz"
    if path.exists():
        with np.load(path) as saved:
            if str(saved["version"])!=VERSION or len(saved["transforms"])!=count:raise ValueError("Source motion belongs to other settings")
            if bool(saved["complete"]):return saved["transforms"].copy(),saved["confidence"].copy(),saved["segments"].copy()
            transforms,confidence,segments = saved["transforms"].copy(),saved["confidence"].copy(),saved["segments"].copy()
            start = int(saved["next_frame"])
    else:
        transforms = np.tile(np.eye(3),(count,1,1))
        confidence,segments = np.zeros(count),np.zeros(count,np.int32)
        start = 1
    pixels = np.load(pixel_file,mmap_mode="r",allow_pickle=False)
    def image(i):return cv2.resize(pixels[i],(64,48),interpolation=cv2.INTER_AREA).astype(np.float32)/255
    previous = image(start-1)
    def save(next_frame):
        temporary = path.with_suffix(".tmp")
        with temporary.open("wb") as stream:
            np.savez(stream,transforms=transforms,confidence=confidence,segments=segments,
                     next_frame=next_frame,complete=next_frame==count,version=VERSION)
        temporary.replace(path)
        write_json(output/"motion-progress.json",{"stage":"source-motion","completed_frames":next_frame,"total_frames":count})
        check_pause(pause_file)
    for i in range(start,count):
        current = image(i)
        if np.mean((current-previous)**2)>.18:
            segments[i] = segments[i-1]+1
        else:
            transforms[i],confidence[i] = estimate(previous,current)
            segments[i] = segments[i-1]
        previous = current
        if i%256==0:save(i+1)
    save(count)
    return transforms,confidence,segments


def propagation(parameters,relative):
    from .geometry import coefficients,parameters_from_matrix,SIZE
    transform = np.vstack([np.asarray(coefficients(parameters)).reshape(2,3),[0,0,1]])
    scale = np.diag([*SIZE,1.])
    forward = scale@relative@np.linalg.inv(scale)
    predicted = transform@np.linalg.inv(forward)
    result = parameters_from_matrix(predicted[:2].ravel())
    result[2] = np.clip(result[2],.35,1)
    return result


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default='config/tracking-full.json')
    args=parser.parse_args()
    config=json.loads(Path(args.config).read_text(encoding='utf-8'))
    import pyarrow.parquet as pq
    count=pq.ParquetFile(Path(config['baseline'])/'selected.parquet').metadata.num_rows
    cv2.setNumThreads(1)
    build(config['target_pixel_cache'],config['output'],count,Path(config['output'])/'pause.request')
