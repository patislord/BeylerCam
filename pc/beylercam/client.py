"""Telefona bağlanan, kareleri alıp çözen istemci."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable

import cv2
import numpy as np

from . import protocol as P

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 6.0
PING_INTERVAL = 2.0
RECONNECT_DELAY = 2.0


@dataclass
class StreamSettings:
    width: int = 1280
    height: int = 720
    fps: int = 30
    quality: int = 80
    camera: str = "back"

    def to_command(self) -> dict:
        return {"cmd": "config", **asdict(self)}


EventCallback = Callable[[str, dict], None]
FrameCallback = Callable[[np.ndarray, int, bool], None]


class PhoneClient:
    """Arka planda telefona bağlanır; koparsa otomatik yeniden bağlanır.

    Olaylar (on_event): connecting, connected, hello, status, stats, rtt, disconnected, stopped
    Kareler (on_frame): (BGR görüntü, telefonun bildirdiği dönüş, ön kamera mı)
    """

    def __init__(
        self,
        host: str,
        port: int,
        settings: StreamSettings,
        on_event: EventCallback,
        on_frame: FrameCallback,
        auto_reconnect: bool = True,
    ):
        self.host = host
        self.port = port
        self.settings = settings
        self.auto_reconnect = auto_reconnect
        self._on_event = on_event
        self._on_frame = on_frame
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.connected = False

    # ---------------- Genel API ----------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="bc-client", daemon=True)
        self._thread.start()

    def stop(self, wait: bool = True) -> None:
        self._stop.set()
        self._close_socket()
        if wait and self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)

    def update_settings(self, settings: StreamSettings) -> None:
        self.settings = settings
        self.send(settings.to_command())

    def switch_camera(self) -> None:
        self.send({"cmd": "switch"})

    def send(self, cmd: dict) -> bool:
        sock = self._sock
        if sock is None:
            return False
        try:
            with self._send_lock:
                sock.sendall(P.encode_command(cmd))
            return True
        except OSError as e:
            log.debug("Komut gönderilemedi: %s", e)
            return False

    # ---------------- İç işleyiş ----------------

    def _emit(self, name: str, **data) -> None:
        try:
            self._on_event(name, data)
        except Exception:  # pragma: no cover - arayüz hataları bağlantıyı düşürmesin
            log.exception("Olay işleyici hatası (%s)", name)

    def _close_socket(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            reason = ""
            try:
                self._emit("connecting", host=self.host, port=self.port)
                sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
                sock.settimeout(READ_TIMEOUT)
                self._sock = sock
                if self._stop.is_set():
                    break
                self.send(self.settings.to_command())
                self.connected = True
                self._emit("connected", host=self.host, port=self.port)
                conn_done = threading.Event()
                threading.Thread(target=self._ping_loop, args=(sock, conn_done), name="bc-ping", daemon=True).start()
                try:
                    self._receive_loop(sock)
                finally:
                    conn_done.set()
            except socket.timeout:
                reason = "Zaman aşımı (telefon cevap vermiyor)"
            except ConnectionRefusedError:
                reason = "Bağlantı reddedildi (telefonda BeylerCam açık mı?)"
            except (OSError, ConnectionError, P.ProtocolError) as e:
                reason = str(e) or e.__class__.__name__
            finally:
                was_connected = self.connected
                self.connected = False
                self._close_socket()
            if self._stop.is_set():
                break
            self._emit("disconnected", reason=reason, was_connected=was_connected)
            if not self.auto_reconnect:
                break
            self._stop.wait(RECONNECT_DELAY)
        self._emit("stopped")

    def _ping_loop(self, sock: socket.socket, done: threading.Event) -> None:
        """Telefon kare göndermese bile bağlantının canlı olduğunu anlamak için."""
        while self._sock is sock and not done.is_set():
            self.send({"cmd": "ping", "t": time.monotonic_ns() // 1000})
            done.wait(PING_INTERVAL)

    def _receive_loop(self, sock: socket.socket) -> None:
        frames = 0
        nbytes = 0
        window_start = time.monotonic()
        while not self._stop.is_set():
            msg_type, payload = P.read_message(sock)
            nbytes += len(payload) + P.HEADER.size

            if msg_type == P.MSG_FRAME:
                rotation, front, jpeg = P.decode_frame_payload(payload)
                img = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
                if img is None:
                    log.warning("Bozuk JPEG karesi atlandı")
                    continue
                frames += 1
                self._on_frame(img, rotation, front)
            elif msg_type in (P.MSG_HELLO, P.MSG_STATUS):
                data = self._parse_json(payload)
                if data is not None:
                    self._emit("hello" if msg_type == P.MSG_HELLO else "status", **data)
            elif msg_type == P.MSG_PONG:
                data = self._parse_json(payload) or {}
                try:
                    sent_us = int(data.get("t", 0))
                    rtt_ms = (time.monotonic_ns() // 1000 - sent_us) / 1000.0
                    if 0 <= rtt_ms < 60_000:
                        self._emit("rtt", ms=rtt_ms)
                except (TypeError, ValueError):
                    pass

            elapsed = time.monotonic() - window_start
            if elapsed >= 1.0:
                self._emit("stats", fps=frames / elapsed, mbps=nbytes * 8 / elapsed / 1e6)
                frames = 0
                nbytes = 0
                window_start = time.monotonic()

    @staticmethod
    def _parse_json(payload: bytes) -> dict | None:
        try:
            data = json.loads(bytes(payload).decode("utf-8"))
            return data if isinstance(data, dict) else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
