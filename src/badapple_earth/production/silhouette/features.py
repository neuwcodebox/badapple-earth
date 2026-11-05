"""Compare shapes through both regions and visible changes across their boundary."""

from functools import lru_cache

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter

from ..correlation.features import fine as gradient_features

VERSION = "balanced-regions-signed-gradient-visible-boundary-v1"


def balanced_weights(gray):
    values = np.asarray(gray,dtype=np.float32).reshape(-1)
    white = values > .5
    if white.any() and not white.all():
        weights = np.where(white,.5/white.sum(),.5/(len(white)-white.sum())).astype(np.float32)
    else:
        weights = np.full(len(values),1/len(values),np.float32)
    return weights


def boundary_pairs(gray):
    """Pairs cross the target's white/black boundary at three local radii."""
    mask = gray > .5
    boundary = np.zeros_like(mask)
    vertical,horizontal = mask[1:] != mask[:-1],mask[:,1:] != mask[:,:-1]
    boundary[1:] |= vertical
    boundary[:-1] |= vertical
    boundary[:,1:] |= horizontal
    boundary[:,:-1] |= horizontal
    smooth = gaussian_filter(mask.astype(np.float32),sigma=.75,mode="reflect")
    gy,gx = np.gradient(smooth)
    length = np.hypot(gx,gy)
    yy,xx = np.nonzero(boundary & (length > 1e-5))
    nx,ny = gx[yy,xx]/length[yy,xx],gy[yy,xx]/length[yy,xx]
    whites,blacks,points = [],[],[]
    h,w = mask.shape
    for radius in (1,2,4):
        wx,wy = np.rint(xx+radius*nx).astype(int),np.rint(yy+radius*ny).astype(int)
        bx,by = np.rint(xx-radius*nx).astype(int),np.rint(yy-radius*ny).astype(int)
        valid = (wx>=0)&(wx<w)&(wy>=0)&(wy<h)&(bx>=0)&(bx<w)&(by>=0)&(by<h)
        p = np.flatnonzero(valid)
        p = p[mask[wy[p],wx[p]] & ~mask[by[p],bx[p]]]
        whites.extend((wy[p]*w+wx[p]).tolist())
        blacks.extend((by[p]*w+bx[p]).tolist())
        points.extend(p.tolist())
    if not points:
        return None
    _,inverse = np.unique(points,return_inverse=True)
    return np.array(whites),np.array(blacks),inverse,np.bincount(inverse)


def scales(gray):
    image = Image.fromarray(np.asarray(gray,np.float32))
    return [np.asarray(image.resize((w,round(w*image.height/image.width)),Image.Resampling.BOX))
            for w in (64,128)]


def target_features(gray):
    result = []
    for image in scales(gray):
        result.append((image.reshape(-1),balanced_weights(image),boundary_pairs(image)))
    _,gradient = gradient_features(gray)
    mask = np.asarray(gray) > .5
    return result,gradient,mask, bool(np.std(gray)<.005 or not mask.any() or mask.all())


def photo_features(gray):
    _,gradient = gradient_features(gray)
    return [image.reshape(-1) for image in scales(gray)],gradient


def evaluate(target,photo,original,config):
    targets,tg,mask,flat = target
    photos,pg = photo
    mse,edge,coverage,reverse = [],[],[],[]
    for (values,weights,pairs),pixels in zip(targets,photos):
        mse.append(float(np.sum(weights*(pixels-values)**2,dtype=np.float64)))
        if pairs is None:
            continue
        white,black,point,counts = pairs
        contrast = np.bincount(point,weights=pixels[white]-pixels[black])/counts
        edge.append(float(np.mean(np.clip(contrast/config["edge_contrast_scale"],0,1))))
        coverage.append(float(np.mean(contrast>=config["edge_contrast_min"])))
        reverse.append(float(np.mean(contrast<=-.02)))
    gc = float(np.clip(tg@pg,-1,1))
    region = 1-float(np.mean(mse))
    edge_score = float(np.mean(edge)) if edge else 0.
    region_contrast = float(original[mask].mean()-original[~mask].mean()) if mask.any() and not mask.all() else 0.
    result = {"balanced_mse":1-region,"gc":gc,"boundary_contrast":edge_score,
              "boundary_coverage":float(np.mean(coverage)) if coverage else 0.,
              "boundary_reversed":float(np.mean(reverse)) if reverse else 0.,"region_contrast":region_contrast}
    result["shape_score"] = (config["weights"][0]*region+config["weights"][1]*max(gc,0)
                             +config["weights"][2]*edge_score)
    result["visible"] = (not flat and region_contrast>=config["region_contrast_min"]
                         and result["boundary_coverage"]>=config["coverage_min"]
                         and result["boundary_reversed"]<=config["reversed_max"] and gc>0)
    return result


@lru_cache(maxsize=1024)
def load_target(path):
    with Image.open(path) as image:
        gray = np.asarray(image.convert("L"),np.float32)/255
    return target_features(gray)
