"""Luminance FSIM, ported from the authors' FeatureSIM.m (2010).

Copyright (c) 2010 Lin ZHANG, Lei Zhang, Xuanqin Mou and David Zhang.
Research/educational use only; see THIRD_PARTY_NOTICES.md for permissions.
Phase congruency: Copyright (c) 1996-2009 Peter Kovesi, permissive license
reproduced in THIRD_PARTY_NOTICES.md. Uses the FSIM-specific 4x4 filters,
not the different defaults of the standalone phasecong2 implementation.
"""

from functools import lru_cache

import numpy as np
from scipy.fft import fft2, ifft2, ifftshift
from scipy.ndimage import convolve


@lru_cache(maxsize=4)
def filters(shape):
    def axis(length):
        if length % 2:
            return np.arange(-(length-1)//2, (length-1)//2+1) / (length-1)
        return np.arange(-length//2, length//2) / length

    x, y = np.meshgrid(axis(shape[1]), axis(shape[0]))
    radius = ifftshift(np.hypot(x, y))
    theta = ifftshift(np.arctan2(-y, x))
    lowpass = 1 / (1 + (radius / .45)**30)
    radius[0, 0] = 1
    radial = [np.exp(-np.log(radius * (6 * 2**scale))**2 / (2*np.log(.55)**2)) * lowpass
              for scale in range(4)]
    for band in radial:
        band[0, 0] = 0
    result = []
    for orientation in range(4):
        angle = orientation * np.pi / 4
        distance = np.abs(np.arctan2(np.sin(theta-angle), np.cos(theta-angle)))
        spread = np.exp(-distance**2 / (2*(np.pi/4/1.2)**2))
        bank = np.asarray([band * spread for band in radial])
        impulses = ifft2(bank, axes=(-2, -1)).real * np.sqrt(np.prod(shape))
        # 2*sum(h_s^2) + 4*sum(h_s*h_t) = 2*sum((sum_s h_s)^2).
        noise_factor = 2 * np.sum(np.sum(impulses, axis=0)**2) / np.sum(bank[0]**2)
        result.append((bank, noise_factor))
    return result


def phase_congruency(image):
    spectrum = fft2(np.asarray(image, dtype=np.float64))
    total_energy = np.zeros(image.shape)
    total_amplitude = np.zeros(image.shape)
    for bank, noise_factor in filters(image.shape):
        response = ifft2(bank * spectrum, axes=(-2, -1))
        amplitude = np.abs(response)
        summed = np.sum(response, axis=0)
        mean = summed / (np.abs(summed) + .0001)
        energy = np.sum(response.real*mean.real + response.imag*mean.imag
                        - np.abs(response.real*mean.imag - response.imag*mean.real), axis=0)
        mean_noise_squared = -np.median(amplitude[0]**2) / np.log(.5)
        tau = np.sqrt(max(0, mean_noise_squared * noise_factor / 2))
        threshold = tau * (np.sqrt(np.pi/2) + 2*np.sqrt(2-np.pi/2)) / 1.7
        total_energy += np.maximum(energy - threshold, 0)
        total_amplitude += np.sum(amplitude, axis=0)
    return np.divide(total_energy, total_amplitude, out=np.zeros_like(total_energy),
                     where=total_amplitude > 1e-12).astype(np.float32)


def features(gray):
    """Input: canonical grayscale in [0,1], below the automatic resize threshold."""
    image = np.asarray(gray, dtype=np.float64) * 255
    dx = np.array([[3, 0, -3], [10, 0, -10], [3, 0, -3]]) / 16
    gradient = np.hypot(convolve(image, dx, mode="constant"),
                        convolve(image, dx.T, mode="constant"))
    return phase_congruency(image), gradient.astype(np.float32)


def similarity(left, right, fallback_mse):
    pc1, gradient1 = left
    pc2, gradient2 = right
    weight = np.maximum(pc1, pc2)
    denominator = float(np.sum(weight, dtype=np.float64))
    # Featureless images make the reference implementation's 0/0 undefined.
    if denominator < 1e-8:
        return 1 - fallback_mse
    pc_similarity = (2*pc1*pc2 + .85) / (pc1*pc1 + pc2*pc2 + .85)
    gradient_similarity = (2*gradient1*gradient2 + 160) / (gradient1**2 + gradient2**2 + 160)
    return float(np.sum(pc_similarity*gradient_similarity*weight, dtype=np.float64) / denominator)
