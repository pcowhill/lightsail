"""aiohttp application factory for the lightsail-demo applet suite."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from .config import SERVICE_NAME, Settings, load_settings
from .revision import load_revision
from .static import StaticFiles
from .ws import APPLETS, ROOM_CLASSES, SHUTDOWN_CLOSE_TIMEOUT, Room, TaskGroupish

SETTINGS_KEY: web.AppKey[Settings] = web.AppKey("settings", Settings)
REVISION_KEY: web.AppKey[str] = web.AppKey("revision", str)
ROOMS_KEY: web.AppKey[dict[str, Room]] = web.AppKey("rooms", dict)
TASKS_KEY: web.AppKey[TaskGroupish] = web.AppKey("tasks", TaskGroupish)

log = logging.getLogger("lightsail_demo.app")


async def healthz(request: web.Request) -> web.Response:
    """Liveness and deployed-revision report (no environment, no paths)."""
    payload: dict[str, Any] = {
        "status": "ok",
        "service": SERVICE_NAME,
        "revision": request.app[REVISION_KEY],
    }
    return web.json_response(payload, headers={"Cache-Control": "no-store"})


async def _on_startup(app: web.Application) -> None:
    settings = app[SETTINGS_KEY]
    log.info(
        "%s revision %s listening on %s:%d (static: %s, origins: %s)",
        SERVICE_NAME,
        app[REVISION_KEY],
        settings.host,
        settings.port,
        "public/ (development)" if settings.serve_static else "off (Caddy serves public/)",
        "loopback development policy"
        if settings.development_mode
        else ", ".join(settings.allowed_origins or ()),
    )


async def _on_shutdown(app: web.Application) -> None:
    log.info("shutting down: closing WebSocket connections")
    for room in app[ROOMS_KEY].values():
        await room.close_all()
    await app[TASKS_KEY].drain(SHUTDOWN_CLOSE_TIMEOUT)
    log.info("shutdown complete")


def create_app(settings: Settings | None = None, *, revision: str | None = None) -> web.Application:
    """Build the application.

    ``settings`` defaults to :func:`load_settings` from the environment. The
    revision is read once here, from the resolved application directory, and
    stored on the app; ``revision`` overrides the file (tests only).
    """
    if settings is None:
        settings = load_settings()
    if revision is None:
        revision = load_revision(settings.revision_file, allow_development=settings.development_mode)

    app = web.Application(client_max_size=settings.ws_max_message_bytes)
    app[SETTINGS_KEY] = settings
    app[REVISION_KEY] = revision
    tasks = TaskGroupish()
    app[TASKS_KEY] = tasks
    rooms: dict[str, Room] = {name: ROOM_CLASSES[name](settings, tasks) for name in APPLETS}
    app[ROOMS_KEY] = rooms

    app.router.add_get("/healthz", healthz)
    for name, room in rooms.items():
        app.router.add_get(f"/ws/{name}", room.handle)

    if settings.serve_static:
        static = StaticFiles(settings.public_dir)
        app.router.add_route("*", "/{tail:.*}", static.handle)

    app.on_startup.append(_on_startup)
    app.on_shutdown.append(_on_shutdown)
    return app
