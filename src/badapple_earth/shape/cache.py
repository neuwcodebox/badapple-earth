"""Byte-bounded reuse of geometry determined by an exact binary mask."""

from collections import OrderedDict

import numpy as np


def payload_bytes(value):
    if isinstance(value, np.ndarray):
        return value.nbytes
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, (tuple, list)):
        return sum(payload_bytes(item) for item in value)
    if isinstance(value, dict):
        return sum(payload_bytes(item) for item in value.values())
    return 0


class MaskCache:
    def __init__(self, budget=128*1024**2):
        self.budget, self.bytes = budget, 0
        self.items = OrderedDict()
        self.hits = self.misses = 0

    def get(self, key, factory):
        if key in self.items:
            self.hits += 1
            value, size = self.items.pop(key)
            self.items[key] = (value, size)
            return value
        self.misses += 1
        value = factory()
        size = payload_bytes(value) + payload_bytes(key)
        if size <= self.budget:
            while self.items and self.bytes + size > self.budget:
                _, (_, removed) = self.items.popitem(last=False)
                self.bytes -= removed
            self.items[key] = (value, size)
            self.bytes += size
        return value


def mask_key(mask):
    # The full packed bytes are compared, not just a possibly colliding digest.
    # SDF/contours depend on this mask and grid alone, irrespective of the photo.
    return ("mask-geometry-v1", mask.shape, np.packbits(mask, bitorder="little").tobytes())
