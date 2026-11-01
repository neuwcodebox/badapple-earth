"""Fused exact signed EDT, using separable squared-distance envelopes.

The binary column pass computes distances to both classes together. Each row
then uses the lower envelope of parabolas (Felzenszwalb/Huttenlocher, 2012).
Only distances needed for the pixel's class are evaluated; saturated pixels
do not need a square root. No outside pixels or approximate distance stencil.
"""

import math

from numba import njit
import numpy as np

BACKEND = "numba-fused-exact-squared-edt-v1"


@njit(cache=True, nogil=True)
def signed_distance(mask, radius):
    height, width = mask.shape
    infinite = (height + width + 1) ** 2
    columns = np.empty((height, width, 2), np.int32)
    for x in range(width):
        last_zero, last_one = -1, -1
        for y in range(height):
            if mask[y, x]:
                last_one = y
                columns[y, x, 0] = (y-last_zero)**2 if last_zero >= 0 else infinite
                columns[y, x, 1] = 0
            else:
                last_zero = y
                columns[y, x, 0] = 0
                columns[y, x, 1] = (y-last_one)**2 if last_one >= 0 else infinite
        next_zero, next_one = height, height
        for y in range(height-1, -1, -1):
            if mask[y, x]:
                next_one = y
                if next_zero < height:
                    columns[y, x, 0] = min(columns[y, x, 0], (next_zero-y)**2)
            else:
                next_zero = y
                if next_one < height:
                    columns[y, x, 1] = min(columns[y, x, 1], (next_one-y)**2)
    result = np.empty((height, width), np.float32)
    sites = np.empty(width, np.int32)
    splits = np.empty(width+1, np.float64)
    radius2 = radius * radius
    radius32 = np.float32(radius)
    for y in range(height):
        for component in range(2):
            count = 0
            sites[0] = 0
            splits[0], splits[1] = -np.inf, np.inf
            for q in range(1, width):
                p = sites[count]
                split = ((columns[y, q, component] + q*q) - (columns[y, p, component] + p*p)) / (2.0*(q-p))
                while split <= splits[count]:
                    count -= 1
                    p = sites[count]
                    split = ((columns[y, q, component] + q*q) - (columns[y, p, component] + p*p)) / (2.0*(q-p))
                count += 1
                sites[count] = q
                splits[count], splits[count+1] = split, np.inf
            count = 0
            sign = np.float32(1 if component == 0 else -1)
            for x in range(width):
                while splits[count+1] < x:
                    count += 1
                if mask[y, x] == (component == 0):
                    dx = x-sites[count]
                    distance2 = dx*dx + columns[y, sites[count], component]
                    if distance2 >= radius2:
                        result[y, x] = sign
                    else:
                        result[y, x] = sign * np.float32(math.sqrt(distance2)) / radius32
    return result
