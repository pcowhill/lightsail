"""Entrypoint for the lightsail-demo backend.

Production (systemd, deployment contract version 1) runs exactly::

    /srv/apps/lightsail-demo/current/.venv/bin/python /srv/apps/lightsail-demo/current/main.py

with HOST, PORT, SERVE_STATIC and ALLOWED_ORIGINS from the root-owned
environment file. Locally, ``python main.py`` serves the frontend from
``public/`` and the WebSocket backends on http://127.0.0.1:8080.

Invalid configuration or an occupied port is a startup failure (exit status
1 with a one-line reason). The process never falls back to another interface
or port.
"""

from __future__ import annotations

import logging
import sys

from aiohttp import web

from lightsail_demo.app import create_app
from lightsail_demo.config import ConfigError, load_settings

log = logging.getLogger("lightsail_demo.main")


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"lightsail-demo: configuration error: {exc}", file=sys.stderr)
        return 1

    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    try:
        app = create_app(settings)
    except (ConfigError, FileNotFoundError) as exc:
        log.error("startup failed: %s", exc)
        return 1

    try:
        web.run_app(
            app,
            host=settings.host,
            port=settings.port,
            print=None,
            # The access log is off by default: Caddy already logs requests and
            # the health poller would otherwise fill the journal. LOG_LEVEL=DEBUG
            # turns it on.
            access_log=logging.getLogger("aiohttp.access") if settings.log_level == "DEBUG" else None,
            shutdown_timeout=10.0,
            # SO_REUSEADDR (the POSIX default) lets a restart bind while the
            # previous process's connections are still in TIME_WAIT. It does
            # not allow two listeners on the port; SO_REUSEPORT would, and is
            # explicitly disabled so a second process fails instead of sharing.
            reuse_port=False,
        )
    except OSError as exc:
        log.error("could not bind %s:%d: %s", settings.host, settings.port, exc.strerror or exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
