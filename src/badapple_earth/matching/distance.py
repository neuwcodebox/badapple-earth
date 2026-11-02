"""Integer-exact MSE dot products using the CPU's floating-point BLAS."""

import numpy as np


def dot_dtype(dimension):
    # Each product and every partial nonnegative sum is an integer <= 2**24.
    # Cast the dot product back before adding norms: the latter can exceed it.
    return np.float32 if dimension * 255**2 <= 2**24 else np.float64


def exact_sse(targets, candidates):
    left, right = np.asarray(targets), np.asarray(candidates)
    dtype = dot_dtype(left.shape[1])
    dot = left.astype(dtype) @ right.astype(dtype).T
    a, b = left.astype(np.int64), right.astype(np.int64)
    return (np.sum(a * a, axis=1)[:, None] + np.sum(b * b, axis=1)[None, :]
            - 2 * dot.astype(np.int64))
