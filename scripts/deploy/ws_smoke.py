#!/usr/bin/env python3
"""Lightweight WebSocket smoke check against the public site (standard library only).

Opens ``wss://HOST/ws/game`` with the production Origin, expects the server's
initial ``worms`` message (so nothing has to be *sent* to real users) and
closes cleanly. Then confirms that a foreign Origin is refused with HTTP 403.
Also usable against a local ``http://`` server in tests.

Usage: ws_smoke.py https://lightsail-demo.cowhill.dev [--timeout 10]
Exit 0 on success; a one-line reason and exit 1 otherwise.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import ssl
import struct
import sys
from urllib.parse import urlsplit

WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def connect(base: str, path: str, origin: str, timeout: float) -> tuple[socket.socket, int, str]:
    parts = urlsplit(base)
    tls = parts.scheme == "https"
    host = parts.hostname or ""
    port = parts.port or (443 if tls else 80)
    raw = socket.create_connection((host, port), timeout=timeout)
    if tls:
        context = ssl.create_default_context()
        sock: socket.socket = context.wrap_socket(raw, server_hostname=host)
    else:
        sock = raw
    key = base64.b64encode(os.urandom(16)).decode()
    host_header = host if port in (80, 443) else f"{host}:{port}"
    request = (
        f"GET {path} HTTP/1.1\r\nHost: {host_header}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: {origin}\r\n"
        "User-Agent: lightsail-demo-smoke\r\n\r\n"
    ).encode()
    sock.sendall(request)
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            break
        head += chunk
        if len(head) > 65536:
            raise RuntimeError("handshake response too large")
    status_line, _, rest = head.partition(b"\r\n")
    try:
        status = int(status_line.split()[1])
    except (IndexError, ValueError):
        raise RuntimeError(f"bad handshake response: {status_line!r}") from None
    return sock, status, rest.decode("latin-1")


def read_frame(sock: socket.socket) -> tuple[int, bytes]:
    def exactly(n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                raise RuntimeError("connection closed mid-frame")
            data += chunk
        return data

    first, second = exactly(2)
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", exactly(2))
    elif length == 127:
        (length,) = struct.unpack("!Q", exactly(8))
    if length > 1024 * 1024:
        raise RuntimeError("frame too large")
    mask = exactly(4) if second & 0x80 else b""
    payload = exactly(length)
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def send_close(sock: socket.socket) -> None:
    payload = struct.pack("!H", 1000)
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes([0x88, 0x80 | len(payload)]) + mask + masked)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base", help="site base URL, e.g. https://lightsail-demo.cowhill.dev")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    base = args.base.rstrip("/")
    origin = base
    try:
        sock, status, _ = connect(base, "/ws/game", origin, args.timeout)
        if status != 101:
            print(f"FAIL: /ws/game upgrade with Origin {origin} returned HTTP {status}, expected 101")
            return 1
        opcode, payload = read_frame(sock)
        if opcode != 0x1:
            print(f"FAIL: expected a text frame from /ws/game, got opcode {opcode}")
            return 1
        message = json.loads(payload)
        if message.get("type") != "worms" or not isinstance(message.get("positions"), list):
            print(f"FAIL: unexpected first game message: {payload[:200]!r}")
            return 1
        send_close(sock)
        sock.close()
        count = len(message["positions"])
        print(f"ok: upgrade through {base}/ws/game with Origin {origin}; received {count} worm positions")

        sock, status, _ = connect(base, "/ws/chat", "https://not-the-demo.example", args.timeout)
        sock.close()
        if status != 403:
            print(f"FAIL: foreign Origin was answered with HTTP {status}, expected 403")
            return 1
        print("ok: foreign Origin refused with 403")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
