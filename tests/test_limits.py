"""Origin handling, connection caps, message size, rate limits, slow receivers."""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import WSCloseCode, WSMsgType, WSServerHandshakeError

from lightsail_demo.app import ROOMS_KEY
from lightsail_demo.ws import INVALID_MESSAGE_LIMIT, TokenBucket
from tests.conftest import TEST_ORIGIN, make_settings, wait_for, ws_connect


async def expect_close(ws, code: int, timeout: float = 3.0):
    """Drain until the server's close frame arrives and check the code it carries."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        msg = await asyncio.wait_for(ws.receive(), timeout)
        if msg.type == WSMsgType.CLOSE:
            assert msg.data == code, (msg.data, msg.extra, code)
            return
        if msg.type in (WSMsgType.CLOSING, WSMsgType.CLOSED):
            # The frame was consumed by aiohttp's automatic close reply.
            assert ws.close_code == code, (ws.close_code, code)
            return
    raise AssertionError("no close frame received")


# --- Origin --------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "",
        "null",
        "https://evil.example",
        "https://127.0.0.1:12345",
        TEST_ORIGIN + ".evil.example",
        "http://localhost:8080",
    ],
)
async def test_upgrade_refused_for_missing_or_foreign_origin(client, origin):
    with pytest.raises(WSServerHandshakeError) as info:
        await ws_connect(client, "/ws/chat", origin=origin)
    assert info.value.status == 403
    assert len(client.server.app[ROOMS_KEY]["chat"].connections) == 0


async def test_upgrade_allowed_for_exact_origin_only_case_insensitively(client):
    ws = await ws_connect(client, "/ws/chat", origin=TEST_ORIGIN.upper())
    assert not ws.closed
    await ws.close()


async def test_development_policy_accepts_loopback_and_refuses_others(app_factory):
    client = await app_factory(make_settings(allowed_origins=None), revision=None)
    ws = await ws_connect(client, "/ws/chat", origin="http://localhost:8080")
    await ws.close()
    ws = await ws_connect(client, "/ws/chat", origin="http://127.0.0.1:5173")
    await ws.close()
    with pytest.raises(WSServerHandshakeError):
        await ws_connect(client, "/ws/chat", origin="http://192.168.0.5:8080")
    with pytest.raises(WSServerHandshakeError):
        await ws_connect(client, "/ws/chat", origin="https://lightsail-demo.cowhill.dev")


async def test_origin_check_is_not_authentication_a_forged_origin_passes(client):
    """Documented limitation: any non-browser client can send the allowed Origin."""
    ws = await client.ws_connect("/ws/chat", headers={"Origin": TEST_ORIGIN, "User-Agent": "curl/8"})
    assert not ws.closed
    await ws.close()


async def test_plain_get_on_ws_path_is_not_a_success(client):
    response = await client.get("/ws/chat", headers={"Origin": TEST_ORIGIN})
    assert response.status == 400


# --- capacity --------------------------------------------------------------------


async def test_connection_cap_per_applet_returns_503(app_factory):
    client = await app_factory(make_settings(ws_max_connections=2))
    a = await ws_connect(client, "/ws/draw")
    b = await ws_connect(client, "/ws/draw")
    with pytest.raises(WSServerHandshakeError) as info:
        await ws_connect(client, "/ws/draw")
    assert info.value.status == 503
    assert info.value.headers.get("Retry-After") == "10"
    # The cap is per applet: chat still accepts.
    c = await ws_connect(client, "/ws/chat")
    await a.close()
    # A slot frees up once a client leaves.
    rooms = client.server.app[ROOMS_KEY]
    assert await wait_for(lambda: len(rooms["draw"].connections) == 1)
    d = await ws_connect(client, "/ws/draw")
    for ws in (b, c, d):
        await ws.close()


# --- message size ----------------------------------------------------------------


async def test_oversized_message_closes_with_1009_and_others_survive(app_factory):
    client = await app_factory(make_settings(ws_max_message_bytes=512))
    a = await ws_connect(client, "/ws/chat")
    b = await ws_connect(client, "/ws/chat")
    await a.send_str("x" * 600)
    await expect_close(a, WSCloseCode.MESSAGE_TOO_BIG)
    rooms = client.server.app[ROOMS_KEY]
    assert await wait_for(lambda: len(rooms["chat"].connections) == 1)
    c = await ws_connect(client, "/ws/chat")
    await c.send_json({"type": "chat", "name": "c", "text": "fine"})
    msg = await asyncio.wait_for(b.receive(), 3)
    assert json.loads(msg.data)["text"] == "fine"
    await b.close()
    await c.close()


async def test_binary_frames_are_refused(client):
    a = await ws_connect(client, "/ws/draw")
    await a.send_bytes(b"\x00\x01")
    await expect_close(a, WSCloseCode.UNSUPPORTED_DATA)


# --- rate limit --------------------------------------------------------------------


def test_token_bucket_refills_at_rate():
    now = [0.0]
    bucket = TokenBucket(rate=10, burst=5, clock=lambda: now[0])
    assert [bucket.allow() for _ in range(6)] == [True] * 5 + [False]
    now[0] += 0.25  # 2.5 tokens
    assert [bucket.allow() for _ in range(3)] == [True, True, False]
    now[0] += 100
    assert [bucket.allow() for _ in range(6)] == [True] * 5 + [False]  # capped at burst


async def test_rate_limit_closes_flooding_client_with_1008(app_factory):
    client = await app_factory(make_settings(ws_rate_limit_per_second=10, ws_rate_limit_burst=20))
    flood = await ws_connect(client, "/ws/draw")
    peer = await ws_connect(client, "/ws/draw")
    for _ in range(40):
        await flood.send_json({"type": "clear"})
    await expect_close(flood, WSCloseCode.POLICY_VIOLATION)
    # The peer received at most the burst worth of messages and stays connected.
    received = 0
    while True:
        try:
            msg = await asyncio.wait_for(peer.receive(), 0.2)
        except asyncio.TimeoutError:
            break
        if msg.type != WSMsgType.TEXT:
            break
        received += 1
    assert 1 <= received <= 20
    assert not peer.closed
    await peer.close()


async def test_repeated_invalid_messages_close_the_connection(client):
    a = await ws_connect(client, "/ws/chat")
    for _ in range(INVALID_MESSAGE_LIMIT):
        await a.send_str("garbage")
    await expect_close(a, WSCloseCode.POLICY_VIOLATION)


# --- slow receiver -------------------------------------------------------------------


async def test_slow_receiver_is_dropped_without_blocking_others(app_factory):
    """A client whose socket never drains is dropped once its bounded queue is full;
    a client that keeps up receives every message meanwhile."""
    client = await app_factory(make_settings(ws_send_queue_limit=8))
    sender = await ws_connect(client, "/ws/draw")
    fast = await ws_connect(client, "/ws/draw")
    rooms = client.server.app[ROOMS_KEY]
    before = set(rooms["draw"].connections)
    slow = await ws_connect(client, "/ws/draw")
    (slow_id,) = set(rooms["draw"].connections) - before
    slow_conn = rooms["draw"].connections[slow_id]
    # Simulate a receiver whose socket never drains: stall its sender task.
    stall = asyncio.Event()

    async def stalled_send(text: str) -> None:
        await stall.wait()

    slow_conn.ws.send_str = stalled_send  # type: ignore[method-assign]

    total = 30
    for i in range(total):
        await sender.send_json(
            {"type": "draw", "from": {"x": i, "y": 0}, "to": {"x": i, "y": 1}, "color": "red"}
        )
        await asyncio.sleep(0.005)

    got = 0
    while got < total:
        msg = await asyncio.wait_for(fast.receive(), 3)
        assert msg.type == WSMsgType.TEXT
        got += 1
    assert got == total
    assert await wait_for(lambda: slow_conn.id not in rooms["draw"].connections)
    assert slow_conn.dropped
    stall.set()
    await expect_close(slow, WSCloseCode.POLICY_VIOLATION)
    await sender.close()
    await fast.close()


async def test_forwarded_headers_do_not_change_limits(app_factory):
    """Limits are per connection; X-Forwarded-For is not trusted or used for anything."""
    client = await app_factory(make_settings(ws_max_connections=1))
    a = await ws_connect(client, "/ws/game", headers={"X-Forwarded-For": "1.1.1.1"})
    with pytest.raises(WSServerHandshakeError) as info:
        await ws_connect(client, "/ws/game", headers={"X-Forwarded-For": "2.2.2.2"})
    assert info.value.status == 503
    await a.close()
