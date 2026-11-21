"""Yerel ağda BeylerCam çalıştıran telefonları UDP yayını ile bulur."""

from __future__ import annotations

import ipaddress
import json
import socket
import time
from dataclasses import dataclass

from .protocol import APP, DISCOVER_MAGIC, PORT


@dataclass(frozen=True)
class FoundDevice:
    ip: str
    port: int
    name: str

    def label(self) -> str:
        return f"{self.ip}  ({self.name})"


def _local_ipv4_addresses() -> list[str]:
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    # Varsayılan rota üzerindeki arayüz (paket gönderilmez)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.") and not ip.startswith("169.254.")]


def _broadcast_targets() -> list[str]:
    targets = {"255.255.255.255"}
    for ip in _local_ipv4_addresses():
        # Alt ağ maskesini bilmediğimiz için yaygın olan /24 varsayılır
        targets.add(str(ipaddress.ip_network(f"{ip}/24", strict=False).broadcast_address))
    return sorted(targets)


def discover(timeout: float = 1.5, port: int = PORT, targets: list[str] | None = None) -> list[FoundDevice]:
    """Ağdaki telefonları arar. `targets` verilirse yalnızca o adreslere sorar."""
    found: dict[str, FoundDevice] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", 0))
        sock.settimeout(0.2)
        for target in targets or _broadcast_targets():
            try:
                sock.sendto(DISCOVER_MAGIC, (target, port))
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, (ip, _) = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                # Windows: ulaşılamayan hedefler ICMP ile ConnectionResetError üretebilir
                continue
            try:
                info = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(info, dict) or info.get("app") != APP:
                continue
            found[ip] = FoundDevice(ip=ip, port=int(info.get("port", PORT)), name=str(info.get("device", "Telefon")))
    return sorted(found.values(), key=lambda d: d.ip)
