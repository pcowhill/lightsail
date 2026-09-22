#!/usr/bin/env python3
"""Inspect a lightsail-demo release artifact before it is trusted.

Standard library only, so the same file runs in CI and on the server (as the
unprivileged deployment account) before the archive is extracted. It refuses
the artifact unless every member is expected:

* names are relative, normalized, and match the allowlist below
  (``main.py``, ``requirements.txt``, ``REVISION``, ``lightsail_demo/**/*.py``,
  ``public/**`` with web file types only);
* only regular files and directories: no symlinks, hard links, devices,
  FIFOs or sockets, and no absolute or ``..`` paths;
* nothing executable, nothing hidden (dotfiles), no ``__pycache__``;
* nothing from the development side: tests, ``.git``, ``.github``, scripts,
  virtual environments, caches, secrets;
* ``REVISION`` contains exactly the expected full commit SHA and the required
  entrypoints and pages are present; total size and member counts are bounded.

Usage:
    inspect_release.py ARTIFACT --sha SHA [--checksum FILE] [--quiet]

Exit status 0 means "safe to extract into an empty release directory".
"""

from __future__ import annotations

import argparse
import hashlib
import posixpath
import re
import stat
import sys
import tarfile
from pathlib import Path

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PY_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.py$")
PUBLIC_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*\.(html|css|js|png|svg|ico|txt|webmanifest|woff2)$")
DIR_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

REQUIRED_FILES = (
    "main.py",
    "requirements.txt",
    "REVISION",
    "lightsail_demo/__init__.py",
    "lightsail_demo/app.py",
    "public/index.html",
    "public/chat/index.html",
    "public/draw/index.html",
    "public/game/index.html",
    "public/shared/demo-socket.js",
    "public/shared/demo.css",
)
MAX_MEMBERS = 500
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_PUBLIC_DEPTH = 4


class Rejected(Exception):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_checksum(artifact: Path, checksum_file: Path) -> None:
    lines = [ln.split() for ln in checksum_file.read_text().splitlines() if ln.strip()]
    if len(lines) != 1 or len(lines[0]) != 2:
        raise Rejected(f"{checksum_file} must contain exactly one '<sha256>  <name>' line")
    expected, name = lines[0]
    name = name.lstrip("*")
    if name != artifact.name:
        raise Rejected(f"checksum file names {name!r}, artifact is {artifact.name!r}")
    actual = sha256_file(artifact)
    if actual != expected.lower():
        raise Rejected(f"SHA-256 mismatch: artifact {actual}, checksum file {expected}")


def classify(name: str) -> str:
    """Return 'file-<kind>' or 'dir' for an allowed normalized name; raise otherwise."""
    parts = name.split("/")
    if any(p.startswith(".") for p in parts):
        raise Rejected(f"hidden entry not allowed: {name}")
    if "__pycache__" in parts or any(p.endswith((".pyc", ".pyo")) for p in parts):
        raise Rejected(f"bytecode not allowed: {name}")
    if name in ("main.py", "requirements.txt", "REVISION"):
        return "file-root"
    if parts[0] == "lightsail_demo":
        if len(parts) == 1:
            return "dir"
        if len(parts) > 3:
            raise Rejected(f"too deep for a runtime module: {name}")
        if all(DIR_NAME_RE.match(p) for p in parts[1:]):
            # intermediate package directory (announced as a dir member)
            return "dir-or-file"
        if PY_NAME_RE.match(parts[-1]) and all(DIR_NAME_RE.match(p) for p in parts[1:-1]):
            return "file-py"
        raise Rejected(f"unexpected runtime member: {name}")
    if parts[0] == "public":
        if len(parts) == 1:
            return "dir"
        if len(parts) > MAX_PUBLIC_DEPTH:
            raise Rejected(f"too deep for public/: {name}")
        if all(DIR_NAME_RE.match(p) for p in parts[1:]):
            return "dir-or-file"
        if PUBLIC_NAME_RE.match(parts[-1]) and all(DIR_NAME_RE.match(p) for p in parts[1:-1]):
            return "file-public"
        raise Rejected(f"unexpected public member: {name}")
    raise Rejected(f"member outside the allowlist: {name}")


def normalize(raw: str) -> str:
    if raw.startswith("./"):
        raw = raw[2:]
    if raw.endswith("/"):
        raw = raw[:-1]
    if not raw or raw.startswith("/") or "\\" in raw or "\x00" in raw:
        raise Rejected(f"bad member name: {raw!r}")
    if posixpath.normpath(raw) != raw or ".." in raw.split("/") or "//" in raw:
        raise Rejected(f"member name is not normalized: {raw!r}")
    return raw


def inspect(artifact: Path, expected_sha: str, *, quiet: bool = False) -> list[str]:
    if not SHA_RE.match(expected_sha):
        raise Rejected("expected SHA must be a full lower-case commit SHA")
    if artifact.stat().st_size > MAX_TOTAL_BYTES:
        raise Rejected("artifact larger than the size bound")
    seen: dict[str, str] = {}
    total = 0
    revision_content: bytes | None = None
    requirements_content: bytes | None = None
    listing: list[str] = []
    try:
        tar = tarfile.open(artifact, mode="r:gz")  # noqa: SIM115 - closed by the with below
    except (tarfile.TarError, OSError) as exc:
        raise Rejected(f"not a readable gzip tar archive: {exc}") from None
    with tar:
        for member in tar:
            if len(seen) >= MAX_MEMBERS:
                raise Rejected("too many members")
            name = normalize(member.name)
            if name in seen:
                raise Rejected(f"duplicate member: {name}")
            if member.issym() or member.islnk():
                raise Rejected(f"link not allowed: {name}")
            if member.isdev() or member.isfifo() or member.ischr() or member.isblk():
                raise Rejected(f"special file not allowed: {name}")
            if not (member.isfile() or member.isdir()):
                raise Rejected(f"unexpected member type {member.type!r}: {name}")
            if (
                member.uid != 0
                or member.gid != 0
                or member.uname not in ("", "root")
                or member.gname not in ("", "root")
            ):
                raise Rejected(f"member must be owned by 0:0 in the archive: {name}")
            kind = classify(name)
            if member.isdir():
                if kind not in ("dir", "dir-or-file"):
                    raise Rejected(f"directory where a file was expected: {name}")
                if stat.S_IMODE(member.mode) != 0o755:
                    raise Rejected(f"directory mode must be 0755: {name} has {oct(member.mode)}")
                seen[name] = "dir"
                listing.append(f"d {name}/")
                continue
            if kind == "dir":
                raise Rejected(f"file where a directory was expected: {name}")
            if kind == "dir-or-file":
                # a file without an allowed extension inside lightsail_demo/ or public/
                raise Rejected(f"file type not allowed: {name}")
            if stat.S_IMODE(member.mode) != 0o644:
                raise Rejected(f"file mode must be 0644 (no executable bits): {name} has {oct(member.mode)}")
            if member.size > MAX_MEMBER_BYTES:
                raise Rejected(f"member too large: {name}")
            total += member.size
            if total > MAX_TOTAL_BYTES:
                raise Rejected("archive contents exceed the size bound")
            seen[name] = "file"
            listing.append(f"f {name} ({member.size} bytes)")
            if name == "REVISION":
                handle = tar.extractfile(member)
                revision_content = handle.read() if handle else b""
            elif name == "requirements.txt":
                handle = tar.extractfile(member)
                requirements_content = handle.read() if handle else b""
    for required in REQUIRED_FILES:
        if seen.get(required) != "file":
            raise Rejected(f"required file missing: {required}")
    if revision_content is None or revision_content.decode("ascii", "replace").strip() != expected_sha:
        raise Rejected("REVISION does not contain the expected commit SHA")
    if revision_content.count(b"\n") > 1:
        raise Rejected("REVISION must be a single line")
    if requirements_content is None or b"--hash=sha256:" not in requirements_content:
        raise Rejected("requirements.txt must be a hash-pinned lock")
    if b"aiohttp==" not in requirements_content:
        raise Rejected("requirements.txt does not pin aiohttp")
    if not quiet:
        for line in listing:
            print(line)
        print(f"ok: {len(seen)} members, {total} bytes, revision {expected_sha}")
    return listing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--sha", required=True, help="full commit SHA the artifact must carry in REVISION")
    parser.add_argument("--checksum", type=Path, help="'<sha256>  <artifact name>' file to verify first")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.checksum is not None:
            verify_checksum(args.artifact, args.checksum)
            if not args.quiet:
                print(f"checksum ok: {args.checksum.name}")
        inspect(args.artifact, args.sha.strip().lower(), quiet=args.quiet)
    except Rejected as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
