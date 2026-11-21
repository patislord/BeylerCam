"""Kullanıcı ayarlarını %APPDATA%\\BeylerCam\\settings.json içinde saklar."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

log = logging.getLogger(__name__)


def app_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    path = Path(base) / "BeylerCam"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class AppSettings:
    host: str = ""
    port: int = 4747
    resolution: str = "1280x720"
    fps: int = 30
    quality: int = 80
    camera: str = "back"
    mirror: bool = False
    rotation: int = 0
    vcam_enabled: bool = False


def load(path: Path | None = None) -> AppSettings:
    path = path or app_dir() / "settings.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return AppSettings()
    except (OSError, ValueError) as e:
        log.warning("Ayarlar okunamadı (%s), varsayılanlar kullanılıyor", e)
        return AppSettings()
    defaults = AppSettings()
    values = {}
    for f in fields(AppSettings):
        default = getattr(defaults, f.name)
        value = raw.get(f.name, default)
        values[f.name] = value if isinstance(value, type(default)) else default
    return AppSettings(**values)


def save(settings: AppSettings, path: Path | None = None) -> None:
    path = path or app_dir() / "settings.json"
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(settings), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        log.warning("Ayarlar kaydedilemedi: %s", e)
