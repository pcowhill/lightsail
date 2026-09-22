#!/usr/bin/env python3
"""Guard rails for .github/workflows (run by CI and locally).

These checks are deliberately literal: they fail if someone loosens the
deployment gate, widens permissions, un-pins an action, or lets a pull request
job anywhere near the production secrets.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
DEPLOY = WORKFLOWS / "deploy.yml"
CI = WORKFLOWS / "ci.yml"

GATE = (
    "github.ref == 'refs/heads/main' "
    "&& (github.event_name == 'push' || github.event_name == 'workflow_dispatch')"
)
PINNED_USES = re.compile(r"^\s*uses:\s*([\w.-]+/[\w.-]+)@([0-9a-f]{40})\s*#\s*v\d", re.MULTILINE)
ANY_USES = re.compile(r"^\s*uses:\s*(\S+)", re.MULTILINE)


def fail(problems: list[str]) -> int:
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    return 1


def main() -> int:
    problems: list[str] = []
    deploy = DEPLOY.read_text()
    ci = CI.read_text()

    # Deployment gate present on both jobs, verbatim.
    if deploy.count(f"if: {GATE}") < 2:
        problems.append("deploy.yml: the main-only gate must appear on the build and deploy jobs verbatim")
    if "pull_request" in deploy:
        problems.append("deploy.yml: must never run on pull_request events")
    if "workflow_dispatch:" not in deploy or "push:\n    branches: [main]" not in deploy:
        problems.append("deploy.yml: triggers must be exactly push to main and workflow_dispatch")
    if "cancel-in-progress: false" not in deploy:
        problems.append("deploy.yml: an in-flight deployment must never be cancelled")
    if "/branches/main" not in deploy or "is superseded" not in deploy:
        problems.append("deploy.yml: stale-run check (compare with the head of main) is missing")
    if "persist-credentials: false" not in deploy:
        problems.append("deploy.yml: checkout must not persist credentials")
    forbidden_tokens = (
        "ssh-keyscan",
        "StrictHostKeyChecking=no",
        "StrictHostKeyChecking no",
        "accept-new",
        "sudo -n true",
        "cowhill-infra",
    )
    for forbidden in forbidden_tokens:
        if forbidden in deploy:
            problems.append(f"deploy.yml: forbidden token {forbidden!r}")
    if "secrets.LIGHTSAIL_SSH_PRIVATE_KEY" in ci or "secrets.LIGHTSAIL_KNOWN_HOSTS" in ci:
        problems.append("ci.yml: pull request jobs must not reference deployment secrets")
    if "vars.LIGHTSAIL_HOST" in ci:
        problems.append("ci.yml: pull request jobs must not reference the server address")

    # Least privilege: top-level permissions read-only, no job widens them.
    for name, text in (("deploy.yml", deploy), ("ci.yml", ci)):
        if "permissions:\n  contents: read" not in text:
            problems.append(f"{name}: top-level permissions must be exactly 'contents: read'")
        for match in re.finditer(r"^[ ]+permissions:[ ]*\n((?:[ ]+\S+:[ ]*\S+\n)+)", text, re.MULTILINE):
            problems.append(f"{name}: job-level permissions block found ({match.group(1).strip()!r})")
        # Every action pinned to a full SHA with a version comment.
        for uses in ANY_USES.findall(text):
            if not re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", uses):
                problems.append(f"{name}: action not pinned to a commit SHA: {uses}")
        if len(PINNED_USES.findall(text)) != len(ANY_USES.findall(text)):
            problems.append(f"{name}: every pinned action needs a '# vX.Y.Z' comment")
        if "self-hosted" in text:
            problems.append(f"{name}: self-hosted runners are not used for this project")

    # The remote script keeps the one allowed privileged command and nothing else.
    remote = (ROOT / "scripts" / "deploy" / "lightsail-demo-remote.sh").read_text()
    sudo_lines = [
        ln.strip()
        for ln in remote.splitlines()
        if re.search(r"\bsudo\b", ln) and not ln.strip().startswith(("#", "|| die", "die "))
    ]
    allowed = {
        "SUDO=sudo",
        '"${SUDO}" -n "${HELPER}" "$1"',
        '"${SUDO}" -n -l "${HELPER}" status >/dev/null 2>&1 \\',
        'SUDO="${SANDBOX_SUDO:-}"          # empty: run the fake helper directly',
        'if [[ -n "${SUDO}" ]]; then',
        'if [[ -z "${SUDO}" ]]; then',
    }
    for line in sudo_lines:
        if line not in allowed:
            problems.append(f"lightsail-demo-remote.sh: unexpected sudo usage: {line}")
    for forbidden in ("systemctl", "apt", "chown", "/etc/systemd", "/etc/caddy"):
        if re.search(rf"^[^#]*\b{re.escape(forbidden)}\b", remote, re.MULTILINE):
            problems.append(f"lightsail-demo-remote.sh: must not touch {forbidden}")

    if problems:
        print("workflow guard rails failed:", file=sys.stderr)
        return fail(problems)
    print("workflow guard rails ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
