"""Shared fixtures.

Every server under test binds an ephemeral loopback port chosen by the
operating system; nothing here ever touches production or a fixed port.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from lightsail_demo.app import create_app
from lightsail_demo.config import Settings, load_settings

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DIR = REPO_ROOT / "public"
FAKE_SHA = "0123456789abcdef0123456789abcdef01234567"
TEST_ORIGIN = "http://127.0.0.1:12345"


def make_settings(**overrides: object) -> Settings:
    """Production-like settings for in-process tests (exact origin allowlist)."""
    base = load_settings(
        {"HOST": "127.0.0.1", "PORT": "8101", "SERVE_STATIC": "0", "ALLOWED_ORIGINS": TEST_ORIGIN},
        app_dir=REPO_ROOT,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def app_factory(aiohttp_client):
    """Create an app (revision FAKE_SHA unless given) and a test client for it."""

    async def _make(settings: Settings | None = None, *, revision: str | None = FAKE_SHA) -> TestClient:
        app = create_app(settings or make_settings(), revision=revision)
        return await aiohttp_client(app)

    return _make


@pytest.fixture
async def client(app_factory) -> TestClient:
    return await app_factory()


async def ws_connect(client: TestClient, path: str, *, origin: str | None = TEST_ORIGIN, **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    if origin is not None:
        headers["Origin"] = origin
    return await client.ws_connect(path, headers=headers, **kwargs)


async def wait_for(predicate, *, timeout: float = 3.0, interval: float = 0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


def free_port() -> int:
    """Ask the OS for a currently free loopback port (tests only)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http_get(url: str, timeout: float = 5.0) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(url, headers={"User-Agent": "lightsail-demo-tests"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def wait_for_http(url: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status, _, _ = http_get(url, timeout=1.0)
            if status < 500:
                return
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            last = exc
        time.sleep(0.05)
    raise RuntimeError(f"server at {url} did not come up: {last}")


class ServerProcess:
    """A real ``python main.py`` process on an ephemeral loopback port."""

    def __init__(self, app_dir: Path, env: dict[str, str], *, cwd: Path | None = None) -> None:
        self.app_dir = app_dir
        self.port = int(env["PORT"])
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.env = env
        self.process = subprocess.Popen(
            [sys.executable, str(app_dir / "main.py")],
            cwd=str(cwd or app_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def wait_ready(self, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                out = self.process.stdout.read() if self.process.stdout else ""
                raise RuntimeError(f"server exited early ({self.process.returncode}):\n{out}")
            try:
                status, _, _ = http_get(f"{self.base_url}/healthz", timeout=1.0)
                if status == 200:
                    return
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
                pass
            time.sleep(0.05)
        raise RuntimeError("server did not become healthy in time")

    def stop(self, timeout: float = 10.0) -> tuple[int, str]:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        out = self.process.stdout.read() if self.process.stdout else ""
        return self.process.returncode, out


def base_env() -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items() if k not in {"HOST", "PORT", "SERVE_STATIC", "ALLOWED_ORIGINS"}
    }
    env["PYTHONUNBUFFERED"] = "1"
    return env


@pytest.fixture
def dev_server() -> Iterator[ServerProcess]:
    """Development-mode server: static files on, loopback origin policy, revision 'development'."""
    env = base_env()
    port = free_port()
    env.update({"HOST": "127.0.0.1", "PORT": str(port), "SERVE_STATIC": "1"})
    server = ServerProcess(REPO_ROOT, env)
    try:
        server.wait_ready()
        yield server
    finally:
        server.stop()


@pytest.fixture
def release_dir(tmp_path: Path) -> Path:
    """A copy of the runtime files laid out like a deployed release, with REVISION."""
    target = tmp_path / "releases" / FAKE_SHA
    target.mkdir(parents=True)
    shutil.copy(REPO_ROOT / "main.py", target / "main.py")
    shutil.copytree(
        REPO_ROOT / "lightsail_demo", target / "lightsail_demo", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copytree(PUBLIC_DIR, target / "public")
    (target / "REVISION").write_text(FAKE_SHA + "\n")
    return target


@pytest.fixture
def prod_server(release_dir: Path) -> Iterator[ServerProcess]:
    """Production-like server from a release copy: static off, exact origin allowlist."""
    env = base_env()
    port = free_port()
    env.update(
        {
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "SERVE_STATIC": "0",
            "ALLOWED_ORIGINS": f"http://127.0.0.1:{port}",
        }
    )
    server = ServerProcess(release_dir, env, cwd=release_dir.parent)
    try:
        server.wait_ready()
        yield server
    finally:
        server.stop()


@contextlib.asynccontextmanager
async def running_app(app: web.Application) -> AsyncIterator[TestServer]:
    server = TestServer(app)
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()
