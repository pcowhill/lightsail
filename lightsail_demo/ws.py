"""WebSocket rooms for the chat, draw and game applets.

One :class:`Room` per applet keeps the connected clients (app-scoped, not
module-global) and relays validated messages between them. The design goals
are the ones that matter for a small public demo behind Caddy:

* **Origin guard.** The upgrade is refused (HTTP 403) unless the browser's
  ``Origin`` is on the configured allowlist. This blocks cross-site pages from
  driving the demo through a visitor's browser; it is *not* authentication
  (a non-browser client can send any Origin).
* **Bounded resources.** A per-applet connection cap (HTTP 503 when full), a
  per-connection message-size limit, a per-connection token-bucket rate limit
  and a bounded outbound queue per connection. A receiver that cannot keep up
  is dropped instead of slowing every other client down.
* **Validation by construction.** Every inbound frame is parsed and rebuilt by
  :mod:`lightsail_demo.messages`; only known fields with checked bounds are
  relayed. Malformed input is ignored (and, if it keeps coming, closed with
  1008) without affecting other sessions.
* **Graceful cleanup.** A dropped or closed client is removed from its room,
  its sender task is cancelled, and on application shutdown every client is
  closed with 1001 (going away).

Nothing here logs message contents: only counts, applet names, connection ids
and close reasons.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import secrets
import time
from collections.abc import Callable
from typing import Any

from aiohttp import WSCloseCode, WSMsgType, web

from .config import Settings
from .messages import (
    GAME_WORLD_LIMIT,
    GAME_WORM_COUNT,
    VALIDATORS,
    InvalidMessage,
    parse_message,
)

# After this many invalid messages on one connection, the connection is closed.
INVALID_MESSAGE_LIMIT = 20
# Time allowed for close handshakes during shutdown.
SHUTDOWN_CLOSE_TIMEOUT = 5.0

APPLETS = ("chat", "draw", "game")


class TokenBucket:
    """Per-connection rate limiter: ``rate`` tokens per second, ``burst`` capacity."""

    def __init__(self, rate: float, burst: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._rate = float(rate)
        self._burst = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._last = clock()

    def allow(self) -> bool:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


class Connection:
    """One accepted WebSocket with a bounded outbound queue and a sender task."""

    def __init__(self, conn_id: int, ws: web.WebSocketResponse, room: Room) -> None:
        self.id = conn_id
        self.ws = ws
        self.room = room
        self.limiter = TokenBucket(room.settings.ws_rate_limit_per_second, room.settings.ws_rate_limit_burst)
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=room.settings.ws_send_queue_limit)
        self.invalid_messages = 0
        self.sent = 0
        self.dropped = False
        self.finishing = False
        self._close_task: asyncio.Task[Any] | None = None
        self.sender = asyncio.create_task(self._send_loop(), name=f"ws-send-{room.name}-{conn_id}")

    def enqueue(self, text: str) -> bool:
        """Queue ``text`` for sending. Returns False (and drops the client) when full."""
        if self.dropped or self.ws.closed:
            return False
        try:
            self.queue.put_nowait(text)
        except asyncio.QueueFull:
            self.drop(WSCloseCode.POLICY_VIOLATION, "receiver too slow")
            return False
        return True

    def drop(self, code: int, reason: str) -> None:
        """Schedule a close with ``code``; the receive loop then ends."""
        if self.dropped:
            return
        self.dropped = True
        # Stop the sender first so no further frames interleave with the close.
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(None)
        # ws.close() may be called from a task other than the receive loop:
        # aiohttp then ends that loop with a CLOSING message and completes the
        # close handshake here (bounded by the close timeout).
        self._close_task = self.room.app_tasks.spawn(
            self._close_now(code, reason), name=f"ws-close-{self.room.name}-{self.id}"
        )

    async def _close_now(self, code: int, reason: str) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                self.ws.close(code=code, message=reason.encode()), timeout=SHUTDOWN_CLOSE_TIMEOUT
            )

    async def _send_loop(self) -> None:
        try:
            while True:
                text = await self.queue.get()
                if text is None or self.ws.closed:
                    return
                try:
                    await self.ws.send_str(text)
                except (ConnectionError, RuntimeError, asyncio.CancelledError):
                    return
                except Exception:
                    self.room.log.debug("send to %s#%d failed", self.room.name, self.id, exc_info=True)
                    return
                self.sent += 1
        finally:
            # A sender that died on its own (send failure) takes the socket
            # with it; a normal finish() does not.
            if not self.finishing and not self.ws.closed and not self.dropped:
                self.drop(WSCloseCode.INTERNAL_ERROR, "send failed")

    async def finish(self) -> None:
        """Stop the sender task and finish any pending close (receive loop has ended)."""
        self.finishing = True
        if not self.sender.done():
            self.sender.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self.sender
        if self._close_task is not None and not self._close_task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(self._close_task), timeout=SHUTDOWN_CLOSE_TIMEOUT)


class TaskGroupish:
    """Tracks fire-and-forget tasks so shutdown can wait for them."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, coro: Any, *, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self, timeout: float) -> None:
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)
        for task in pending:
            if not task.done():
                task.cancel()


class Room:
    """Connected clients of one applet plus the applet's shared state."""

    name = "room"

    def __init__(self, settings: Settings, app_tasks: TaskGroupish) -> None:
        self.settings = settings
        self.app_tasks = app_tasks
        self.connections: dict[int, Connection] = {}
        self.log = logging.getLogger(f"lightsail_demo.{self.name}")
        self.validate = VALIDATORS[self.name]
        self.rejected_origin = 0
        self.rejected_full = 0

    # -- hooks for subclasses ------------------------------------------------

    async def on_connect(self, conn: Connection) -> None:
        """Called after the client is registered."""

    async def on_message(self, conn: Connection, message: dict[str, Any]) -> None:
        """Called with a validated message; default relays it to everyone else."""
        self.broadcast(message, exclude=conn)

    async def on_disconnect(self, conn: Connection) -> None:
        """Called after the client has been removed from the room."""

    # -- relay ---------------------------------------------------------------

    def broadcast(self, message: dict[str, Any], *, exclude: Connection | None = None) -> int:
        """Queue ``message`` once for every connected client except ``exclude``."""
        text = json.dumps(message, separators=(",", ":"))
        delivered = 0
        for conn in list(self.connections.values()):
            if conn is exclude:
                continue
            if conn.enqueue(text):
                delivered += 1
        return delivered

    def send(self, conn: Connection, message: dict[str, Any]) -> bool:
        return conn.enqueue(json.dumps(message, separators=(",", ":")))

    # -- lifecycle -----------------------------------------------------------

    def _new_id(self) -> int:
        while True:
            conn_id = secrets.randbits(32)
            if conn_id and conn_id not in self.connections:
                return conn_id

    async def handle(self, request: web.Request) -> web.StreamResponse:
        settings = self.settings
        origin = request.headers.get("Origin")
        if not settings.origin_allowed(origin):
            self.rejected_origin += 1
            self.log.info("%s: refused upgrade, origin not allowed", self.name)
            raise web.HTTPForbidden(text="origin not allowed")
        if len(self.connections) >= settings.ws_max_connections:
            self.rejected_full += 1
            self.log.warning("%s: refused upgrade, %d connections (limit)", self.name, len(self.connections))
            raise web.HTTPServiceUnavailable(text="too many connections", headers={"Retry-After": "10"})

        ws = web.WebSocketResponse(
            heartbeat=settings.ws_heartbeat_seconds,
            max_msg_size=settings.ws_max_message_bytes,
            autoping=True,
            autoclose=True,
        )
        if not ws.can_prepare(request).ok:
            raise web.HTTPBadRequest(text="websocket upgrade required")
        await ws.prepare(request)

        conn = Connection(self._new_id(), ws, self)
        self.connections[conn.id] = conn
        self.log.info("%s: client %d connected (%d online)", self.name, conn.id, len(self.connections))
        try:
            await self.on_connect(conn)
            await self._receive_loop(conn)
        finally:
            self.connections.pop(conn.id, None)
            await conn.finish()
            self.log.info("%s: client %d disconnected (%d online)", self.name, conn.id, len(self.connections))
            with contextlib.suppress(Exception):
                await self.on_disconnect(conn)
        return ws

    async def _receive_loop(self, conn: Connection) -> None:
        ws = conn.ws
        async for msg in ws:
            if conn.dropped:
                break
            if msg.type == WSMsgType.TEXT:
                if not conn.limiter.allow():
                    self.log.info("%s: client %d exceeded the message rate limit", self.name, conn.id)
                    conn.drop(WSCloseCode.POLICY_VIOLATION, "message rate limit exceeded")
                    break
                try:
                    message = self.validate(parse_message(msg.data))
                except InvalidMessage as exc:
                    conn.invalid_messages += 1
                    self.log.debug("%s: client %d sent an invalid message: %s", self.name, conn.id, exc)
                    if conn.invalid_messages >= INVALID_MESSAGE_LIMIT:
                        conn.drop(WSCloseCode.POLICY_VIOLATION, "too many invalid messages")
                        break
                    continue
                await self.on_message(conn, message)
                # Frames that arrived in one read are otherwise processed
                # without yielding; give the sender tasks a turn so a burst
                # from one client cannot overflow the others' queues.
                await asyncio.sleep(0)
            elif msg.type == WSMsgType.BINARY:
                conn.drop(WSCloseCode.UNSUPPORTED_DATA, "text frames only")
                break
            elif msg.type == WSMsgType.ERROR:
                self.log.info("%s: client %d connection error: %s", self.name, conn.id, ws.exception())
                break
            # CLOSE/CLOSING/CLOSED end the iteration by themselves.

    async def close_all(
        self, code: int = WSCloseCode.GOING_AWAY, reason: str = "server shutting down"
    ) -> None:
        conns = list(self.connections.values())
        if not conns:
            return
        self.log.info("%s: closing %d connection(s): %s", self.name, len(conns), reason)
        payload = reason.encode()

        async def _close(conn: Connection) -> None:
            conn.dropped = True
            with contextlib.suppress(Exception):
                await conn.ws.close(code=code, message=payload)

        await asyncio.wait(
            [asyncio.create_task(_close(c)) for c in conns],
            timeout=SHUTDOWN_CLOSE_TIMEOUT,
        )


class ChatRoom(Room):
    name = "chat"


class DrawRoom(Room):
    name = "draw"


class GameRoom(Room):
    """Relays player movement, shares worm positions and announces disconnects."""

    name = "game"

    def __init__(self, settings: Settings, app_tasks: TaskGroupish, rng: random.Random | None = None) -> None:
        super().__init__(settings, app_tasks)
        self.rng = rng or random.Random()
        self.worms: list[list[int]] = [self._spawn() for _ in range(GAME_WORM_COUNT)]

    def _spawn(self) -> list[int]:
        return [self.rng.randint(-GAME_WORLD_LIMIT, GAME_WORLD_LIMIT) for _ in range(2)]

    def worms_message(self) -> dict[str, Any]:
        return {"type": "worms", "positions": [list(w) for w in self.worms]}

    async def on_connect(self, conn: Connection) -> None:
        self.send(conn, self.worms_message())

    async def on_message(self, conn: Connection, message: dict[str, Any]) -> None:
        kind = message["type"]
        if kind in ("movement", "connect"):
            message["id"] = conn.id
            self.broadcast(message, exclude=conn)
        elif kind == "eat":
            # Respawn the eaten worm and tell *every* client (including the
            # eater) the new shared positions, each exactly once.
            self.worms[message["worm_id"]] = self._spawn()
            self.broadcast(self.worms_message())

    async def on_disconnect(self, conn: Connection) -> None:
        self.broadcast({"type": "disconnect", "id": conn.id})


ROOM_CLASSES: dict[str, type[Room]] = {
    "chat": ChatRoom,
    "draw": DrawRoom,
    "game": GameRoom,
}
