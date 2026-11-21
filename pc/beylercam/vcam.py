"""Görüntüyü sanal webcam olarak (OBS Virtual Camera sürücüsü) yayınlar."""

from __future__ import annotations

import logging
import threading
from typing import Callable

import cv2
import numpy as np

from .processing import letterbox

log = logging.getLogger(__name__)

try:
    import pyvirtualcam
    from pyvirtualcam import PixelFormat

    AVAILABLE = True
    IMPORT_ERROR = ""
except Exception as e:  # pragma: no cover - ortama bağlı
    pyvirtualcam = None
    PixelFormat = None
    AVAILABLE = False
    IMPORT_ERROR = str(e)

FrameSource = Callable[[], tuple[int, "np.ndarray | None"]]
StateCallback = Callable[[str, str], None]


def make_placeholder(width: int, height: int, text: str = "BeylerCam - telefon bekleniyor") -> np.ndarray:
    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[:] = (40, 30, 20)
    scale = max(0.5, width / 1280)
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
    cv2.putText(img, text, ((width - tw) // 2, (height + th) // 2), cv2.FONT_HERSHEY_SIMPLEX,
                scale, (230, 230, 230), 2, cv2.LINE_AA)
    return img


def friendly_error(err: Exception) -> str:
    msg = str(err)
    low = msg.lower()
    if "obs" in low and ("not installed" in low or "not found" in low):
        return ("OBS Virtual Camera bulunamadı. OBS Studio'yu kurun "
                "(winget install OBSProject.OBSStudio), sonra tekrar deneyin.")
    if "in use" in low or "already" in low:
        return "Sanal kamera başka bir program tarafından kullanılıyor (OBS'te sanal kamerayı durdurun)."
    return f"Sanal kamera başlatılamadı: {msg}"


class VirtualCamera:
    def __init__(self, frame_source: FrameSource, on_state: StateCallback):
        self._frame_source = frame_source
        self._on_state = on_state
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.size: tuple[int, int, int] | None = None  # (w, h, fps)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, width: int, height: int, fps: int) -> None:
        self.stop()
        if not AVAILABLE:
            self._on_state("error", f"pyvirtualcam yüklenemedi: {IMPORT_ERROR}")
            return
        self._stop = threading.Event()
        self.size = (width, height, fps)
        self._thread = threading.Thread(target=self._run, args=(width, height, fps, self._stop),
                                        name="bc-vcam", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)
        self._thread = None
        self.size = None

    def _run(self, width: int, height: int, fps: int, stop: threading.Event) -> None:
        try:
            with pyvirtualcam.Camera(width, height, fps, fmt=PixelFormat.BGR, print_fps=False) as cam:
                log.info("Sanal kamera açıldı: %s %dx%d@%d", cam.device, width, height, fps)
                self._on_state("running", f"{cam.device} • {width}x{height} @ {fps} FPS")
                placeholder = make_placeholder(width, height)
                last_id = -1
                out = placeholder
                while not stop.is_set():
                    frame_id, img = self._frame_source()
                    if img is None:
                        out = placeholder
                        last_id = -1
                    elif frame_id != last_id:
                        out = letterbox(img, width, height)
                        last_id = frame_id
                    cam.send(out)
                    cam.sleep_until_next_frame()
        except Exception as e:
            log.exception("Sanal kamera hatası")
            if not stop.is_set():  # bilerek durdurulan eski bir örneğin hatası bildirilmez
                self._on_state("error", friendly_error(e))
