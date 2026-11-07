"""Bounded similarity views with an exact source quadrilateral and geographic centre."""

import json
import math

import numpy as np
from rasterio.transform import Affine

from ...collection.windows import identity
from ...matching.affine import VERSION as TRANSFORM_VERSION,geographic_tools

VERSION = "bounded-pan-zoom-rotation-bilinear-v1"
SIZE = (256,191)


def maximum_scale(angle,size=SIZE):
    w,h = size
    c,s = abs(math.cos(math.radians(angle))),abs(math.sin(math.radians(angle)))
    return min((w-2)/(c*w+s*h),(h-2)/(s*w+c*h))


def coefficients(parameters,size=SIZE):
    """u/v span only valid centres; zoom is a fraction of the inscribed view."""
    u,v,zoom,angle = parameters
    if not (0<=u<=1 and 0<=v<=1 and 0<zoom<=1):raise ValueError("Framing must stay inside the real photo")
    w,h = size
    c,s = math.cos(math.radians(angle)),math.sin(math.radians(angle))
    scale = maximum_scale(angle,size)*zoom
    hx,hy = scale*(abs(c)*w+abs(s)*h)/2,scale*(abs(s)*w+abs(c)*h)/2
    cx,cy = hx+1+u*(w-2*hx-2),hy+1+v*(h-2*hy-2)
    a,b,d,e = c*scale,s*scale,-s*scale,c*scale
    return [a,b,cx-a*w/2-b*h/2,d,e,cy-d*w/2-e*h/2]


def parameters_from_matrix(matrix,size=SIZE):
    w,h = size
    a,b,c,d,e,f = matrix
    angle = math.degrees(math.atan2(b,a))
    scale = math.hypot(a,b)
    zoom = min(1.,scale/maximum_scale(angle,size))
    hx,hy = (abs(a)*w+abs(b)*h)/2,(abs(d)*w+abs(e)*h)/2
    cx,cy = a*w/2+b*h/2+c,d*w/2+e*h/2+f
    u = (cx-hx-1)/max(w-2*hx-2,1e-7)
    v = (cy-hy-1)/max(h-2*hy-2,1e-7)
    return np.array([np.clip(u,0,1),np.clip(v,0,1),zoom,angle],np.float64)


def metadata(row,scene,parameters):
    matrix = coefficients(parameters)
    w,h = SIZE
    a,b,c,d,e,f = matrix
    left,top,sw,sh = row["window"]
    def pixel(x,y):return left+(a*x+b*y+c)*sw/w,top+(d*x+e*y+f)*sh/h
    pixels = [pixel(x,y) for x,y in ((0,0),(w,0),(w,h),(0,h))]
    transform = Affine(*scene["affine"])
    converter,geod = geographic_tools(scene["crs"])
    geographic = [list(converter.transform(*(transform@p))) for p in pixels]
    center = converter.transform(*(transform@pixel(w/2,h/2)))
    xx,yy = zip(*pixels)
    return {**row,"base_dense_row":row["dense_row"],
        "view_id":row["tile_id"]+"-registered-"+identity([VERSION,[round(v,7) for v in matrix]])[:24],
        "view_version":VERSION,"transform_version":TRANSFORM_VERSION,"view_resampling":"bilinear",
        "view_affine":matrix,"transform_size":list(SIZE),"registration_parameters":list(map(float,parameters)),
        "rotation_degrees":float(parameters[3]%360),"rotation_direction":"clockwise","rotation_crop":[0,0,w,h],
        "crop_scale":float(parameters[2]),"view_source_quad":[list(p) for p in pixels],
        "view_source_window":[min(xx),min(yy),max(xx)-min(xx),max(yy)-min(yy)],
        "source_footprint_json":row.get("source_footprint_json") or row["footprint_json"],
        "footprint_json":json.dumps({"type":"Polygon","coordinates":[geographic+[geographic[0]]]}),
        "longitude":center[0],"latitude":center[1],
        "ground_width_m":abs(geod.inv(*geographic[0],*geographic[1])[2]),
        "ground_height_m":abs(geod.inv(*geographic[0],*geographic[3])[2])}


def image_points(matrix):
    """Three fixed source points expressed in output coordinates for motion costs."""
    a,b,c,d,e,f = matrix
    affine = np.array([[a,b],[d,e]])
    points = np.array([[128,95.5],[96,95.5],[128,71.5]],np.float64)
    return (points-np.array([c,f]))@np.linalg.inv(affine).T/np.array(SIZE)
