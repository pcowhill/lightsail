"""Static file serving: development mode serves only public/, production serves nothing."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from lightsail_demo.static import resolve_public_path
from tests.conftest import PUBLIC_DIR, REPO_ROOT, http_get, make_settings


@pytest.fixture
async def dev_client(app_factory):
    return await app_factory(make_settings(serve_static=True, allowed_origins=None), revision=None)


@pytest.mark.parametrize(
    ("path", "needle"),
    [
        ("/", "Lightsail Tower"),
        ("/index.html", "Lightsail Tower"),
        ("/chat/", "WebSocket Chat"),
        ("/chat/index.html", "WebSocket Chat"),
        ("/draw/", "WebSocket Draw"),
        ("/draw/index.html", "WebSocket Draw"),
        ("/game/", "gameCanvas"),
        ("/game/index.html", "gameCanvas"),
        ("/shared/demo-socket.js", "DemoSocket"),
        ("/shared/demo.css", "demo-status"),
    ],
)
async def test_public_pages_are_served(dev_client, path: str, needle: str):
    response = await dev_client.get(path)
    assert response.status == 200
    assert needle in await response.text()
    assert response.headers["X-Content-Type-Options"] == "nosniff"


async def test_game_assets_are_served_with_image_type(dev_client):
    for name in (
        "Robin.png",
        "Cardinal.png",
        "Pigeon.png",
        "Woodpecker.png",
        "Grass.png",
        "Nest.png",
        "worm.png",
    ):
        response = await dev_client.get(f"/game/{name}")
        assert response.status == 200, name
        assert response.headers["Content-Type"] == "image/png"
        assert (await response.read())[:8] == b"\x89PNG\r\n\x1a\n"


async def test_directory_without_slash_redirects(dev_client):
    response = await dev_client.get("/chat", allow_redirects=False)
    assert response.status == 301
    assert response.headers["Location"] == "/chat/"
    response = await dev_client.get("/game?x=1", allow_redirects=False)
    assert response.headers["Location"] == "/game/?x=1"


@pytest.mark.parametrize(
    "path",
    [
        "/main.py",
        "/REVISION",
        "/requirements.txt",
        "/pyproject.toml",
        "/lightsail_demo/app.py",
        "/lightsail_demo/",
        "/tests/conftest.py",
        "/.git/HEAD",
        "/.git/config",
        "/.gitignore",
        "/.release-ready",
        "/.venv/bin/python",
        "/.env",
        "/shared/",  # directory without index: no listing
        "/game/.hidden",
        "/../main.py",
        "/chat/../../main.py",
        "/%2e%2e/main.py",
        "/chat/..%2f..%2fmain.py",
        "/game\\index.html",
        "/nonexistent",
        "/ws",
        "/ws/",
        "/ws/other",
    ],
)
async def test_forbidden_or_missing_paths_are_404(dev_client, path: str):
    response = await dev_client.get(path, allow_redirects=False)
    assert response.status == 404, path


async def test_no_directory_listing_for_directories_without_index(dev_client):
    response = await dev_client.get("/shared/")
    assert response.status == 404
    assert "demo-socket.js" not in await response.text()


async def test_symlinks_inside_public_are_never_followed(tmp_path: Path, app_factory):
    app_dir = tmp_path / "app"
    shutil.copytree(PUBLIC_DIR, app_dir / "public")
    (app_dir / "secret.txt").write_text("not for the web\n")
    os.symlink(app_dir / "secret.txt", app_dir / "public" / "leak.txt")
    os.symlink(app_dir, app_dir / "public" / "root")
    os.symlink("/etc", app_dir / "public" / "etc")
    client = await app_factory(
        make_settings(app_dir=app_dir, serve_static=True, allowed_origins=None), revision=None
    )
    for path in ("/leak.txt", "/root/secret.txt", "/root/", "/etc/hostname", "/etc/passwd"):
        response = await client.get(path)
        assert response.status == 404, path
    assert (await client.get("/index.html")).status == 200


async def test_only_get_and_head(dev_client):
    assert (await dev_client.head("/")).status == 200
    assert (await dev_client.post("/")).status == 405
    assert (await dev_client.put("/index.html")).status == 405


async def test_production_mode_serves_no_static_files(client):
    for path in (
        "/",
        "/index.html",
        "/chat/",
        "/chat/index.html",
        "/game/Robin.png",
        "/shared/demo-socket.js",
        "/main.py",
    ):
        response = await client.get(path)
        assert response.status == 404, path
    assert (await client.get("/healthz")).status == 200


async def test_raw_dot_segments_never_escape_public(dev_client):
    """Sent byte-for-byte (HTTP clients normalize these before sending)."""
    import asyncio

    host, port = dev_client.server.host, dev_client.server.port
    for target in ("/../main.py", "/game/./index.html", "/chat/../../lightsail_demo/app.py", "/./REVISION"):
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(f"GET {target} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        status_line = raw.split(b"\r\n", 1)[0]
        body = raw.split(b"\r\n\r\n", 1)[-1]
        assert b"aiohttp" not in body and b"REVISION" not in body, target
        assert status_line.split()[1] in (b"200", b"400", b"404"), (target, status_line)
        if status_line.split()[1] == b"200":
            assert target == "/game/./index.html" and b"gameCanvas" in body


def test_resolve_public_path_unit(tmp_path: Path):
    public = tmp_path / "public"
    (public / "chat").mkdir(parents=True)
    (public / "index.html").write_text("x")
    (public / "chat" / "index.html").write_text("x")
    (public / "chat" / ".hidden").write_text("x")
    assert resolve_public_path(public, "/") == public / "index.html"
    assert resolve_public_path(public, "/chat/") == public / "chat" / "index.html"
    assert resolve_public_path(public, "/chat") == public / "chat"  # redirect wanted
    assert resolve_public_path(public, "/chat/.hidden") is None
    assert resolve_public_path(public, "/../index.html") is None
    assert resolve_public_path(public, "index.html") is None
    assert resolve_public_path(public, "/index.html\x00") is None
    assert resolve_public_path(public, "/missing") is None


@pytest.mark.slow
def test_files_resolve_relative_to_the_application_not_the_working_directory(dev_server):
    """dev_server runs main.py with cwd=app dir; this one starts it from an unrelated cwd."""
    from tests.conftest import ServerProcess, base_env, free_port

    env = base_env()
    port = free_port()
    env.update({"HOST": "127.0.0.1", "PORT": str(port), "SERVE_STATIC": "1"})
    server = ServerProcess(REPO_ROOT, env, cwd=Path("/"))
    try:
        server.wait_ready()
        status, body, _ = http_get(f"{server.base_url}/chat/")
        assert status == 200 and b"WebSocket Chat" in body
        status, _, _ = http_get(f"{server.base_url}/etc/hostname")
        assert status == 404
    finally:
        server.stop()
