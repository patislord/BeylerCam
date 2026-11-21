"""Görüntü yönlendirme (döndürme/ayna) ve sanal kamera için boyutlandırma."""

from __future__ import annotations

import cv2
import numpy as np

_ROTATIONS = {
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}


def orient(img: np.ndarray, rotation_deg: int, mirror: bool) -> np.ndarray:
    """Görüntüyü saat yönünde `rotation_deg` kadar döndürür, sonra isteğe bağlı aynalar."""
    r = rotation_deg % 360
    if r in _ROTATIONS:
        img = cv2.rotate(img, _ROTATIONS[r])
    elif r != 0:
        raise ValueError(f"Desteklenmeyen dönüş: {rotation_deg}")
    if mirror:
        img = cv2.flip(img, 1)
    return img


def fit_size(src_w: int, src_h: int, max_w: int, max_h: int) -> tuple[int, int]:
    """En-boy oranını koruyarak (max_w, max_h) kutusuna sığan boyut."""
    if src_w <= 0 or src_h <= 0 or max_w <= 0 or max_h <= 0:
        return 0, 0
    scale = min(max_w / src_w, max_h / src_h)
    return max(1, round(src_w * scale)), max(1, round(src_h * scale))


def resize_to_fit(img: np.ndarray, max_w: int, max_h: int) -> np.ndarray:
    h, w = img.shape[:2]
    tw, th = fit_size(w, h, max_w, max_h)
    if (tw, th) == (w, h) or tw == 0:
        return img
    interp = cv2.INTER_AREA if tw < w else cv2.INTER_LINEAR
    return cv2.resize(img, (tw, th), interpolation=interp)


def letterbox(img: np.ndarray, out_w: int, out_h: int) -> np.ndarray:
    """Görüntüyü oranı bozmadan out_w x out_h siyah tuvalin ortasına yerleştirir."""
    h, w = img.shape[:2]
    if (w, h) == (out_w, out_h):
        return np.ascontiguousarray(img)
    resized = resize_to_fit(img, out_w, out_h)
    rh, rw = resized.shape[:2]
    canvas = np.zeros((out_h, out_w, 3), dtype=np.uint8)
    x = (out_w - rw) // 2
    y = (out_h - rh) // 2
    canvas[y:y + rh, x:x + rw] = resized
    return canvas
