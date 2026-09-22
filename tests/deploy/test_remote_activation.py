"""End-to-end tests of scripts/deploy/lightsail-demo-remote.sh in a sandbox.

The real script runs unchanged; only the contract paths, the helper and the
backend port are redirected (LIGHTSAIL_DEMO_SANDBOX=1) to a temporary
directory, a fake helper that starts the real ``main.py`` from ``current/``,
and an ephemeral loopback port. Each activation creates a real virtual
environment and installs the hash-pinned lock from a local wheelhouse, so
these tests are slow and need one initial download of the wheels.

Not covered here (and clearly so): SSH, sudo, systemd, the real server.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT, free_port

pytestmark = [pytest.mark.slow, pytest.mark.deploy]

REMOTE = REPO_ROOT / "scripts" / "deploy" / "lightsail-demo-remote.sh"
INSPECTOR = REPO_ROOT / "scripts" / "inspect_release.py"
FAKE_HELPER = REPO_ROOT / "tests" / "deploy" / "fake-helper.sh"

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40
SHA_E = "e" * 40


@pytest.fixture(scope="session")
def wheelhouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Download the locked wheels once so every sandbox venv installs offline."""
    cache = os.environ.get("LIGHTSAIL_TEST_WHEELHOUSE")
    target = Path(cache) if cache else tmp_path_factory.mktemp("wheelhouse")
    if not any(target.glob("aiohttp-*")):
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--quiet",
                "--disable-pip-version-check",
                "--require-hashes",
                "--no-deps",
                "-r",
                str(REPO_ROOT / "requirements.txt"),
                "-d",
                str(target),
            ],
            check=True,
        )
    return target


def make_artifact(
    dest_dir: Path, sha: str, *, mutate: Callable[[Path], None] | None = None, symlink: bool = False
) -> Path:
    """Stage the runtime files like scripts/build-release.sh and pack them for ``sha``."""
    stage = dest_dir / f"stage-{sha[:6]}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    shutil.copy(REPO_ROOT / "main.py", stage / "main.py")
    shutil.copy(REPO_ROOT / "requirements.txt", stage / "requirements.txt")
    shutil.copytree(
        REPO_ROOT / "lightsail_demo", stage / "lightsail_demo", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copytree(REPO_ROOT / "public", stage / "public")
    (stage / "REVISION").write_text(sha + "\n")
    if mutate:
        mutate(stage)

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for path in sorted(stage.rglob("*")):
            info = tar.gettarinfo(str(path), arcname=str(path.relative_to(stage)))
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 1700000000
            info.mode = 0o755 if path.is_dir() else 0o644
            if path.is_file():
                with path.open("rb") as handle:
                    tar.addfile(info, handle)
            else:
                tar.addfile(info)
        if symlink:
            link = tarfile.TarInfo("public/leak")
            link.type = tarfile.SYMTYPE
            link.linkname = "../../main.py"
            link.uid = link.gid = 0
            tar.addfile(link)
    name = f"lightsail-demo-{sha}.tar.gz"
    artifact = dest_dir / name
    artifact.write_bytes(gzip.compress(buffer.getvalue(), mtime=0))
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (dest_dir / f"{name}.sha256").write_text(f"{digest}  {name}\n")
    shutil.rmtree(stage)
    return artifact


@dataclass
class Sandbox:
    root: Path
    state: Path
    port: int
    wheelhouse: Path
    lock: Path
    env_file: Path

    @property
    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "LIGHTSAIL_DEMO_SANDBOX": "1",
                "SANDBOX_APP_ROOT": str(self.root),
                "SANDBOX_HELPER": str(FAKE_HELPER),
                "SANDBOX_LOCK_FILE": str(self.lock),
                "SANDBOX_ENV_FILE": str(self.env_file),
                "SANDBOX_BACKEND_URL": f"http://127.0.0.1:{self.port}",
                "SANDBOX_PYTHON3": sys.executable,
                "SANDBOX_STATE_DIR": str(self.state),
                "SANDBOX_PORT": str(self.port),
                "SANDBOX_HEALTH_TIMEOUT": "20",
                "SANDBOX_PYTHON_MAX_MINOR": "99",  # the runner's interpreter may be newer than 3.12
                "SANDBOX_MIN_FREE_KB": "1",
                "PIP_NO_INDEX": "1",
                "PIP_FIND_LINKS": str(self.wheelhouse),
            }
        )
        return env

    def remote(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["bash", str(REMOTE), *args], env=self.env, capture_output=True, text=True, timeout=300
        )
        if check and result.returncode != 0:
            raise AssertionError(
                f"remote {args} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}"
            )
        return result

    def upload(self, sha: str, *, mutate=None, symlink=False, tamper=False) -> str:
        name = f"deploy-{sha[:8]}-{int(time.time() * 1000)}"
        target = self.root / "incoming" / name
        target.mkdir()
        make_artifact(target, sha, mutate=mutate, symlink=symlink)
        shutil.copy(INSPECTOR, target / "inspect_release.py")
        if tamper:
            artifact = target / f"lightsail-demo-{sha}.tar.gz"
            artifact.write_bytes(artifact.read_bytes() + b"\x00")
        return name

    def current(self) -> Path | None:
        link = self.root / "current"
        return link.resolve() if link.is_symlink() else None

    def health(self) -> dict | None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=2) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            return None

    def helper_calls(self) -> list[str]:
        path = self.state / "helper.calls"
        return path.read_text().split() if path.exists() else []

    def stop(self) -> None:
        subprocess.run(["bash", str(FAKE_HELPER), "stop"], env=self.env, capture_output=True, check=False)


@pytest.fixture
def sandbox(tmp_path: Path, wheelhouse: Path) -> Sandbox:
    root = tmp_path / "srv" / "apps" / "lightsail-demo"
    (root / "incoming").mkdir(parents=True, mode=0o750)
    (root / "releases").mkdir(mode=0o755)
    root.chmod(0o755)
    (root / "incoming").chmod(0o750)
    lock = tmp_path / "cowhill-lightsail-demo.lock"
    lock.touch()
    env_file = tmp_path / "lightsail-demo.env"
    env_file.write_text(
        "HOST=127.0.0.1\nPORT=8101\nSERVE_STATIC=0\nALLOWED_ORIGINS=https://lightsail-demo.cowhill.dev\n"
    )
    box = Sandbox(
        root=root,
        state=tmp_path / "state",
        port=free_port(),
        wheelhouse=wheelhouse,
        lock=lock,
        env_file=env_file,
    )
    yield box
    box.stop()


def broken_main(stage: Path) -> None:
    (stage / "main.py").write_text("import sys\nprint('broken release', file=sys.stderr)\nsys.exit(3)\n")


def assert_readonly_for_others(path: Path) -> None:
    for entry in path.rglob("*"):
        mode = entry.lstat().st_mode & 0o777
        assert mode & 0o022 == 0, f"{entry} is group/world writable ({oct(mode)})"
        assert mode & 0o044 == 0o044, f"{entry} is not group/world readable ({oct(mode)})"


def test_preflight_passes_in_a_well_formed_sandbox(sandbox: Sandbox):
    result = sandbox.remote("preflight")
    assert "preflight ok" in result.stdout
    assert "current: none (first deployment)" in result.stdout
    assert sandbox.helper_calls() == ["status"]


def test_preflight_refuses_a_wrong_python(sandbox: Sandbox):
    env = sandbox.env
    env["SANDBOX_PYTHON_MAX_MINOR"] = "9"  # pretend only <=3.9 were supported
    result = subprocess.run(["bash", str(REMOTE), "preflight"], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert "outside the supported range" in result.stderr


def test_first_deployment_then_upgrade_then_rerun(sandbox: Sandbox):
    # --- first deployment -----------------------------------------------------
    upload = sandbox.upload(SHA_A)
    result = sandbox.remote("activate", SHA_A, upload)
    assert f"DEPLOYED {SHA_A}" in result.stdout
    release_a = sandbox.root / "releases" / SHA_A
    assert sandbox.current() == release_a
    assert (release_a / ".release-ready").is_file()
    assert (release_a / ".venv" / "bin" / "python").exists()
    assert (release_a / "REVISION").read_text().strip() == SHA_A
    assert sandbox.health() == {"status": "ok", "service": "lightsail-demo", "revision": SHA_A}
    assert_readonly_for_others(release_a / "public")
    assert not any((sandbox.root / "incoming").iterdir()), "staging upload was not cleaned"
    assert sandbox.helper_calls().count("restart") == 1

    # --- second deployment keeps the previous release for rollback ----------------
    pid_before = sandbox.health() and json.loads(json.dumps(sandbox.health()))
    upload = sandbox.upload(SHA_B)
    result = sandbox.remote("activate", SHA_B, upload)
    assert f"DEPLOYED {SHA_B} (previous {SHA_A} retained for rollback)" in result.stdout
    assert sandbox.current() == sandbox.root / "releases" / SHA_B
    assert release_a.is_dir() and (release_a / ".release-ready").is_file()
    assert sandbox.health()["revision"] == SHA_B
    assert pid_before is not None

    # --- re-running the same commit is a no-op, not a rebuild ---------------------
    upload = sandbox.upload(SHA_B)
    result = sandbox.remote("activate", SHA_B, upload)
    assert "already active and healthy; nothing to do" in result.stdout
    assert sandbox.helper_calls().count("restart") == 2


def test_failed_activation_rolls_back_to_the_previous_release(sandbox: Sandbox):
    sandbox.remote("activate", SHA_A, sandbox.upload(SHA_A))
    assert sandbox.health()["revision"] == SHA_A

    result = sandbox.remote("activate", SHA_C, sandbox.upload(SHA_C, mutate=broken_main), check=False)
    assert result.returncode == 1
    assert "DEPLOYMENT FAILED" in result.stderr
    assert f"rolled back to {SHA_A} (healthy again)" in result.stderr
    assert sandbox.current() == sandbox.root / "releases" / SHA_A
    assert sandbox.health()["revision"] == SHA_A
    failed = sandbox.root / "releases" / SHA_C
    assert failed.is_dir() and not (failed / ".release-ready").exists(), (
        "failed release must never be marked ready"
    )
    # A later deployment removes the failed leftover and does not touch A.
    sandbox.remote("activate", SHA_B, sandbox.upload(SHA_B))
    assert sandbox.health()["revision"] == SHA_B
    assert (sandbox.root / "releases" / SHA_A / ".release-ready").exists()


def test_first_deployment_failure_leaves_no_current_link(sandbox: Sandbox):
    result = sandbox.remote("activate", SHA_C, sandbox.upload(SHA_C, mutate=broken_main), check=False)
    assert result.returncode == 1
    assert "first release" in result.stderr and "DEPLOYMENT FAILED" in result.stderr
    assert not (sandbox.root / "current").exists() and not (sandbox.root / "current").is_symlink()
    assert sandbox.health() is None
    assert sandbox.helper_calls()[-1] == "stop"


def test_tampered_or_unsafe_artifacts_are_rejected_before_anything_changes(sandbox: Sandbox):
    result = sandbox.remote("activate", SHA_A, sandbox.upload(SHA_A, tamper=True), check=False)
    assert result.returncode == 1
    assert "SHA-256 verification failed" in result.stderr
    assert not (sandbox.root / "releases" / SHA_A).exists()

    result = sandbox.remote("activate", SHA_A, sandbox.upload(SHA_A, symlink=True), check=False)
    assert result.returncode == 1
    assert "inspection rejected" in result.stderr
    assert not (sandbox.root / "releases" / SHA_A).exists()
    assert sandbox.current() is None
    assert "restart" not in sandbox.helper_calls()


def test_prune_keeps_three_including_current_and_rollback_target(sandbox: Sandbox):
    for sha in (SHA_A, SHA_B, SHA_C, SHA_D):
        sandbox.remote("activate", sha, sandbox.upload(sha))
    releases = sorted(p.name for p in (sandbox.root / "releases").iterdir())
    assert releases == [SHA_B, SHA_C, SHA_D]
    sandbox.remote("activate", SHA_E, sandbox.upload(SHA_E))
    releases = sorted(p.name for p in (sandbox.root / "releases").iterdir())
    assert releases == [SHA_C, SHA_D, SHA_E]
    assert sandbox.current() == sandbox.root / "releases" / SHA_E


def test_manual_rollback_and_forward_again(sandbox: Sandbox):
    sandbox.remote("activate", SHA_A, sandbox.upload(SHA_A))
    sandbox.remote("activate", SHA_B, sandbox.upload(SHA_B))
    result = sandbox.remote("rollback")
    assert f"ROLLED BACK to {SHA_A}" in result.stdout
    assert sandbox.current() == sandbox.root / "releases" / SHA_A
    assert sandbox.health()["revision"] == SHA_A
    result = sandbox.remote("rollback", SHA_B)
    assert f"ROLLED BACK to {SHA_B}" in result.stdout
    assert sandbox.health()["revision"] == SHA_B
    result = sandbox.remote("rollback", SHA_B, check=False)
    assert result.returncode == 1 and "already the current release" in result.stderr
    status = sandbox.remote("status")
    assert f"revision {SHA_B}" in status.stdout


def test_activation_holds_the_shared_lock(sandbox: Sandbox):
    """While another holder has the lock, activation waits instead of racing."""
    import fcntl

    holder = open(sandbox.lock)  # noqa: SIM115
    fcntl.flock(holder, fcntl.LOCK_EX)
    env = sandbox.env
    proc = subprocess.Popen(
        ["bash", str(REMOTE), "activate", SHA_A, sandbox.upload(SHA_A)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    time.sleep(2)
    assert proc.poll() is None, "activation must wait for the lock"
    assert not (sandbox.root / "releases" / SHA_A).exists()
    fcntl.flock(holder, fcntl.LOCK_UN)
    holder.close()
    out, _ = proc.communicate(timeout=300)
    assert proc.returncode == 0, out
    assert f"DEPLOYED {SHA_A}" in out
