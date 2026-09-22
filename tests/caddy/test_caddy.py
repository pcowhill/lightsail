"""Isolated Caddy integration: static files from public/ plus proxied WebSockets.

A real Caddy binary runs on an ephemeral loopback port with a site block that
mirrors the production one in cowhill-infrastructure (server/Caddyfile,
contract v1) except for the address: ``http://127.0.0.1:<port>`` disables
automatic HTTPS/ACME entirely, and the admin API is off. Behind it runs the
real application in production configuration (SERVE_STATIC=0, exact Origin
allowlist) from a release-shaped directory.

Skipped unless a ``caddy`` binary is found (CADDY_BIN or PATH); REQUIRE_CADDY=1
turns the skip into a failure (CI).

What this does NOT prove: TLS, the real server's Caddyfile, DNS.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from aiohttp import ClientSession, WSMsgType, WSServerHandshakeError

from tests.conftest import FAKE_SHA, ServerProcess, base_env, free_port, http_get

pytestmark = [pytest.mark.caddy, pytest.mark.slow]

CADDY_BIN = os.environ.get("CADDY_BIN") or shutil.which("caddy")
if not CADDY_BIN:
    if os.environ.get("REQUIRE_CADDY") == "1":
        pytest.fail("REQUIRE_CADDY=1 but no caddy binary was found", pytrace=False)
    pytest.skip("caddy binary not available", allow_module_level=True)

# Mirror of the lightsail-demo site block in cowhill-infrastructure/server/Caddyfile
# (contract v1). Only the site address differs: plain http on loopback.
CADDYFILE = """{{
\tadmin off
\tauto_https off
}}

http://127.0.0.1:{caddy_port} {{
\troot * {public_root}
\t@backend path /ws/* /healthz
\thandle @backend {{
\t\treverse_proxy 127.0.0.1:{backend_port}
\t}}
\thandle {{
\t\t@dotfiles path */.*
\t\trespond @dotfiles 404
\t\tfile_server
\t}}
}}
"""


@dataclass
class Stack:
    origin: str
    caddy_port: int
    backend: ServerProcess
    release: Path

    def url(self, path: str) -> str:
        return f"{self.origin}{path}"


@pytest.fixture
def stack(release_dir: Path, tmp_path: Path) -> Iterator[Stack]:
    caddy_port = free_port()
    backend_port = free_port()
    origin = f"http://127.0.0.1:{caddy_port}"
    # Files a real release carries next to public/ (must never be reachable).
    (release_dir / ".release-ready").write_text("")
    (release_dir / "requirements.txt").write_text("aiohttp==0.0.0 --hash=sha256:0\n")

    env = base_env()
    env.update(
        {"HOST": "127.0.0.1", "PORT": str(backend_port), "SERVE_STATIC": "0", "ALLOWED_ORIGINS": origin}
    )
    backend = ServerProcess(release_dir, env, cwd=release_dir)
    backend.wait_ready()

    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(
        CADDYFILE.format(caddy_port=caddy_port, public_root=release_dir / "public", backend_port=backend_port)
    )
    subprocess.run(
        [CADDY_BIN, "validate", "--config", str(caddyfile), "--adapter", "caddyfile"],
        check=True,
        capture_output=True,
    )
    caddy = subprocess.Popen(
        [CADDY_BIN, "run", "--config", str(caddyfile), "--adapter", "caddyfile"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={
            **os.environ,
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "HOME": str(tmp_path),
        },
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            if caddy.poll() is not None:
                raise RuntimeError(f"caddy exited: {caddy.stdout.read() if caddy.stdout else ''}")
            try:
                http_get(f"{origin}/healthz", timeout=1)
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
                time.sleep(0.1)
        yield Stack(origin=origin, caddy_port=caddy_port, backend=backend, release=release_dir)
    finally:
        caddy.terminate()
        try:
            caddy.wait(timeout=10)
        except subprocess.TimeoutExpired:
            caddy.kill()
        backend.stop()


def test_static_pages_and_assets_come_from_public(stack: Stack):
    for path, needle in (
        ("/", b"Lightsail Tower"),
        ("/index.html", b"Lightsail Tower"),
        ("/chat/", b"WebSocket Chat"),
        ("/chat/index.html", b"WebSocket Chat"),
        ("/draw/", b"WebSocket Draw"),
        ("/game/", b"gameCanvas"),
        ("/game/index.html", b"gameCanvas"),
        ("/shared/demo-socket.js", b"DemoSocket"),
    ):
        status, body, _ = http_get(stack.url(path))
        assert status == 200, path
        assert needle in body, path
    status, body, headers = http_get(stack.url("/game/Robin.png"))
    assert status == 200 and headers.get("Content-Type") == "image/png" and body[:4] == b"\x89PNG"


def test_directory_without_slash_redirects(stack: Stack):
    request = urllib.request.Request(stack.url("/chat"))
    opener = urllib.request.build_opener(NoRedirect)
    try:
        response = opener.open(request, timeout=5)
        status, location = response.status, response.headers.get("Location")
    except urllib.error.HTTPError as exc:
        status, location = exc.code, exc.headers.get("Location")
    assert status in (301, 308)
    assert location.endswith("/chat/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


@pytest.mark.parametrize(
    "path",
    [
        "/.release-ready",
        "/.anything",
        "/game/.hidden",
        "/main.py",
        "/REVISION",
        "/requirements.txt",
        "/lightsail_demo/app.py",
        "/../main.py",
        "/..%2fmain.py",
        "/%2e%2e/REVISION",
        "/shared/",  # no directory listing
        "/nonexistent",
        "/ws",  # no slash: static 404, never the backend
        "/healthz/",
        "/healthz/anything",
        "/wsx",
    ],
)
def test_release_internals_and_traversal_are_404(stack: Stack, path: str):
    status, body, _ = http_get(stack.url(path))
    assert status == 404, path
    assert b"aiohttp" not in body and FAKE_SHA.encode() not in body and b"demo-socket.js" not in body


def test_healthz_is_proxied_unstripped_with_the_release_revision(stack: Stack):
    status, body, headers = http_get(stack.url("/healthz"))
    assert status == 200
    assert headers.get("Content-Type", "").startswith("application/json")
    assert json.loads(body) == {"status": "ok", "service": "lightsail-demo", "revision": FAKE_SHA}
    # A query string is forwarded too and does not break routing.
    status, body, _ = http_get(stack.url("/healthz?probe=1"))
    assert status == 200 and json.loads(body)["revision"] == FAKE_SHA


def test_backend_only_paths_never_fall_through_to_files(stack: Stack):
    # /ws/other reaches the backend (404 from aiohttp), not the static tree.
    status, _body, headers = http_get(stack.url("/ws/other"))
    assert status == 404
    assert headers.get("Server", "").startswith("Python/")  # aiohttp answered, not Caddy's file server


@pytest.mark.asyncio
async def test_websocket_upgrade_through_caddy_relays_between_two_clients(stack: Stack):
    async with ClientSession() as session:
        headers = {"Origin": stack.origin}
        # Path (with query) reaches the same-named route on the backend: if Caddy
        # stripped /ws the backend would answer 404 and the upgrade would fail.
        a = await session.ws_connect(stack.url("/ws/chat?via=caddy"), headers=headers)
        b = await session.ws_connect(stack.url("/ws/chat"), headers=headers)
        await a.send_json({"type": "chat", "name": "A", "text": "through the proxy"})
        msg = await asyncio.wait_for(b.receive(), 5)
        assert msg.type == WSMsgType.TEXT
        assert json.loads(msg.data) == {"type": "chat", "name": "A", "text": "through the proxy"}
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(a.receive(), 0.3)

        game = await session.ws_connect(stack.url("/ws/game"), headers=headers)
        worms = await asyncio.wait_for(game.receive(), 5)
        assert json.loads(worms.data)["type"] == "worms"
        for ws in (a, b, game):
            await ws.close()


@pytest.mark.asyncio
async def test_origin_check_applies_behind_the_proxy(stack: Stack):
    async with ClientSession() as session:
        with pytest.raises(WSServerHandshakeError) as info:
            await session.ws_connect(stack.url("/ws/draw"), headers={"Origin": "https://evil.example"})
        assert info.value.status == 403
        with pytest.raises(WSServerHandshakeError) as info:
            await session.ws_connect(stack.url("/ws/draw"))  # browsers always send Origin; none = refused
        assert info.value.status == 403


def test_smoke_client_passes_against_the_stack(stack: Stack):
    from tests.conftest import REPO_ROOT

    result = subprocess.run(
        ["python3", str(REPO_ROOT / "scripts" / "deploy" / "ws_smoke.py"), stack.origin, "--timeout", "5"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "worm positions" in result.stdout and "foreign Origin refused" in result.stdout


def test_caddy_follows_symlinks_which_is_why_releases_reject_them(stack: Stack):
    """Documents the threat the release inspector guards against."""
    leak = stack.release / "public" / "leak.txt"
    os.symlink(stack.release / "REVISION", leak)
    try:
        status, body, _ = http_get(stack.url("/leak.txt"))
        assert status == 200 and FAKE_SHA.encode() in body
    finally:
        leak.unlink()


def test_backend_down_gives_502_not_a_file(stack: Stack):
    stack.backend.stop()
    status, body, _ = http_get(stack.url("/healthz"))
    assert status == 502
    assert b"Lightsail Tower" not in body
    status, _, _ = http_get(stack.url("/"))
    assert status == 200  # static pages keep working while the backend is down
