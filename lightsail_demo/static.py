"""Optional static file serving for local development (``SERVE_STATIC=1``).

In production Caddy serves ``public/`` directly and this handler is not even
registered. Locally it mirrors Caddy's behaviour so the same URLs work:

* files come from the installed application's ``public/`` directory only,
  never from the working directory or the repository root;
* ``/`` and ``/<dir>/`` serve that directory's ``index.html``; a directory
  requested without a trailing slash is redirected to it (as Caddy does);
* no directory listings, no dotfiles, no symbolic links, no path traversal:
  every rejected request is a plain 404.
"""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

INDEX_FILE = "index.html"

# Static responses carry a few defensive headers; the demo pages need no
# framing, and sniffing is never wanted.
STATIC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}


def resolve_public_path(public_dir: Path, url_path: str) -> Path | None:  # noqa: PLR0911
    """Map a request path onto a regular file under ``public_dir``.

    Returns the file to serve, ``public_dir / <dir>`` (a directory that has an
    index file, signalling a redirect is needed when the URL lacks a trailing
    slash) or ``None`` for anything that must be a 404. The path is walked
    component by component so a symlink anywhere along the way is refused.
    """
    if not url_path.startswith("/") or "\x00" in url_path:
        return None
    parts = [p for p in url_path.split("/") if p]
    if any(p in (".", "..") or p.startswith(".") for p in parts):
        return None
    if "\\" in url_path:
        return None
    current = public_dir
    for part in parts:
        current = current / part
        if current.is_symlink() or not current.exists():
            return None
    if current.is_dir():
        index = current / INDEX_FILE
        if index.is_symlink() or not index.is_file():
            return None
        return index if url_path.endswith("/") else current
    if current.is_file():
        return current
    return None


class StaticFiles:
    """Serve regular files below ``public_dir`` (development only)."""

    def __init__(self, public_dir: Path) -> None:
        self.public_dir = public_dir.resolve()
        if not self.public_dir.is_dir():
            raise FileNotFoundError(f"public directory not found: {self.public_dir}")

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if request.method not in ("GET", "HEAD"):
            raise web.HTTPMethodNotAllowed(request.method, ["GET", "HEAD"])
        target = resolve_public_path(self.public_dir, request.path)
        if target is None:
            raise web.HTTPNotFound()
        if target.is_dir():
            # Directory with an index, requested without a trailing slash.
            location = request.path + "/"
            if request.query_string:
                location += "?" + request.query_string
            raise web.HTTPMovedPermanently(location=location)
        # Final defence: the resolved file must still live under public_dir.
        real = target.resolve()
        if not real.is_relative_to(self.public_dir) or not real.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(real, headers=STATIC_HEADERS)
