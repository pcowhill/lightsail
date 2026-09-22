"""Message validation: shapes, bounds and unknown fields for all three applets."""

from __future__ import annotations

import json
import math

import pytest

from lightsail_demo.messages import (
    CHAT_NAME_MAX,
    CHAT_TEXT_MAX,
    InvalidMessage,
    parse_message,
    validate_chat,
    validate_draw,
    validate_game,
)


@pytest.mark.parametrize(
    "raw",
    ["", "not json", "[]", "null", "42", '"string"', '{"type": 1}', '{"no": "type"}', "{" * 100],
)
def test_parse_rejects_non_objects_and_missing_type(raw: str):
    with pytest.raises(InvalidMessage):
        parse_message(raw)


def test_parse_rejects_deep_nesting():
    deep = {"type": "draw", "from": {"x": {"y": {"z": {"w": 1}}}}}
    with pytest.raises(InvalidMessage):
        parse_message(json.dumps(deep))


# --- chat --------------------------------------------------------------------


def test_chat_valid_and_normalized():
    out = validate_chat({"type": "chat", "name": "  Ada ", "text": " hi <b>there</b> ", "extra": 1})
    assert out == {"type": "chat", "name": "Ada", "text": "hi <b>there</b>"}
    assert "extra" not in out


@pytest.mark.parametrize(
    "data",
    [
        {"type": "chat", "name": "", "text": "x"},
        {"type": "chat", "name": "a", "text": "   "},
        {"type": "chat", "name": "a" * (CHAT_NAME_MAX + 1), "text": "x"},
        {"type": "chat", "name": "a", "text": "x" * (CHAT_TEXT_MAX + 1)},
        {"type": "chat", "name": 5, "text": "x"},
        {"type": "chat", "name": "a", "text": ["x"]},
        {"type": "chat", "name": "a\x00b", "text": "x"},
        {"type": "chat", "name": "a", "text": "line\nbreak"},
        {"type": "shout", "name": "a", "text": "x"},
        {"type": "chat"},
    ],
)
def test_chat_invalid(data: dict):
    with pytest.raises(InvalidMessage):
        validate_chat(data)


# --- draw --------------------------------------------------------------------


def test_draw_valid_and_normalized():
    out = validate_draw(
        {
            "type": "draw",
            "from": {"x": 1, "y": 2.5, "z": 9},
            "to": {"x": 3, "y": 4},
            "color": "red",
            "width": 99,
        }
    )
    assert out == {"type": "draw", "from": {"x": 1.0, "y": 2.5}, "to": {"x": 3.0, "y": 4.0}, "color": "red"}
    assert validate_draw({"type": "clear", "everything": True}) == {"type": "clear"}


@pytest.mark.parametrize(
    "data",
    [
        {"type": "draw", "from": {"x": 1, "y": 2}, "to": {"x": 3, "y": 4}, "color": "#ff0000"},
        {"type": "draw", "from": {"x": 1, "y": 2}, "to": {"x": 3, "y": 4}, "color": "url(javascript:1)"},
        {"type": "draw", "from": {"x": math.nan, "y": 2}, "to": {"x": 3, "y": 4}, "color": "red"},
        {"type": "draw", "from": {"x": math.inf, "y": 2}, "to": {"x": 3, "y": 4}, "color": "red"},
        {"type": "draw", "from": {"x": 1e9, "y": 2}, "to": {"x": 3, "y": 4}, "color": "red"},
        {"type": "draw", "from": {"x": "1", "y": 2}, "to": {"x": 3, "y": 4}, "color": "red"},
        {"type": "draw", "from": {"x": True, "y": 2}, "to": {"x": 3, "y": 4}, "color": "red"},
        {"type": "draw", "from": [1, 2], "to": {"x": 3, "y": 4}, "color": "red"},
        {"type": "draw", "from": {"x": 1, "y": 2}, "color": "red"},
        {"type": "erase"},
    ],
)
def test_draw_invalid(data: dict):
    with pytest.raises(InvalidMessage):
        validate_draw(data)


# --- game --------------------------------------------------------------------


def movement(**overrides: object) -> dict:
    base = {
        "type": "movement",
        "sprite": "Robin",
        "posX": 10,
        "posY": -20.5,
        "velX": 4,
        "velY": 0,
        "rightFacing": True,
        "boosting": False,
    }
    base.update(overrides)
    return base


def test_game_movement_valid_and_normalized():
    out = validate_game(movement(image="https://evil.example/x.png", id=7))
    assert out == {
        "type": "movement",
        "sprite": "Robin",
        "posX": 10.0,
        "posY": -20.5,
        "velX": 4.0,
        "velY": 0.0,
        "rightFacing": True,
        "boosting": False,
    }
    assert "image" not in out
    assert "id" not in out  # the server assigns ids; a client cannot pick one


@pytest.mark.parametrize(
    "data",
    [
        movement(sprite="https://evil.example/x.png"),
        movement(sprite="robin"),
        movement(sprite="../../etc/passwd"),
        movement(posX=math.nan),
        movement(posY=1e12),
        movement(velX=1e6),
        movement(rightFacing="yes"),
        movement(boosting=1),
        {"type": "eat", "worm_id": -1},
        {"type": "eat", "worm_id": 10},
        {"type": "eat", "worm_id": 1.5},
        {"type": "eat", "worm_id": "1"},
        {"type": "eat", "worm_id": True},
        {"type": "eat"},
        {"type": "worms", "positions": []},  # server -> client only
        {"type": "disconnect", "id": 1},  # server -> client only
        {"type": "teleport"},
    ],
)
def test_game_invalid(data: dict):
    with pytest.raises(InvalidMessage):
        validate_game(data)


def test_game_connect_and_eat_valid():
    assert validate_game({"type": "connect", "id": 5}) == {"type": "connect"}
    assert validate_game({"type": "eat", "worm_id": 0}) == {"type": "eat", "worm_id": 0}
    assert validate_game({"type": "eat", "worm_id": 9}) == {"type": "eat", "worm_id": 9}
