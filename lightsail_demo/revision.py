"""Deployed revision, loaded once at startup.

The deployment writes the full commit SHA into ``REVISION`` next to
``main.py``. The file is read from the *resolved* application directory (see
:func:`lightsail_demo.config.default_app_dir`), so a process started from an
older release keeps reporting its own revision even after the ``current``
symlink has been switched to a newer one. The value is cached on the
application at startup and never re-read.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import DEVELOPMENT_REVISION, ConfigError

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class RevisionError(ConfigError):
    """The REVISION file is missing or malformed."""


def is_commit_sha(value: str) -> bool:
    return bool(_SHA_RE.match(value))


def load_revision(revision_file: Path, *, allow_development: bool) -> str:
    """Return the deployed commit SHA from ``revision_file``.

    ``allow_development`` (development mode) permits a missing file, which
    yields ``"development"``. A file that exists must contain exactly one
    lower-case 40-hex commit SHA; anything else fails startup.
    """
    try:
        raw = revision_file.read_text(encoding="ascii")
    except FileNotFoundError:
        if allow_development:
            return DEVELOPMENT_REVISION
        raise RevisionError(
            f"REVISION file not found at {revision_file}; production releases must "
            "contain the deployed commit SHA (set ALLOWED_ORIGINS unset for local development)"
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise RevisionError(f"REVISION file {revision_file} could not be read: {exc}") from None
    value = raw.strip()
    if not is_commit_sha(value):
        raise RevisionError(f"REVISION file {revision_file} does not contain a full lower-case commit SHA")
    return value
