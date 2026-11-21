"""Uçtan uca testler: sahte telefon + gerçek PC istemcisi.

    cd pc
    python -m unittest -v
"""

from __future__ import annotations

import queue
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

from beylercam import protocol as P
from beylercam import settings_store
from beylercam.app import FrameHub, parse_host, parse_resolution
from beylercam.client import PhoneClient, StreamSettings
from beylercam.discovery import discover
from beylercam.processing import fit_size, letterbox, orient
from beylercam.simulator import FakePhone


class EventRecorder:
    def __init__(self):
        self.events: queue.Queue = queue.Queue()
        self.frames: list[tuple[tuple[int, ...], int, bool]] = []
        self.lock = threading.Lock()

    def on_event(self, name, data):
        self.events.put((name, data))

    def on_frame(self, img, rotation, front):
        with self.lock:
            self.frames.append((img.shape, rotation, front))

    def wait_event(self, name, timeout=5.0, predicate=lambda d: True):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                ev, data = self.events.get(timeout=0.1)
            except queue.Empty:
                continue
            if ev == name and predicate(data):
                return data
        raise AssertionError(f"'{name}' olayı gelmedi")

    def wait_frame(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                for f in reversed(self.frames):
                    if predicate(f):
                        return f
            time.sleep(0.02)
        raise AssertionError("Beklenen kare gelmedi")


class ProtocolTests(unittest.TestCase):
    def test_frame_roundtrip(self):
        data = P.encode_frame(270, True, b"\xff\xd8jpeg")
        msg_type, length = P.HEADER.unpack_from(data)
        self.assertEqual(msg_type, P.MSG_FRAME)
        rot, front, jpeg = P.decode_frame_payload(data[P.HEADER.size:])
        self.assertEqual((rot, front, bytes(jpeg)), (270, True, b"\xff\xd8jpeg"))
        self.assertEqual(length, len(data) - P.HEADER.size)

    def test_read_message_over_socket(self):
        a, b = socket.socketpair()
        with a, b:
            a.sendall(P.encode_json_message(P.MSG_STATUS, {"width": 640}) + P.encode_message(P.MSG_PONG, b""))
            t, payload = P.read_message(b)
            self.assertEqual(t, P.MSG_STATUS)
            self.assertIn(b"640", payload)
            t, payload = P.read_message(b)
            self.assertEqual((t, bytes(payload)), (P.MSG_PONG, b""))

    def test_oversized_message_rejected(self):
        a, b = socket.socketpair()
        with a, b:
            a.sendall(P.HEADER.pack(P.MSG_FRAME, P.MAX_PAYLOAD + 1))
            with self.assertRaises(P.ProtocolError):
                P.read_message(b)

    def test_closed_connection(self):
        a, b = socket.socketpair()
        with b:
            a.sendall(b"\x02\x00")
            a.close()
            with self.assertRaises(ConnectionError):
                P.read_message(b)

    def test_command_is_single_line(self):
        line = P.encode_command({"cmd": "config", "width": 1280})
        self.assertTrue(line.endswith(b"\n"))
        self.assertEqual(line.count(b"\n"), 1)


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        self.img = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.img[0, 0] = (255, 0, 0)  # sol üst köşe işaretli

    def test_rotations(self):
        self.assertEqual(orient(self.img, 0, False).shape, (720, 1280, 3))
        r90 = orient(self.img, 90, False)
        self.assertEqual(r90.shape, (1280, 720, 3))
        self.assertEqual(tuple(r90[0, -1]), (255, 0, 0))  # saat yönünde: sol üst -> sağ üst
        self.assertEqual(orient(self.img, 180, False).shape, (720, 1280, 3))
        self.assertEqual(tuple(orient(self.img, 180, False)[-1, -1]), (255, 0, 0))
        self.assertEqual(tuple(orient(self.img, 270, False)[-1, 0]), (255, 0, 0))
        self.assertEqual(orient(self.img, 450, False).shape, (1280, 720, 3))
        self.assertEqual(orient(self.img, -90, False).shape, (1280, 720, 3))

    def test_mirror(self):
        m = orient(self.img, 0, True)
        self.assertEqual(tuple(m[0, -1]), (255, 0, 0))

    def test_invalid_rotation(self):
        with self.assertRaises(ValueError):
            orient(self.img, 45, False)

    def test_fit_size(self):
        self.assertEqual(fit_size(1280, 720, 640, 640), (640, 360))
        self.assertEqual(fit_size(720, 1280, 1280, 720), (405, 720))
        self.assertEqual(fit_size(0, 720, 10, 10), (0, 0))

    def test_letterbox_portrait_into_landscape(self):
        portrait = np.full((1280, 720, 3), 200, dtype=np.uint8)
        out = letterbox(portrait, 1280, 720)
        self.assertEqual(out.shape, (720, 1280, 3))
        self.assertEqual(out[360, 0].tolist(), [0, 0, 0])      # kenar siyah
        self.assertEqual(out[360, 640].tolist(), [200, 200, 200])  # orta görüntü
        self.assertTrue(out.flags["C_CONTIGUOUS"])

    def test_letterbox_same_size(self):
        out = letterbox(self.img, 1280, 720)
        self.assertEqual(out.shape, self.img.shape)


class HelperTests(unittest.TestCase):
    def test_parse_host(self):
        self.assertEqual(parse_host("192.168.1.5  (Samsung S21)"), ("192.168.1.5", None))
        self.assertEqual(parse_host("192.168.1.5:5000"), ("192.168.1.5", 5000))
        self.assertEqual(parse_host("  "), ("", None))

    def test_parse_resolution(self):
        self.assertEqual(parse_resolution("1920x1080"), (1920, 1080))

    def test_frame_hub(self):
        hub = FrameHub()
        self.assertEqual(hub.get(), (0, None))
        hub.set(np.zeros((2, 2, 3), np.uint8))
        fid, img = hub.get()
        self.assertEqual(fid, 1)
        self.assertIsNotNone(img)
        hub.clear()
        self.assertEqual(hub.get()[1], None)

    def test_settings_roundtrip_and_bad_values(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            self.assertEqual(settings_store.load(path), settings_store.AppSettings())
            s = settings_store.AppSettings(host="10.0.0.2", mirror=True, rotation=90, fps=60)
            settings_store.save(s, path)
            self.assertEqual(settings_store.load(path), s)
            path.write_text('{"port": "abc", "mirror": 1, "host": "x"}', encoding="utf-8")
            loaded = settings_store.load(path)
            self.assertEqual((loaded.port, loaded.mirror, loaded.host), (4747, False, "x"))
            path.write_text("bozuk json", encoding="utf-8")
            self.assertEqual(settings_store.load(path), settings_store.AppSettings())


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.phone = FakePhone(port=0, udp_port=0, host="127.0.0.1", rotation=90).start()
        self.rec = EventRecorder()
        self.client = PhoneClient("127.0.0.1", self.phone.port, StreamSettings(),
                                  self.rec.on_event, self.rec.on_frame)

    def tearDown(self):
        self.client.stop()
        self.phone.stop()

    def test_stream_config_switch_and_ping(self):
        self.client.start()
        self.rec.wait_event("connected")
        hello = self.rec.wait_event("hello")
        self.assertEqual(hello["device"], "Sahte Telefon")

        # Varsayılan 1280x720; sahte telefon sensör gibi 90° yan gönderir
        shape, rotation, front = self.rec.wait_frame(lambda f: True)
        self.assertEqual(shape, (1280, 720, 3))
        self.assertEqual((rotation, front), (90, False))
        self.assertEqual(orient(np.zeros(shape, np.uint8), rotation, False).shape, (720, 1280, 3))

        # Çözünürlük + FPS değişimi
        self.client.update_settings(StreamSettings(width=640, height=480, fps=15, quality=60))
        status = self.rec.wait_event("status", predicate=lambda d: d.get("width") == 640)
        self.assertEqual((status["height"], status["fps"], status["quality"]), (480, 15, 60))
        self.rec.wait_frame(lambda f: f[0] == (640, 480, 3))

        # Kamera değiştirme
        self.client.switch_camera()
        self.rec.wait_event("status", predicate=lambda d: d.get("camera") == "front")
        self.rec.wait_frame(lambda f: f[2] is True)

        # Ping/RTT ve istatistik
        rtt = self.rec.wait_event("rtt", timeout=4)
        self.assertGreaterEqual(rtt["ms"], 0)
        stats = self.rec.wait_event("stats", timeout=4)
        self.assertGreater(stats["fps"], 5)

    def test_fps_limit_respected(self):
        self.client.settings = StreamSettings(fps=15)
        self.client.start()
        self.rec.wait_event("connected")
        self.rec.wait_frame(lambda f: True)
        stats = self.rec.wait_event("stats", timeout=4, predicate=lambda d: d["fps"] > 0)
        stats = self.rec.wait_event("stats", timeout=4)
        self.assertLess(stats["fps"], 20)
        self.assertGreater(stats["fps"], 10)

    def test_reconnect_after_phone_restart(self):
        self.client.start()
        self.rec.wait_event("connected")
        port = self.phone.port
        self.phone.stop()
        self.rec.wait_event("disconnected")
        self.phone = FakePhone(port=port, udp_port=0, host="127.0.0.1").start()
        self.rec.wait_event("connected", timeout=8)
        self.rec.wait_event("hello")

    def test_connection_refused_reported(self):
        port = self.phone.port
        self.phone.stop()
        time.sleep(0.1)
        client = PhoneClient("127.0.0.1", port, StreamSettings(), self.rec.on_event, self.rec.on_frame,
                             auto_reconnect=False)
        client.start()
        data = self.rec.wait_event("disconnected", timeout=8)
        self.assertTrue(data["reason"])
        self.rec.wait_event("stopped")
        client.stop()

    def test_stop_is_prompt(self):
        self.client.start()
        self.rec.wait_event("connected")
        t0 = time.monotonic()
        self.client.stop()
        self.assertLess(time.monotonic() - t0, 2.0)
        self.rec.wait_event("stopped")


class DiscoveryTests(unittest.TestCase):
    def test_discover_on_localhost(self):
        phone = FakePhone(port=0, udp_port=0, host="127.0.0.1").start()
        try:
            devices = discover(timeout=0.8, port=phone.udp_port, targets=["127.0.0.1"])
            self.assertEqual(len(devices), 1)
            self.assertEqual(devices[0].ip, "127.0.0.1")
            self.assertEqual(devices[0].port, phone.port)
            self.assertEqual(devices[0].name, "Sahte Telefon")
        finally:
            phone.stop()

    def test_discover_ignores_foreign_replies(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as fake:
            fake.bind(("127.0.0.1", 0))
            port = fake.getsockname()[1]

            def respond():
                data, addr = fake.recvfrom(512)
                fake.sendto(b'{"app": "BaskaUygulama"}', addr)
                fake.sendto(b"cop veri", addr)

            threading.Thread(target=respond, daemon=True).start()
            self.assertEqual(discover(timeout=0.5, port=port, targets=["127.0.0.1"]), [])


if __name__ == "__main__":
    unittest.main()
