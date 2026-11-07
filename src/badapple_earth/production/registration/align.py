"""SIMD template search over valid pans, followed by bounded similarity refinement."""

from functools import lru_cache
import math

import cv2
import numpy as np
from PIL import Image
from scipy.optimize import least_squares

from .geometry import SIZE,coefficients,maximum_scale,parameters_from_matrix

cv2.setNumThreads(1)
TARGET_PIXELS = None


def set_target_cache(path):
    global TARGET_PIXELS
    TARGET_PIXELS = np.load(path,mmap_mode="r",allow_pickle=False) if path else None
    target_image.cache_clear()
    template.cache_clear()


@lru_cache(maxsize=512)
def target_image(path):
    if TARGET_PIXELS is not None:
        from pathlib import Path
        slot = int(Path(path).stem)
        return Image.fromarray(TARGET_PIXELS[slot])
    with Image.open(path) as im:return im.convert("L")


@lru_cache(maxsize=8192)
def template(path,width,height):
    pixels = np.asarray(target_image(path).resize((width,height),Image.Resampling.BOX),np.float32)/255
    white = pixels>.5
    weights = np.where(white,.5/max(white.sum(),1),.5/max((~white).sum(),1)).astype(np.float32)
    gx = cv2.Sobel(pixels,cv2.CV_32F,1,0,ksize=3)
    gy = cv2.Sobel(pixels,cv2.CV_32F,0,1,ksize=3)
    norm = float(np.sum(gx*gx+gy*gy))
    return pixels,weights,gx,gy,norm


def prepared(gray,angle):
    h,w = gray.shape
    rotation = cv2.getRotationMatrix2D((w/2,h/2),-angle,1)
    c,s = abs(rotation[0,0]),abs(rotation[0,1])
    ow,oh = math.ceil(c*w+s*h),math.ceil(s*w+c*h)
    rotation[:,2] += np.array([ow/2-w/2,oh/2-h/2])
    pixels = cv2.warpAffine(gray,rotation,(ow,oh),flags=cv2.INTER_LINEAR)
    mask = cv2.warpAffine(np.ones(gray.shape,np.uint8),rotation,(ow,oh),flags=cv2.INTER_NEAREST)
    integral = cv2.integral(mask,sdepth=cv2.CV_32S)
    gx = cv2.Sobel(pixels,cv2.CV_32F,1,0,ksize=3)
    gy = cv2.Sobel(pixels,cv2.CV_32F,0,1,ksize=3)
    energy = cv2.integral(gx*gx+gy*gy,sdepth=cv2.CV_64F)
    return pixels,integral,gx,gy,energy,cv2.invertAffineTransform(rotation)


def rectangle_sum(integral,w,h):
    return integral[h:,w:]-integral[:-h,w:]-integral[h:,:-w]+integral[:-h,:-w]


def coarse_search(gray,target_path,angle,config):
    """Every valid integer translation is searched, without padding in a result."""
    width = config["coarse_width"]
    height = round(width*SIZE[1]/SIZE[0])
    source = cv2.resize(gray,(width,height),interpolation=cv2.INTER_AREA)
    best = (-np.inf,None)
    for delta in config["angle_offsets"]:
        degrees = angle+delta
        pixels,integral,gx,gy,energy,inverse = prepared(source,degrees)
        squared = pixels*pixels
        for zoom in config["crop_scales"]:
            scale = maximum_scale(degrees)*zoom
            w,h = max(8,round(width*scale)),max(6,round(height*scale))
            if w>pixels.shape[1] or h>pixels.shape[0]:continue
            target,weights,tx,ty,tn = template(target_path,w,h)
            valid = rectangle_sum(integral,w,h)==w*h
            if not np.any(valid):continue
            mse = (cv2.matchTemplate(squared,weights,cv2.TM_CCORR)
                  -2*cv2.matchTemplate(pixels,weights*target,cv2.TM_CCORR)
                  +np.sum(weights*target*target))
            numerator = (cv2.matchTemplate(gx,tx,cv2.TM_CCORR)+cv2.matchTemplate(gy,ty,cv2.TM_CCORR))
            gc = numerator/np.sqrt(np.maximum(rectangle_sum(energy,w,h)*tn,1e-10))
            scores = .55*(1-mse)+.45*np.maximum(gc,0)
            scores[~valid] = -np.inf
            index = int(np.argmax(scores))
            score = float(scores.flat[index])
            if score<=best[0]:continue
            y,x = np.unravel_index(index,scores.shape)
            to_rotated = np.array([[w/SIZE[0],0,x],[0,h/SIZE[1],y],[0,0,1]],np.float64)
            affine = np.vstack([inverse,[0,0,1]])@to_rotated
            affine[0] *= SIZE[0]/width
            affine[1] *= SIZE[1]/height
            parameters = parameters_from_matrix(affine[:2].ravel())
            # Clamp to the renderer's stricter one-pixel source guard.
            parameters[2] = min(1.,max(.35,parameters[2]))
            best = score,parameters
    if best[1] is None:raise ValueError("No valid photo viewport")
    return best


def warped_gray(gray,parameters,width=64):
    height = round(width*SIZE[1]/SIZE[0])
    matrix = np.asarray(coefficients(parameters),np.float64).reshape(2,3).copy()
    matrix[:,0] *= SIZE[0]/width
    matrix[:,1] *= SIZE[1]/height
    matrix[0] *= gray.shape[1]/SIZE[0]
    matrix[1] *= gray.shape[0]/SIZE[1]
    return cv2.warpAffine(gray,matrix,(width,height),flags=cv2.INTER_LINEAR|cv2.WARP_INVERSE_MAP)


def refine(gray,target_path,initial,iterations=24):
    target,weights,tx,ty,tn = template(target_path,64,48)
    region_weights = np.sqrt(.4*weights).ravel()
    target_gradient = np.concatenate([tx.ravel(),ty.ravel()])/math.sqrt(max(tn,1e-10))
    def residual(p):
        pixels = warped_gray(gray,p)
        gx = cv2.Sobel(pixels,cv2.CV_32F,1,0,ksize=3)
        gy = cv2.Sobel(pixels,cv2.CV_32F,0,1,ksize=3)
        norm = math.sqrt(max(float(np.sum(gx*gx+gy*gy)),1e-10))
        gradient = np.concatenate([gx.ravel(),gy.ravel()])/norm
        return np.concatenate([(pixels-target).ravel()*region_weights,
                               math.sqrt(.6)*(gradient-target_gradient)]).astype(np.float64)
    lower = np.array([0,0,.35,initial[3]-15],np.float64)
    upper = np.array([1,1,1,initial[3]+15],np.float64)
    start = np.clip(initial,lower+1e-7,upper-1e-7)
    steps = np.array([.005,.005,.003,.15])
    def jacobian(p):
        base = residual(p)
        columns = []
        for i,step in enumerate(steps):
            q = p.copy()
            q[i] += step if p[i]+step<=upper[i] else -step
            columns.append((residual(q)-base)/(q[i]-p[i]))
        return np.stack(columns,axis=1)
    result = least_squares(residual,start,jac=jacobian,bounds=(lower,upper),
                           max_nfev=iterations,ftol=2e-3,xtol=2e-3,gtol=2e-3)
    return result.x
