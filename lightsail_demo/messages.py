"""Validation of the JSON messages the three applets exchange.

Every inbound text frame is parsed and validated here before anything is
done with it. Validation is by construction: a validator returns a *new*
dictionary containing only the known fields with checked types and bounds,
so unknown fields, oversized strings, non-finite numbers and wrong shapes
never reach other clients. An invalid message raises :class:`InvalidMessage`;
the caller decides whether to ignore it or close the connection.

Wire formats (all JSON objects with a ``type`` field):

chat
    ``{"type": "chat", "name": <str>, "text": <str>}``

draw
    ``{"type": "draw", "from": {"x": <num>, "y": <num>}, "to": {...}, "color": <name>}``
    ``{"type": "clear"}``

game (client -> server)
    ``{"type": "movement", "sprite": <name>, "posX": <num>, "posY": <num>,
    "velX": <num>, "velY": <num>, "rightFacing": <bool>, "boosting": <bool>}``
    ``{"type": "connect"}``
    ``{"type": "eat", "worm_id": <int>}``

game (server -> client) adds ``"id": <int>`` to relayed ``movement`` and
``connect`` messages, sends ``{"type": "disconnect", "id": <int>}`` when a
player leaves and ``{"type": "worms", "positions": [[x, y], ...]}`` with the
shared worm positions.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from typing import Any

# Chat limits (characters).
CHAT_NAME_MAX = 32
CHAT_TEXT_MAX = 500

# Drawing: the canvas is 600x400 CSS pixels; coordinates are allowed a wide
# margin so scaled or touch input near the edge is not rejected.
DRAW_COORD_MIN = -1000.0
DRAW_COORD_MAX = 10000.0
DRAW_COLORS = frozenset({"white", "black", "red", "green", "blue", "purple"})

# Game: sprites are names of images shipped in public/game/, never URLs.
GAME_SPRITES = frozenset({"Robin", "Cardinal", "Pigeon", "Woodpecker"})
GAME_POSITION_LIMIT = 1_000_000.0
GAME_VELOCITY_LIMIT = 100.0
GAME_WORM_COUNT = 10
GAME_WORLD_LIMIT = 1000  # worms spawn within [-1000, 1000] on both axes

MAX_JSON_DEPTH = 4


class InvalidMessage(ValueError):
    """The message is malformed, of an unknown type or out of bounds."""


def _require_object(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise InvalidMessage(f"{what} must be a JSON object")
    return value


def _string(data: Mapping[str, Any], key: str, *, max_length: int, min_length: int = 1) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise InvalidMessage(f"{key} must be a string")
    value = value.strip()
    if any(ch < " " or ch == "\x7f" for ch in value):
        raise InvalidMessage(f"{key} must not contain control characters")
    if not min_length <= len(value) <= max_length:
        raise InvalidMessage(f"{key} must be between {min_length} and {max_length} characters")
    return value


def _number(data: Mapping[str, Any], key: str, *, minimum: float, maximum: float) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidMessage(f"{key} must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise InvalidMessage(f"{key} must be finite")
    if not minimum <= value <= maximum:
        raise InvalidMessage(f"{key} out of range")
    return value


def _boolean(data: Mapping[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise InvalidMessage(f"{key} must be true or false")
    return value


def _choice(data: Mapping[str, Any], key: str, allowed: frozenset[str]) -> str:
    value = data.get(key)
    if not isinstance(value, str) or value not in allowed:
        raise InvalidMessage(f"{key} must be one of {sorted(allowed)}")
    return value


def _point(data: Mapping[str, Any], key: str) -> dict[str, float]:
    point = _require_object(data.get(key), key)
    return {
        "x": _number(point, "x", minimum=DRAW_COORD_MIN, maximum=DRAW_COORD_MAX),
        "y": _number(point, "y", minimum=DRAW_COORD_MIN, maximum=DRAW_COORD_MAX),
    }


def _depth(value: Any, level: int = 0) -> int:
    if level > MAX_JSON_DEPTH:
        return level
    if isinstance(value, dict):
        return max((_depth(v, level + 1) for v in value.values()), default=level)
    if isinstance(value, list):
        return max((_depth(v, level + 1) for v in value), default=level)
    return level


def parse_message(raw: str) -> dict[str, Any]:
    """Parse a JSON text frame into an object with a string ``type``."""
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise InvalidMessage("not valid JSON") from None
    data = _require_object(data, "message")
    if _depth(data) > MAX_JSON_DEPTH:
        raise InvalidMessage("message too deeply nested")
    kind = data.get("type")
    if not isinstance(kind, str):
        raise InvalidMessage("type must be a string")
    return dict(data)


# --- chat ------------------------------------------------------------------


def validate_chat(data: Mapping[str, Any]) -> dict[str, Any]:
    if data.get("type") != "chat":
        raise InvalidMessage("unknown chat message type")
    return {
        "type": "chat",
        "name": _string(data, "name", max_length=CHAT_NAME_MAX),
        "text": _string(data, "text", max_length=CHAT_TEXT_MAX),
    }


# --- draw ------------------------------------------------------------------


def validate_draw(data: Mapping[str, Any]) -> dict[str, Any]:
    kind = data.get("type")
    if kind == "clear":
        return {"type": "clear"}
    if kind == "draw":
        return {
            "type": "draw",
            "from": _point(data, "from"),
            "to": _point(data, "to"),
            "color": _choice(data, "color", DRAW_COLORS),
        }
    raise InvalidMessage("unknown draw message type")


# --- game ------------------------------------------------------------------


def validate_game(data: Mapping[str, Any]) -> dict[str, Any]:
    kind = data.get("type")
    if kind == "movement":
        return {
            "type": "movement",
            "sprite": _choice(data, "sprite", GAME_SPRITES),
            "posX": _number(data, "posX", minimum=-GAME_POSITION_LIMIT, maximum=GAME_POSITION_LIMIT),
            "posY": _number(data, "posY", minimum=-GAME_POSITION_LIMIT, maximum=GAME_POSITION_LIMIT),
            "velX": _number(data, "velX", minimum=-GAME_VELOCITY_LIMIT, maximum=GAME_VELOCITY_LIMIT),
            "velY": _number(data, "velY", minimum=-GAME_VELOCITY_LIMIT, maximum=GAME_VELOCITY_LIMIT),
            "rightFacing": _boolean(data, "rightFacing"),
            "boosting": _boolean(data, "boosting"),
        }
    if kind == "connect":
        return {"type": "connect"}
    if kind == "eat":
        worm_id = data.get("worm_id")
        if isinstance(worm_id, bool) or not isinstance(worm_id, int):
            raise InvalidMessage("worm_id must be an integer")
        if not 0 <= worm_id < GAME_WORM_COUNT:
            raise InvalidMessage("worm_id out of range")
        return {"type": "eat", "worm_id": worm_id}
    raise InvalidMessage("unknown game message type")


Validator = Callable[[Mapping[str, Any]], dict[str, Any]]

VALIDATORS: dict[str, Validator] = {
    "chat": validate_chat,
    "draw": validate_draw,
    "game": validate_game,
}
