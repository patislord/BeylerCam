"""BeylerCam ağ protokolü (v1). Android tarafı: android/.../Protocol.kt

TCP (port 4747):
  Telefon -> PC : [tip: u8][uzunluk: u32 big-endian][veri]
      HELLO  (1): JSON
      FRAME  (2): [dönüş derecesi: u16][bayraklar: u8 (bit0 = ön kamera)][JPEG]
      STATUS (3): JSON
      PONG   (4): JSON {"t": ...}
  PC -> Telefon : UTF-8 JSON satırları ('\\n' ile biter)

UDP (port 4747): PC "BEYLERCAM_DISCOVER" yayınlar, telefon JSON ile cevap verir.
"""

from __future__ import annotations

import json
import socket
import struct

PORT = 4747
VERSION = 1
APP = "BeylerCam"
DISCOVER_MAGIC = b"BEYLERCAM_DISCOVER"

MSG_HELLO = 1
MSG_FRAME = 2
MSG_STATUS = 3
MSG_PONG = 4

FLAG_FRONT = 0x01

HEADER = struct.Struct(">BI")
FRAME_HEAD = struct.Struct(">HB")
MAX_PAYLOAD = 32 * 1024 * 1024


class ProtocolError(Exception):
    pass


def encode_message(msg_type: int, payload: bytes) -> bytes:
    return HEADER.pack(msg_type, len(payload)) + payload


def encode_json_message(msg_type: int, data: dict) -> bytes:
    return encode_message(msg_type, json.dumps(data, ensure_ascii=False).encode("utf-8"))


def encode_frame(rotation: int, front: bool, jpeg: bytes) -> bytes:
    head = FRAME_HEAD.pack(rotation % 360, FLAG_FRONT if front else 0)
    return encode_message(MSG_FRAME, head + jpeg)


def decode_frame_payload(payload: bytes) -> tuple[int, bool, memoryview]:
    if len(payload) < FRAME_HEAD.size:
        raise ProtocolError("Kare verisi çok kısa")
    rotation, flags = FRAME_HEAD.unpack_from(payload, 0)
    return rotation, bool(flags & FLAG_FRONT), memoryview(payload)[FRAME_HEAD.size:]


def encode_command(cmd: dict) -> bytes:
    return json.dumps(cmd, separators=(",", ":")).encode("utf-8") + b"\n"


def recv_exact(sock: socket.socket, n: int) -> bytearray:
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        r = sock.recv_into(view[got:], n - got)
        if r == 0:
            raise ConnectionError("Bağlantı karşı taraftan kapatıldı")
        got += r
    return buf


def read_message(sock: socket.socket) -> tuple[int, bytearray]:
    msg_type, length = HEADER.unpack(recv_exact(sock, HEADER.size))
    if length > MAX_PAYLOAD:
        raise ProtocolError(f"Geçersiz mesaj uzunluğu: {length}")
    payload = recv_exact(sock, length) if length else bytearray()
    return msg_type, payload
