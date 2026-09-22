"""Multi-client behaviour of the chat, draw and game WebSocket rooms."""

from __future__ import annotations

import asyncio
import json
import random

import pytest
from aiohttp import WSMsgType

from lightsail_demo.app import ROOMS_KEY
from lightsail_demo.messages import GAME_WORM_COUNT
from tests.conftest import wait_for, ws_connect


async def recv_json(ws, timeout: float = 3.0) -> dict:
    msg = await asyncio.wait_for(ws.receive(), timeout)
    assert msg.type == WSMsgType.TEXT, msg
    return json.loads(msg.data)


async def assert_silent(ws, timeout: float = 0.2) -> None:
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(ws.receive(), timeout)


# --- chat --------------------------------------------------------------------


async def test_chat_relays_to_every_other_client_exactly_once(client):
    a = await ws_connect(client, "/ws/chat")
    b = await ws_connect(client, "/ws/chat")
    c = await ws_connect(client, "/ws/chat")
    await a.send_json({"type": "chat", "name": "Ada", "text": "hello"})
    assert await recv_json(b) == {"type": "chat", "name": "Ada", "text": "hello"}
    assert await recv_json(c) == {"type": "chat", "name": "Ada", "text": "hello"}
    await assert_silent(a)  # the sender does not get an echo
    await assert_silent(b)  # ...and nobody gets it twice
    for ws in (a, b, c):
        await ws.close()


async def test_chat_html_like_text_is_relayed_verbatim_as_data(client):
    """The server does not sanitize markup; the browser renders it as text (browser test)."""
    a = await ws_connect(client, "/ws/chat")
    b = await ws_connect(client, "/ws/chat")
    payload = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
    await a.send_json({"type": "chat", "name": "<b>bold</b>", "text": payload})
    got = await recv_json(b)
    assert got["name"] == "<b>bold</b>"
    assert got["text"] == payload
    await a.close()
    await b.close()


async def test_chat_ignores_invalid_messages_without_affecting_others(client):
    a = await ws_connect(client, "/ws/chat")
    b = await ws_connect(client, "/ws/chat")
    for bad in (
        "not json",
        "[]",
        '{"type":"chat"}',
        '{"type":"chat","name":"","text":"x"}',
        json.dumps({"type": "chat", "name": "a", "text": "x" * 600}),
    ):
        await a.send_str(bad)
    await a.send_json({"type": "chat", "name": "a", "text": "still fine"})
    assert (await recv_json(b))["text"] == "still fine"
    assert not a.closed
    await a.close()
    await b.close()


# --- draw --------------------------------------------------------------------


async def test_draw_strokes_and_clear_are_relayed_normalized(client):
    a = await ws_connect(client, "/ws/draw")
    b = await ws_connect(client, "/ws/draw")
    await a.send_json(
        {"type": "draw", "from": {"x": 1, "y": 2}, "to": {"x": 3, "y": 4}, "color": "blue", "junk": 1}
    )
    assert await recv_json(b) == {
        "type": "draw",
        "from": {"x": 1.0, "y": 2.0},
        "to": {"x": 3.0, "y": 4.0},
        "color": "blue",
    }
    await b.send_json({"type": "clear"})
    assert await recv_json(a) == {"type": "clear"}
    await assert_silent(b)
    await a.close()
    await b.close()


async def test_draw_rejects_bad_colors_and_coordinates(client):
    a = await ws_connect(client, "/ws/draw")
    b = await ws_connect(client, "/ws/draw")
    await a.send_json({"type": "draw", "from": {"x": 1, "y": 2}, "to": {"x": 3, "y": 4}, "color": "url(x)"})
    await a.send_str('{"type":"draw","from":{"x":NaN,"y":2},"to":{"x":3,"y":4},"color":"red"}')
    await a.send_str('{"type":"draw","from":{"x":Infinity,"y":2},"to":{"x":3,"y":4},"color":"red"}')
    await a.send_json({"type": "draw", "from": {"x": 1e9, "y": 2}, "to": {"x": 3, "y": 4}, "color": "red"})
    await assert_silent(b)
    await a.close()
    await b.close()


# --- game --------------------------------------------------------------------


def movement(**overrides: object) -> dict:
    base = {
        "type": "movement",
        "sprite": "Cardinal",
        "posX": 1,
        "posY": 2,
        "velX": 4,
        "velY": 0,
        "rightFacing": True,
        "boosting": False,
    }
    base.update(overrides)
    return base


async def test_game_connect_receives_worms_and_movement_is_relayed_with_server_id(client):
    a = await ws_connect(client, "/ws/game")
    worms = await recv_json(a)
    assert worms["type"] == "worms"
    assert len(worms["positions"]) == GAME_WORM_COUNT
    assert all(len(p) == 2 and all(-1000 <= v <= 1000 for v in p) for p in worms["positions"])

    b = await ws_connect(client, "/ws/game")
    assert (await recv_json(b))["type"] == "worms"

    await a.send_json(movement(id=999, image="https://evil.example/x.png"))
    got = await recv_json(b)
    assert got["type"] == "movement" and got["sprite"] == "Cardinal"
    assert isinstance(got["id"], int) and got["id"] != 999  # the server assigns ids
    assert "image" not in got
    await assert_silent(a)

    await b.send_json({"type": "connect"})
    got = await recv_json(a)
    assert got["type"] == "connect" and isinstance(got["id"], int)
    await a.close()
    await b.close()


async def test_game_eat_updates_every_client_exactly_once(client):
    """Regression test for the eat broadcast that used to send to the eater N times."""
    rooms = client.server.app[ROOMS_KEY]
    game = rooms["game"]
    game.rng = random.Random(1234)
    clients = [await ws_connect(client, "/ws/game") for _ in range(4)]
    before = None
    for ws in clients:
        before = (await recv_json(ws))["positions"]

    eater = clients[1]
    await eater.send_json({"type": "eat", "worm_id": 3})

    for ws in clients:  # every client, including the eater
        got = await recv_json(ws)
        assert got["type"] == "worms"
        assert len(got["positions"]) == GAME_WORM_COUNT
        assert got["positions"][3] != before[3]
        assert [p for i, p in enumerate(got["positions"]) if i != 3] == [
            p for i, p in enumerate(before) if i != 3
        ]
        assert got["positions"] == game.worms_message()["positions"]
    for ws in clients:  # ...and exactly once
        await assert_silent(ws)
    for ws in clients:
        await ws.close()


async def test_game_bad_worm_index_and_unknown_types_are_ignored(client):
    a = await ws_connect(client, "/ws/game")
    b = await ws_connect(client, "/ws/game")
    await recv_json(a)
    await recv_json(b)
    for bad in (
        {"type": "eat", "worm_id": 10},
        {"type": "eat", "worm_id": -1},
        {"type": "eat", "worm_id": "3"},
        {"type": "eat"},
        {"type": "worms", "positions": []},
        {"type": "disconnect", "id": 1},
        {
            "type": "movement",
            "sprite": "https://evil.example/x.png",
            "posX": 0,
            "posY": 0,
            "velX": 0,
            "velY": 0,
            "rightFacing": True,
            "boosting": False,
        },
    ):
        await a.send_json(bad)
    await assert_silent(b)
    await assert_silent(a)
    assert not a.closed
    await a.close()
    await b.close()


async def test_game_disconnect_is_announced_with_the_leaving_id(client):
    a = await ws_connect(client, "/ws/game")
    b = await ws_connect(client, "/ws/game")
    await recv_json(a)
    await recv_json(b)
    await a.send_json(movement())
    a_id = (await recv_json(b))["id"]
    await a.close()
    got = await recv_json(b)
    assert got == {"type": "disconnect", "id": a_id}
    rooms = client.server.app[ROOMS_KEY]
    assert await wait_for(lambda: len(rooms["game"].connections) == 1)
    await b.close()
    assert await wait_for(lambda: len(rooms["game"].connections) == 0)


async def test_rooms_are_isolated_from_each_other(client):
    chat = await ws_connect(client, "/ws/chat")
    draw = await ws_connect(client, "/ws/draw")
    await chat.send_json({"type": "chat", "name": "a", "text": "x"})
    await draw.send_json({"type": "clear"})
    await assert_silent(chat)
    await assert_silent(draw)
    await chat.close()
    await draw.close()


async def test_abrupt_disconnect_cleans_up_and_others_keep_working(client):
    a = await ws_connect(client, "/ws/chat")
    b = await ws_connect(client, "/ws/chat")
    c = await ws_connect(client, "/ws/chat")
    rooms = client.server.app[ROOMS_KEY]
    assert len(rooms["chat"].connections) == 3
    # Drop the transport without a close frame.
    a._response.connection.transport.abort()
    assert await wait_for(lambda: len(rooms["chat"].connections) == 2)
    await b.send_json({"type": "chat", "name": "b", "text": "after"})
    assert (await recv_json(c))["text"] == "after"
    await b.close()
    await c.close()
