"""GET /healthz and revision loading."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lightsail_demo.app import REVISION_KEY, create_app
from lightsail_demo.config import DEVELOPMENT_REVISION
from lightsail_demo.revision import RevisionError, load_revision
from tests.conftest import FAKE_SHA, ServerProcess, base_env, free_port, http_get, make_settings


async def test_healthz_reports_status_service_and_revision(client):
    response = await client.get("/healthz")
    assert response.status == 200
    assert response.headers["Content-Type"].startswith("application/json")
    assert response.headers["Cache-Control"] == "no-store"
    body = await response.json()
    assert body == {"status": "ok", "service": "lightsail-demo", "revision": FAKE_SHA}


async def test_healthz_has_no_environment_or_path_details(client):
    body = await (await client.get("/healthz")).text()
    for secret_looking in ("HOST", "PORT", "/srv/", "ALLOWED_ORIGINS", "python", "/home/"):
        assert secret_looking not in body


async def test_healthz_only_exact_path(client):
    assert (await client.get("/healthz/")).status == 404
    assert (await client.get("/healthz/anything")).status == 404
    assert (await client.post("/healthz")).status == 405


def test_load_revision_reads_sha(tmp_path: Path):
    f = tmp_path / "REVISION"
    f.write_text(FAKE_SHA + "\n")
    assert load_revision(f, allow_development=False) == FAKE_SHA


@pytest.mark.parametrize(
    "content", ["", "main", FAKE_SHA[:7], FAKE_SHA.upper(), FAKE_SHA + "x", f"{FAKE_SHA}\n{FAKE_SHA}"]
)
def test_load_revision_rejects_malformed(tmp_path: Path, content: str):
    f = tmp_path / "REVISION"
    f.write_text(content)
    with pytest.raises(RevisionError):
        load_revision(f, allow_development=True)


def test_missing_revision_is_development_only_in_development_mode(tmp_path: Path):
    f = tmp_path / "REVISION"
    assert load_revision(f, allow_development=True) == DEVELOPMENT_REVISION
    with pytest.raises(RevisionError, match="REVISION file not found"):
        load_revision(f, allow_development=False)


def test_create_app_fails_without_revision_in_production(tmp_path: Path):
    (tmp_path / "public").mkdir()
    with pytest.raises(RevisionError):
        create_app(make_settings(app_dir=tmp_path))


def test_create_app_uses_development_revision_locally(tmp_path: Path):
    (tmp_path / "public").mkdir()
    settings = make_settings(app_dir=tmp_path, allowed_origins=None)
    assert create_app(settings)[REVISION_KEY] == DEVELOPMENT_REVISION


@pytest.mark.slow
def test_running_process_keeps_its_own_revision_after_current_is_switched(release_dir: Path):
    """An old process must not report the revision of a newly switched symlink.

    Layout: releases/<A> (running), releases/<B> (new), current -> A. After the
    process is up, current is repointed to B. /healthz must still say A.
    """
    releases = release_dir.parent
    sha_a = FAKE_SHA
    sha_b = "f" * 40
    release_b = releases / sha_b
    os.symlink(release_dir, releases / "current_tmp")
    os.rename(releases / "current_tmp", releases / "current")
    current = releases / "current"

    env = base_env()
    port = free_port()
    env.update(
        {
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "SERVE_STATIC": "0",
            "ALLOWED_ORIGINS": f"http://127.0.0.1:{port}",
        }
    )
    server = ServerProcess(current, env, cwd=current)  # started through the symlink, like systemd does
    try:
        server.wait_ready()
        status, body, _ = http_get(f"{server.base_url}/healthz")
        assert status == 200 and f'"revision":"{sha_a}"' in body.decode().replace(" ", "")

        # Publish a new release and switch current atomically.
        release_b.mkdir()
        (release_b / "REVISION").write_text(sha_b + "\n")
        os.symlink(release_b, releases / "current_tmp")
        os.rename(releases / "current_tmp", current)
        assert os.readlink(current) == str(release_b)

        status, body, _ = http_get(f"{server.base_url}/healthz")
        assert status == 200
        assert sha_a in body.decode()
        assert sha_b not in body.decode()
    finally:
        server.stop()
