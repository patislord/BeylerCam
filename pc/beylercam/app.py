"""BeylerCam PC arayüzü."""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import messagebox, ttk

import cv2
import numpy as np
from PIL import Image, ImageTk

from . import __version__, settings_store
from . import vcam as vcam_mod
from .client import PhoneClient, StreamSettings
from .discovery import discover
from .processing import orient, resize_to_fit
from .protocol import PORT

log = logging.getLogger("beylercam")

RESOLUTIONS = ["640x360", "640x480", "960x540", "1280x720", "1920x1080"]
FPS_OPTIONS = ["15", "24", "30", "60"]

COLOR_OK = "#1a7f37"
COLOR_WARN = "#9a6700"
COLOR_ERR = "#cf222e"
COLOR_MUTED = "#57606a"
WRAP = 250  # yan paneldeki uzun metinler paneli genişletmesin


class FrameHub:
    """İstemci iş parçacığının yazdığı, arayüz ve sanal kameranın okuduğu son kare."""

    def __init__(self):
        self._lock = threading.Lock()
        self._img: np.ndarray | None = None
        self._id = 0
        self.last_time = 0.0

    def set(self, img: np.ndarray) -> None:
        with self._lock:
            self._img = img
            self._id += 1
            self.last_time = time.monotonic()

    def clear(self) -> None:
        with self._lock:
            self._img = None
            self._id += 1

    def get(self) -> tuple[int, np.ndarray | None]:
        with self._lock:
            return self._id, self._img


def parse_resolution(text: str) -> tuple[int, int]:
    w, h = text.lower().split("x")
    return int(w), int(h)


def parse_host(text: str) -> tuple[str, int | None]:
    """'192.168.1.5  (Samsung)' veya '192.168.1.5:4747' -> (ip, port)"""
    token = text.strip().split()[0] if text.strip() else ""
    if token.count(":") == 1:
        host, port = token.split(":")
        try:
            return host, int(port)
        except ValueError:
            return host, None
    return token, None


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = settings_store.load()
        self.events: queue.Queue = queue.Queue()
        self.hub = FrameHub()
        self.client: PhoneClient | None = None
        self.vcam = vcam_mod.VirtualCamera(self.hub.get, self._on_vcam_state)

        # İstemci iş parçacığından okunur
        self.mirror = self.settings.mirror
        self.rotation = self.settings.rotation % 360

        self._photo: ImageTk.PhotoImage | None = None
        self._preview_id = -1
        self._preview_size = (0, 0)
        self._quality_job: str | None = None
        self._phone_info: dict = {}
        self._rtt_ms: float | None = None
        self._stats: dict = {}
        self._reconnecting = False

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(15, self._tick)
        if self.settings.vcam_enabled:
            self.vcam_var.set(True)
            self._start_vcam()

    # ================================================================== UI
    def _build_ui(self) -> None:
        r = self.root
        r.title(f"BeylerCam {__version__}")
        r.geometry("1180x700")
        r.minsize(940, 600)

        style = ttk.Style()
        style.configure("Muted.TLabel", foreground=COLOR_MUTED)
        style.configure("Title.TLabel", font=("Segoe UI", 15, "bold"))
        style.configure("Big.TButton", padding=(10, 6))

        main = ttk.Frame(r, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        side = ttk.Frame(main, width=320)
        side.grid(row=0, column=0, sticky="ns", padx=(0, 10))

        ttk.Label(side, text="BeylerCam", style="Title.TLabel").pack(anchor="w", pady=(0, 6))

        # ---- Bağlantı
        conn = ttk.LabelFrame(side, text="Bağlantı", padding=8)
        conn.pack(fill="x", pady=4)
        ttk.Label(conn, text="Telefon IP adresi").grid(row=0, column=0, sticky="w")
        ttk.Label(conn, text="Port").grid(row=0, column=1, sticky="w", padx=(6, 0))
        self.host_var = tk.StringVar(value=self.settings.host)
        self.host_box = ttk.Combobox(conn, textvariable=self.host_var, width=24)
        self.host_box.grid(row=1, column=0, sticky="ew")
        self.host_box.bind("<Return>", lambda e: self.toggle_connect())
        self.port_var = tk.StringVar(value=str(self.settings.port))
        ttk.Entry(conn, textvariable=self.port_var, width=7).grid(row=1, column=1, padx=(6, 0))
        btns = ttk.Frame(conn)
        btns.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        btns.columnconfigure((0, 1), weight=1)
        self.find_btn = ttk.Button(btns, text="Telefonu Bul", command=self.find_phones)
        self.find_btn.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self.connect_btn = ttk.Button(btns, text="Bağlan", style="Big.TButton", command=self.toggle_connect)
        self.connect_btn.grid(row=0, column=1, sticky="ew", padx=(3, 0))
        self.status_label = ttk.Label(conn, text="Bağlı değil", foreground=COLOR_MUTED, wraplength=WRAP)
        self.status_label.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # ---- Kamera
        cam = ttk.LabelFrame(side, text="Kamera", padding=8)
        cam.pack(fill="x", pady=4)
        cam.columnconfigure(1, weight=1)
        self.camera_var = tk.StringVar(value=self.settings.camera)
        rowf = ttk.Frame(cam)
        rowf.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Radiobutton(rowf, text="Arka", value="back", variable=self.camera_var,
                        command=self.send_settings).pack(side="left")
        ttk.Radiobutton(rowf, text="Ön", value="front", variable=self.camera_var,
                        command=self.send_settings).pack(side="left", padx=8)
        ttk.Button(rowf, text="Değiştir", command=self.switch_camera).pack(side="right")

        ttk.Label(cam, text="Çözünürlük").grid(row=1, column=0, sticky="w", pady=(6, 0))
        res = self.settings.resolution if self.settings.resolution in RESOLUTIONS else "1280x720"
        self.res_var = tk.StringVar(value=res)
        res_box = ttk.Combobox(cam, textvariable=self.res_var, values=RESOLUTIONS, state="readonly", width=12)
        res_box.grid(row=1, column=1, sticky="e", pady=(6, 0))
        res_box.bind("<<ComboboxSelected>>", lambda e: self.on_format_changed())

        ttk.Label(cam, text="FPS").grid(row=2, column=0, sticky="w", pady=(6, 0))
        fps = str(self.settings.fps) if str(self.settings.fps) in FPS_OPTIONS else "30"
        self.fps_var = tk.StringVar(value=fps)
        fps_box = ttk.Combobox(cam, textvariable=self.fps_var, values=FPS_OPTIONS, state="readonly", width=12)
        fps_box.grid(row=2, column=1, sticky="e", pady=(6, 0))
        fps_box.bind("<<ComboboxSelected>>", lambda e: self.on_format_changed())

        ttk.Label(cam, text="JPEG kalitesi").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.quality_var = tk.IntVar(value=max(30, min(95, self.settings.quality)))
        self.quality_label = ttk.Label(cam, text=str(self.quality_var.get()), width=3)
        self.quality_label.grid(row=3, column=1, sticky="e", pady=(6, 0))
        ttk.Scale(cam, from_=30, to=95, variable=self.quality_var, orient="horizontal",
                  command=self.on_quality_changed).grid(row=4, column=0, columnspan=2, sticky="ew")
        self.phone_label = ttk.Label(cam, text="", style="Muted.TLabel", wraplength=WRAP)
        self.phone_label.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # ---- Görüntü
        img = ttk.LabelFrame(side, text="Görüntü", padding=8)
        img.pack(fill="x", pady=4)
        self.mirror_var = tk.BooleanVar(value=self.mirror)
        ttk.Checkbutton(img, text="Ayna (yatay çevir)", variable=self.mirror_var,
                        command=self.on_mirror_changed).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Button(img, text="Sola 90°", command=lambda: self.rotate(-90)).grid(row=1, column=0, pady=(6, 0))
        ttk.Button(img, text="Sağa 90°", command=lambda: self.rotate(90)).grid(row=1, column=1, padx=6, pady=(6, 0))
        self.rotation_label = ttk.Label(img, text="", width=13)
        self.rotation_label.grid(row=1, column=2, pady=(6, 0))
        self._update_rotation_label()

        # ---- Sanal webcam
        vc = ttk.LabelFrame(side, text="Sanal Webcam", padding=8)
        vc.pack(fill="x", pady=4)
        self.vcam_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(vc, text="Sanal webcam'i etkinleştir", variable=self.vcam_var,
                        command=self.on_vcam_toggled).pack(anchor="w")
        initial = "Kapalı" if vcam_mod.AVAILABLE else f"Kullanılamıyor: {vcam_mod.IMPORT_ERROR}"
        self.vcam_label = ttk.Label(vc, text=initial, style="Muted.TLabel", wraplength=WRAP)
        self.vcam_label.pack(anchor="w", pady=(4, 0))

        ttk.Button(side, text="Fotoğraf Kaydet", command=self.save_snapshot).pack(fill="x", pady=(8, 0))

        # ---- Önizleme
        right = ttk.Frame(main)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(right, bg="#101418", highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self._canvas_img = self.canvas.create_image(0, 0, anchor="center")
        self._canvas_text = self.canvas.create_text(0, 0, text="", fill="#8b949e", font=("Segoe UI", 14),
                                                    justify="center")
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.stats_label = ttk.Label(right, text="", style="Muted.TLabel")
        self.stats_label.grid(row=1, column=0, sticky="w", pady=(6, 0))

    # ============================================================ Bağlantı
    def _stream_settings(self) -> StreamSettings:
        w, h = parse_resolution(self.res_var.get())
        return StreamSettings(width=w, height=h, fps=int(self.fps_var.get()),
                              quality=int(self.quality_var.get()), camera=self.camera_var.get())

    def toggle_connect(self) -> None:
        if self.client:
            self.disconnect()
        else:
            self.connect()

    def connect(self) -> None:
        host, port_in_host = parse_host(self.host_var.get())
        if not host:
            messagebox.showinfo("BeylerCam", "Telefonda görünen IP adresini girin veya 'Telefonu Bul'a basın.")
            return
        try:
            port = port_in_host or int(self.port_var.get())
            if not 0 < port < 65536:
                raise ValueError
        except ValueError:
            messagebox.showerror("BeylerCam", "Port geçersiz (varsayılan: 4747).")
            return
        self.port_var.set(str(port))
        holder: dict = {}

        def on_event(name: str, data: dict) -> None:
            # Olayı hangi istemcinin ürettiği saklanır; eski istemcilerin olayları yok sayılır
            self.events.put(("client", holder.get("client"), name, data))

        client = PhoneClient(host, port, self._stream_settings(), on_event=on_event, on_frame=self._on_frame)
        holder["client"] = client
        self.client = client
        self._reconnecting = False
        client.start()
        self.connect_btn.configure(text="Bağlantıyı Kes")
        self._set_status(f"{host}:{port} adresine bağlanılıyor…", COLOR_WARN)
        self._save_settings()

    def disconnect(self) -> None:
        client, self.client = self.client, None
        if client:
            client.stop(wait=False)
        self.hub.clear()
        self._stats = {}
        self._rtt_ms = None
        self.connect_btn.configure(text="Bağlan")
        self._set_status("Bağlı değil", COLOR_MUTED)
        self.phone_label.configure(text="")

    def _on_frame(self, img: np.ndarray, phone_rotation: int, front: bool) -> None:
        # İstemci iş parçacığında çalışır
        self.hub.set(orient(img, phone_rotation + self.rotation, self.mirror))

    def _handle_client_event(self, name: str, data: dict) -> None:
        if name == "connecting":
            if not self._reconnecting:  # kopma sebebi ekranda kalsın
                self._set_status(f"{data['host']}:{data['port']} adresine bağlanılıyor…", COLOR_WARN)
        elif name == "connected":
            self._reconnecting = False
            self._set_status(f"Bağlandı: {data['host']}", COLOR_OK)
        elif name in ("hello", "status"):
            self._phone_info.update(data)
            if data.get("camera") in ("back", "front"):
                self.camera_var.set(data["camera"])
            self._update_phone_label()
            if name == "hello" and data.get("device"):
                self._set_status(f"Bağlandı: {data['device']} ({self.client.host if self.client else ''})",
                                 COLOR_OK)
        elif name == "stats":
            self._stats = data
        elif name == "rtt":
            self._rtt_ms = data["ms"]
        elif name == "disconnected":
            self._reconnecting = True
            self.hub.clear()
            self._stats = {}
            reason = data.get("reason") or "bağlantı koptu"
            self._set_status(f"Bağlantı yok: {reason}. Yeniden deneniyor…", COLOR_ERR)

    def _update_phone_label(self) -> None:
        i = self._phone_info
        if not i:
            self.phone_label.configure(text="")
            return
        cam = "Ön" if i.get("camera") == "front" else "Arka"
        parts = [f"Telefon: {i.get('width', '?')}x{i.get('height', '?')}", f"{cam} kamera"]
        if i.get("fpsRange"):
            parts.append(f"sensör {i['fpsRange']} fps")
        req = (i.get("requestedWidth"), i.get("requestedHeight"))
        if all(req) and req != (i.get("width"), i.get("height")):
            parts.append(f"(istenen {req[0]}x{req[1]} desteklenmiyor)")
        self.phone_label.configure(text=" • ".join(parts))

    def find_phones(self) -> None:
        self.find_btn.configure(state="disabled", text="Aranıyor…")

        def worker():
            try:
                devices = discover()
                self.events.put(("discovery", devices))
            except Exception as e:
                self.events.put(("discovery_error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _handle_discovery(self, devices) -> None:
        self.find_btn.configure(state="normal", text="Telefonu Bul")
        self.host_box.configure(values=[d.label() for d in devices])
        if not devices:
            self._set_status("Telefon bulunamadı. Telefonda BeylerCam açık ve aynı Wi-Fi'da mı? "
                             "IP adresini elle de girebilirsiniz.", COLOR_ERR)
            return
        self.host_var.set(devices[0].label())
        self.port_var.set(str(devices[0].port))
        if len(devices) == 1:
            self._set_status(f"Bulundu: {devices[0].name}. 'Bağlan'a basın.", COLOR_OK)
        else:
            self._set_status(f"{len(devices)} telefon bulundu, listeden seçin.", COLOR_OK)

    # ======================================================== Ayarlar
    def send_settings(self) -> None:
        if self.client:
            self.client.update_settings(self._stream_settings())
        self._save_settings()

    def switch_camera(self) -> None:
        if self.client and self.client.connected:
            self.client.switch_camera()
        else:
            self.camera_var.set("front" if self.camera_var.get() == "back" else "back")
            self._save_settings()

    def on_format_changed(self) -> None:
        self.send_settings()
        if self.vcam_var.get():
            self._start_vcam()

    def on_quality_changed(self, _value=None) -> None:
        self.quality_label.configure(text=str(int(float(self.quality_var.get()))))
        if self._quality_job:
            self.root.after_cancel(self._quality_job)
        self._quality_job = self.root.after(300, self._apply_quality)

    def _apply_quality(self) -> None:
        self._quality_job = None
        self.quality_var.set(int(float(self.quality_var.get())))
        self.send_settings()

    def on_mirror_changed(self) -> None:
        self.mirror = bool(self.mirror_var.get())
        self._save_settings()

    def rotate(self, delta: int) -> None:
        self.rotation = (self.rotation + delta) % 360
        self._update_rotation_label()
        self._save_settings()

    def _update_rotation_label(self) -> None:
        self.rotation_label.configure(text=f"Döndürme: {self.rotation}°")

    # ======================================================= Sanal kamera
    def on_vcam_toggled(self) -> None:
        if self.vcam_var.get():
            self._start_vcam()
        else:
            self.vcam.stop()
            self.vcam_label.configure(text="Kapalı", foreground=COLOR_MUTED)
        self._save_settings()

    def _start_vcam(self) -> None:
        w, h = parse_resolution(self.res_var.get())
        self.vcam_label.configure(text="Başlatılıyor…", foreground=COLOR_WARN)
        self.vcam.start(w, h, int(self.fps_var.get()))

    def _on_vcam_state(self, state: str, message: str) -> None:
        self.events.put(("vcam", state, message))

    def _handle_vcam(self, state: str, message: str) -> None:
        if state == "running":
            self.vcam_label.configure(text=f"Açık: {message}\nZoom/Discord/Teams'te bu kamerayı seçin.",
                                      foreground=COLOR_OK)
        elif state == "error":
            self.vcam_var.set(False)
            self.vcam_label.configure(text=message, foreground=COLOR_ERR)
            self._save_settings()

    # ============================================================ Diğer
    def save_snapshot(self) -> None:
        _, img = self.hub.get()
        if img is None:
            messagebox.showinfo("BeylerCam", "Kaydedilecek görüntü yok. Önce telefona bağlanın.")
            return
        folder = Path.home() / "Pictures" / "BeylerCam"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / time.strftime("BeylerCam_%Y%m%d_%H%M%S.png")
        ok, buf = cv2.imencode(".png", img)
        if not ok:
            messagebox.showerror("BeylerCam", "Görüntü kaydedilemedi.")
            return
        path.write_bytes(buf.tobytes())  # cv2.imwrite Türkçe karakterli yollarda sorun çıkarabilir
        self._set_status(f"Kaydedildi: {path}", COLOR_OK)

    def _set_status(self, text: str, color: str) -> None:
        self.status_label.configure(text=text, foreground=color)

    def _save_settings(self) -> None:
        s = self.settings
        host, _ = parse_host(self.host_var.get())
        s.host = host
        try:
            s.port = int(self.port_var.get())
        except ValueError:
            s.port = PORT
        s.resolution = self.res_var.get()
        s.fps = int(self.fps_var.get())
        s.quality = int(float(self.quality_var.get()))
        s.camera = self.camera_var.get()
        s.mirror = self.mirror
        s.rotation = self.rotation
        s.vcam_enabled = bool(self.vcam_var.get())
        settings_store.save(s)

    # ======================================================= Ana döngü
    def _tick(self) -> None:
        try:
            self._process_events()
            self._update_preview()
            self._update_stats()
        except Exception:
            log.exception("Arayüz güncelleme hatası")
        self.root.after(15, self._tick)

    def _process_events(self) -> None:
        for _ in range(100):
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                return
            kind = ev[0]
            if kind == "client":
                _, client, name, data = ev
                if client is self.client:
                    self._handle_client_event(name, data)
            elif kind == "discovery":
                self._handle_discovery(ev[1])
            elif kind == "discovery_error":
                self.find_btn.configure(state="normal", text="Telefonu Bul")
                self._set_status(f"Arama hatası: {ev[1]}", COLOR_ERR)
            elif kind == "vcam":
                if self.vcam_var.get():
                    self._handle_vcam(ev[1], ev[2])

    def _on_canvas_resize(self, event) -> None:
        self.canvas.coords(self._canvas_img, event.width // 2, event.height // 2)
        self.canvas.coords(self._canvas_text, event.width // 2, event.height // 2)
        self._preview_id = -1  # yeni boyutta yeniden çiz

    def _update_preview(self) -> None:
        frame_id, img = self.hub.get()
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if frame_id == self._preview_id and (cw, ch) == self._preview_size:
            return
        self._preview_id = frame_id
        self._preview_size = (cw, ch)
        if img is None:
            self.canvas.itemconfigure(self._canvas_img, image="")
            self._photo = None
            msg = ("Telefon bağlı değil\n\n1) Telefonda BeylerCam'i açın\n"
                   "2) 'Telefonu Bul'a basın ya da telefonda yazan IP'yi girin\n3) 'Bağlan'a basın")
            if self.client:
                msg = "Görüntü bekleniyor…"
            self.canvas.itemconfigure(self._canvas_text, text=msg)
            return
        if cw < 10 or ch < 10:
            return
        small = resize_to_fit(img, cw, ch)
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        self._photo = ImageTk.PhotoImage(Image.fromarray(rgb))
        self.canvas.itemconfigure(self._canvas_img, image=self._photo)
        self.canvas.itemconfigure(self._canvas_text, text="")

    def _update_stats(self) -> None:
        if not self.client or not self.client.connected:
            self.stats_label.configure(text="")
            return
        _, img = self.hub.get()
        stale = time.monotonic() - self.hub.last_time > 1.5
        parts = []
        if img is not None:
            parts.append(f"{img.shape[1]}x{img.shape[0]}")
        fps = 0.0 if stale else self._stats.get("fps", 0.0)
        parts.append(f"{fps:.1f} FPS")
        if self._stats and not stale:
            parts.append(f"{self._stats.get('mbps', 0):.1f} Mbit/s")
        if self._rtt_ms is not None:
            parts.append(f"Gecikme (RTT): {self._rtt_ms:.0f} ms")
        if stale:
            parts.append("Telefondan görüntü gelmiyor (uygulama ön planda mı?)")
        self.stats_label.configure(text="   •   ".join(parts))

    def on_close(self) -> None:
        self._save_settings()
        if self.client:
            self.client.stop(wait=False)
        self.vcam.stop()
        self.root.destroy()


def _setup_logging() -> None:
    handlers: list[logging.Handler] = []
    try:
        handlers.append(logging.FileHandler(settings_store.app_dir() / "beylercam.log", encoding="utf-8"))
    except OSError:
        pass
    if sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> None:
    _setup_logging()
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()

    def report(exc, val, tb):
        log.error("Beklenmeyen hata:\n%s", "".join(traceback.format_exception(exc, val, tb)))
        messagebox.showerror("BeylerCam", f"Beklenmeyen hata: {val}")

    root.report_callback_exception = report
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
