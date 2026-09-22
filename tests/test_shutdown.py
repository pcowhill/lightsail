"""Process lifecycle: graceful shutdown, bind failures, invalid configuration."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time

import pytest
from aiohttp import ClientSession, WSCloseCode, WSMsgType

from lightsail_demo.app import ROOMS_KEY, create_app
from tests.conftest import FAKE_SHA, REPO_ROOT, ServerProcess, base_env, free_port, make_settings, running_app


@pytest.mark.asyncio
async def test_shutdown_closes_every_websocket_with_going_away():
    app = create_app(make_settings(), revision=FAKE_SHA)
    async with running_app(app) as server, ClientSession() as session:
        url = server.make_url("/ws/chat")
        headers = {"Origin": "http://127.0.0.1:12345"}
        # autoclose=False so the test sees the server's close frame itself
        # instead of aiohttp's automatic reply racing the transport close.
        sockets = [await session.ws_connect(url, headers=headers, autoclose=False) for _ in range(3)]
        assert len(app[ROOMS_KEY]["chat"].connections) == 3
        # Closing the server runs on_shutdown; every client must get 1001.
        await server.close()
        for ws in sockets:
            msg = await asyncio.wait_for(ws.receive(), 5)
            assert msg.type == WSMsgType.CLOSE
            assert msg.data == WSCloseCode.GOING_AWAY
            assert msg.extra == "server shutting down"
            await ws.close()
        assert app[ROOMS_KEY]["chat"].connections == {}


def run_main(env: dict[str, str], timeout: float = 15.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "main.py")],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO_ROOT),
    )


@pytest.mark.slow
def test_occupied_port_is_a_startup_failure_not_a_fallback():
    env = base_env()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        env.update({"HOST": "127.0.0.1", "PORT": str(port)})
        result = run_main(env)
    assert result.returncode == 1
    assert "could not bind 127.0.0.1" in result.stdout + result.stderr
    # ...and it did not quietly listen somewhere else.
    assert "listening on" not in result.stdout or f":{port}" in result.stdout


@pytest.mark.parametrize(
    ("key", "value", "needle"),
    [
        ("HOST", "0.0.0.0", "wildcard"),
        ("PORT", "99999", "PORT"),
        ("SERVE_STATIC", "sometimes", "SERVE_STATIC"),
        ("ALLOWED_ORIGINS", "not-an-origin", "origin"),
    ],
)
def test_invalid_configuration_exits_with_status_1(key: str, value: str, needle: str):
    env = base_env()
    env.update({"HOST": "127.0.0.1", "PORT": str(free_port()), key: value})
    result = run_main(env)
    assert result.returncode == 1
    assert "configuration error" in result.stderr
    assert needle in result.stderr


@pytest.mark.slow
def test_production_mode_without_revision_file_refuses_to_start():
    env = base_env()
    env.update(
        {
            "HOST": "127.0.0.1",
            "PORT": str(free_port()),
            "SERVE_STATIC": "0",
            "ALLOWED_ORIGINS": "https://lightsail-demo.cowhill.dev",
        }
    )
    result = run_main(env)  # the repository checkout has no REVISION file
    assert result.returncode == 1
    assert "REVISION" in result.stdout + result.stderr


@pytest.mark.slow
def test_sigterm_stops_the_process_cleanly(dev_server: ServerProcess):
    started = time.monotonic()
    code, out = dev_server.stop()
    assert code == 0, out
    assert time.monotonic() - started < 10
    assert "development" in out  # development revision was reported at startup


@pytest.mark.slow
def test_production_like_process_serves_only_backend(prod_server: ServerProcess):
    from tests.conftest import http_get

    status, body, _ = http_get(f"{prod_server.base_url}/healthz")
    assert status == 200
    assert json.loads(body) == {"status": "ok", "service": "lightsail-demo", "revision": FAKE_SHA}
    for path in ("/", "/index.html", "/chat/", "/game/Robin.png"):
        status, _, _ = http_get(f"{prod_server.base_url}{path}")
        assert status == 404, path


@pytest.mark.slow
def test_restart_on_the_same_port_right_after_serving_requests():
    """systemd restarts the unit in place: the new process must bind while the old
    process's client connections are still in TIME_WAIT (SO_REUSEADDR)."""
    from tests.conftest import REPO_ROOT, http_get

    env = base_env()
    port = free_port()
    env.update({"HOST": "127.0.0.1", "PORT": str(port), "SERVE_STATIC": "1"})
    first = ServerProcess(REPO_ROOT, env)
    try:
        first.wait_ready()
        for _ in range(5):
            assert http_get(f"{first.base_url}/healthz")[0] == 200
    finally:
        code, _ = first.stop()
    assert code == 0
    second = ServerProcess(REPO_ROOT, env)
    try:
        second.wait_ready(timeout=10)
        assert http_get(f"{second.base_url}/healthz")[0] == 200
    finally:
        code, out = second.stop()
    assert code == 0, out
    assert "address already in use" not in out
