# CLAUDE.md: working notes for this repository

`pcowhill/lightsail` is the **lightsail-demo** applet suite (chat, draw, game)
served at https://lightsail-demo.cowhill.dev. Read `README.md` for the layout
and `docs/deployment.md` for the production contract before changing anything
that touches configuration, paths, ports or deployment.

## Ground rules

* **Deployment contract v1 is fixed.** Paths under `/srv/apps/lightsail-demo`,
  the backend `127.0.0.1:8101`, the environment variable names `HOST`, `PORT`,
  `SERVE_STATIC`, `ALLOWED_ORIGINS`, the account names, the helper
  `/usr/local/sbin/cowhill-lightsail-demo-service` and the lock are owned
  jointly with `pcowhill/cowhill-infrastructure`. Never change one side alone,
  never add a `sudo` beyond the helper's `restart|stop|status`, never touch
  Caddy, systemd units, DNS, the firewall or other applications from here.
* **Only `public/` is web-visible.** Never put credentials, source, `.git`,
  virtual environments, release tooling or symlinks under `public/`;
  `scripts/inspect_release.py` and the server-side activation reject them.
* **Never serve the repository root or bind a wildcard interface.** Static
  files are a development convenience (`SERVE_STATIC=1`) resolved relative to
  the installed application, not the working directory.
* **Validate every inbound WebSocket message by construction** in
  `lightsail_demo/messages.py`: rebuild a dict of known fields with checked
  types and bounds; never relay the raw client object. Sprites and colours are
  names from allowlists, never URLs.
* **Frontend rendering is text-node only.** No `innerHTML`, no inline event
  handlers, no hand-built WebSocket URLs: use `LightsailDemo.DemoSocket` from
  `public/shared/demo-socket.js` (scheme follows the page, host from
  `location.host`). Reconnect is manual; there is no reconnect loop and no
  offline queue. Keep UI changes restrained.
* **Do not log message contents or secrets.** Counts, ids, close reasons and
  configuration summaries only.
* **One process, in-memory state, public shared demo.** No persistence, no
  multiple workers, no accounts or rooms in this milestone.
* **Workflows:** every action pinned to a commit SHA with a version comment;
  `permissions: contents: read`; pull requests never see deployment secrets;
  the deploy gate `github.ref == 'refs/heads/main' && (push || workflow_dispatch)`
  stays verbatim on both deploy jobs (`scripts/check-workflows.py` enforces
  these). No `ssh-keyscan`, no `StrictHostKeyChecking=no/accept-new`.
* **Do not require the GitHub CLI** for anything in this repository.
* The catalog (`projects.yaml` in the Cowhill site) is maintained separately;
  do not edit it from here.

## Everyday commands

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .\.venv\Scripts\Activate.ps1
python -m pip install --require-hashes -r requirements-dev.txt
python main.py                                           # http://127.0.0.1:8080 (dev mode)
ruff check . && ruff format --check .
python -m pytest -m "not browser and not caddy"          # fast suite (what deploy.yml runs)
python -m pytest -m browser                              # needs: python -m playwright install chromium
CADDY_BIN=/path/to/caddy python -m pytest -m caddy
node scripts/check-frontend.js && python scripts/check-workflows.py
scripts/lock-requirements.sh                             # after editing requirements/*.in
scripts/build-release.sh && python scripts/inspect_release.py dist/*.tar.gz --sha "$(git rev-parse HEAD)"
```

## Process

`edit -> pull request -> CI green -> merge to main -> Deploy workflow`. The
Deploy workflow re-runs lint and tests, builds and inspects the artifact for
the exact commit, uploads it as `deploy-lightsail-demo`, builds the venv on
the VM, switches `current`, restarts through the helper and requires
`/healthz` to report the new SHA, rolling back otherwise. Rollback and the
runbook are in `docs/deployment.md`. Report a real contract mismatch instead
of working around it on the server.
