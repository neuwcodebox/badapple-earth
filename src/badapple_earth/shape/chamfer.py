"""Exact capped directional matching using a small, ordered pixel neighbourhood.

Adapted to the existing float32 angles and arithmetic from the user-provided
Bad_Apple_Shape_Optimization.md; edge extraction remains in geometry.py.
"""

from functools import lru_cache
import math

from numba import njit
import numpy as np

BACKEND = "numba-exact-capped-grid-float32-v1"


@lru_cache(maxsize=8)
def offsets(radius):
    extent = math.ceil(radius)
    sites = [(dy, dx, dy*dy+dx*dx) for dy in range(-extent, extent+1) for dx in range(-extent, extent+1)
             if dy*dy+dx*dx < radius*radius]
    sites.sort(key=lambda row: (row[2], row[0], row[1]))
    coordinates = np.asarray([(dy, dx) for dy, dx, _ in sites], dtype=np.int32)
    distance = np.sqrt(np.asarray([squared for _, _, squared in sites], np.float64)).astype(np.float32)
    costs = np.float32(.8) * (distance / np.float32(radius))
    return coordinates, costs


@njit(cache=True, nogil=True)
def directed_minima(points, angles, other_grid, other_angles, neighbourhood, spatial):
    result = np.empty(len(points), np.float32)
    height, width = other_grid.shape
    pi = np.float32(np.pi)
    half_pi = np.float32(np.pi / 2)
    for i in range(len(points)):
        angle = angles[i]
        place = np.searchsorted(other_angles, angle)
        before = abs(np.float32(angle-other_angles[(place-1) % len(other_angles)]))
        after = abs(np.float32(angle-other_angles[place % len(other_angles)]))
        delta = min(min(before, np.float32(pi-before)), min(after, np.float32(pi-after)))
        best = np.float32(.8) + np.float32(.2) * np.float32(delta / half_pi)
        for j in range(len(neighbourhood)):
            if spatial[j] > best:
                break
            y = points[i, 0] + neighbourhood[j, 0]
            x = points[i, 1] + neighbourhood[j, 1]
            if y < 0 or y >= height or x < 0 or x >= width:
                continue
            other = other_grid[y, x]
            if other < 0:
                continue
            difference = abs(np.float32(angle-other))
            angular = np.float32(min(difference, np.float32(pi-difference)) / half_pi)
            cost = spatial[j] + np.float32(.2) * angular
            if cost < best:
                best = cost
        result[i] = best
    return result
