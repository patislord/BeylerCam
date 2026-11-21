"""Sahte telefon: Android uygulaması olmadan PC tarafını test etmek için.

Android uygulamasıyla aynı protokolü konuşur ve hareketli bir test görüntüsü yayınlar.

    python -m beylercam.simulator            # 4747 portunda
    python -m beylercam.simulator --rotation 90
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import threading
import time

import cv2
import numpy as np

from . import protocol as P


class _Client:
    def __init__(self, sim: "FakePhone", sock: socket.socket):
        self.sim = sim
        self.sock = sock
        self.cond = threading.Condition()
        self.frame: bytes | None = None
        self.control: list[bytes] = []
        self.open = True

    def offer_frame(self, data: bytes) -> None:
        with self.cond:
            self.frame = data
            self.cond.notify_all()

    def offer_control(self, data: bytes) -> None:
        with self.cond:
            self.control.append(data)
            self.cond.notify_all()

    def start(self) -> None:
        self.offer_control(P.encode_json_message(P.MSG_HELLO, self.sim.hello()))
        threading.Thread(target=self._write_loop, daemon=True).start()
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _write_loop(self) -> None:
        try:
            while self.open:
                with self.cond:
                    while self.open and self.frame is None and not self.control:
                        self.cond.wait(0.5)
                    batch = self.control + ([self.frame] if self.frame is not None else [])
                    self.control = []
                    self.frame = None
                for item in batch:
                    self.sock.sendall(item)
        except OSError:
            pass
        finally:
            self.close()

    def _read_loop(self) -> None:
        try:
            with self.sock.makefile("r", encoding="utf-8", newline="\n") as f:
                for line in f:
                    try:
                        cmd = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if cmd.get("cmd") == "ping":
                        self.offer_control(P.encode_json_message(P.MSG_PONG, {"t": cmd.get("t", 0)}))
                    else:
                        self.sim.handle_command(cmd)
        except (OSError, ValueError):
            pass
        finally:
            self.close()

    def close(self) -> None:
        if not self.open:
            return
        self.open = False
        with self.cond:
            self.cond.notify_all()
        try:
            # makefile() açıkken close() soketi kapatmaz; shutdown karşı tarafa hemen haber verir
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.sim._remove(self)


class FakePhone:
    def __init__(self, port: int = P.PORT, udp_port: int | None = None, rotation: int = 0,
                 host: str = "0.0.0.0", name: str = "Sahte Telefon"):
        self.width, self.height, self.fps, self.quality = 1280, 720, 30, 80
        self.front = False
        self.rotation = rotation
        self.name = name
        self.commands: list[dict] = []
        self._lock = threading.Lock()
        self._clients: list[_Client] = []
        self._running = False
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp.bind((host, port))
        self._tcp.listen(4)
        self.port = self._tcp.getsockname()[1]
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.bind((host, self.port if udp_port is None else udp_port))
        self.udp_port = self._udp.getsockname()[1]

    # ------------------------------------------------------------------
    def hello(self) -> dict:
        return {"app": P.APP, "version": P.VERSION, "device": self.name, "port": self.port, **self.status()}

    def status(self) -> dict:
        return {"width": self.width, "height": self.height, "fps": self.fps, "quality": self.quality,
                "camera": "front" if self.front else "back", "cameras": ["back", "front"]}

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def handle_command(self, cmd: dict) -> None:
        self.commands.append(cmd)
        with self._lock:
            if cmd.get("cmd") == "config":
                self.width = int(cmd.get("width", self.width))
                self.height = int(cmd.get("height", self.height))
                self.fps = max(1, min(60, int(cmd.get("fps", self.fps))))
                self.quality = max(10, min(100, int(cmd.get("quality", self.quality))))
                if cmd.get("camera") in ("front", "back"):
                    self.front = cmd["camera"] == "front"
            elif cmd.get("cmd") == "switch":
                self.front = not self.front
            else:
                return
        msg = P.encode_json_message(P.MSG_STATUS, self.status())
        for c in list(self._clients):
            c.offer_control(msg)

    def _remove(self, client: _Client) -> None:
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)

    # ------------------------------------------------------------------
    def start(self) -> "FakePhone":
        self._running = True
        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._udp_loop, daemon=True).start()
        threading.Thread(target=self._frame_loop, daemon=True).start()
        return self

    def stop(self) -> None:
        self._running = False
        for s in (self._tcp, self._udp):
            try:
                s.close()
            except OSError:
                pass
        for c in list(self._clients):
            c.close()

    def _accept_loop(self) -> None:
        while self._running:
            try:
                sock, _ = self._tcp.accept()
            except OSError:
                break
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            client = _Client(self, sock)
            with self._lock:
                self._clients.append(client)
            client.start()

    def _udp_loop(self) -> None:
        while self._running:
            try:
                data, addr = self._udp.recvfrom(512)
            except OSError:
                if not self._running:
                    break
                continue
            if data.strip() == P.DISCOVER_MAGIC:
                reply = json.dumps({"app": P.APP, "version": P.VERSION, "device": self.name,
                                    "port": self.port}).encode()
                try:
                    self._udp.sendto(reply, addr)
                except OSError:
                    pass

    def render(self, t: float) -> np.ndarray:
        w, h = self.width, self.height
        x = np.linspace(0, 255, w, dtype=np.float32)
        y = np.linspace(0, 255, h, dtype=np.float32)
        img = np.empty((h, w, 3), dtype=np.uint8)
        img[..., 0] = (x[None, :] * 0.6).astype(np.uint8)
        img[..., 1] = (y[:, None] * 0.6).astype(np.uint8)
        img[..., 2] = int(128 + 100 * math.sin(t))
        cx = int(w / 2 + w / 3 * math.cos(t * 1.3))
        cy = int(h / 2 + h / 3 * math.sin(t * 1.7))
        cv2.circle(img, (cx, cy), max(10, h // 10), (255, 255, 255), -1)
        # "YUKARI" yazısı, yönlendirmenin doğru olup olmadığını görmek için
        cv2.arrowedLine(img, (60, h - 40), (60, 40), (0, 255, 255), 6, tipLength=0.15)
        label = f"{'ON' if self.front else 'ARKA'} {w}x{h} {self.fps}fps  {time.strftime('%H:%M:%S')}"
        cv2.putText(img, label, (100, 60), cv2.FONT_HERSHEY_SIMPLEX, max(0.6, w / 1280), (255, 255, 255), 2,
                    cv2.LINE_AA)
        # Telefon sensörü gibi: görüntüyü "yan" gönder, PC rotation kadar döndürüp düzeltmeli
        undo = {90: cv2.ROTATE_90_COUNTERCLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_CLOCKWISE}
        if self.rotation in undo:
            img = cv2.rotate(img, undo[self.rotation])
        return img

    def _frame_loop(self) -> None:
        start = time.monotonic()
        next_due = start
        while self._running:
            if self._clients:
                with self._lock:
                    img = self.render(time.monotonic() - start)
                    quality, front, fps = self.quality, self.front, self.fps
                ok, jpeg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
                if ok:
                    data = P.encode_frame(self.rotation, front, jpeg.tobytes())
                    for c in list(self._clients):
                        c.offer_frame(data)
            else:
                fps = self.fps
            next_due += 1.0 / fps
            delay = next_due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_due = time.monotonic()


def main() -> None:
    ap = argparse.ArgumentParser(description="BeylerCam sahte telefon (test için)")
    ap.add_argument("--port", type=int, default=P.PORT)
    ap.add_argument("--rotation", type=int, default=0, choices=[0, 90, 180, 270],
                    help="Telefonun bildirdiği sensör dönüşü")
    args = ap.parse_args()
    phone = FakePhone(port=args.port, rotation=args.rotation).start()
    print(f"Sahte telefon çalışıyor: TCP/UDP {phone.port}. Durdurmak için Ctrl+C.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        phone.stop()


if __name__ == "__main__":
    main()
