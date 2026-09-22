# lightsail-demo (`pcowhill/lightsail`)

Three small WebSocket applets on one Python/aiohttp backend, served in
production at **https://lightsail-demo.cowhill.dev**:

| Page | Backend | What it does |
|---|---|---|
| `/chat/` (also `/chat/index.html`) | `/ws/chat` | Public shared chat room |
| `/draw/` (also `/draw/index.html`) | `/ws/draw` | Shared drawing canvas with clear |
| `/game/` (also `/game/index.html`) | `/ws/game` | Fly a bird around a shared world, eat worms, see other players |

**This is a public shared demo.** Everyone connected at the same time shares
one chat, one canvas and one game world; nothing is private, nothing is
stored, and a deployment or restart disconnects everyone and resets the
server-side state. The pages say so, show their connection state, and offer a
manual **Reconnect** button.

## Repository layout

```
main.py                 entrypoint (production: .venv/bin/python main.py)
lightsail_demo/         application package
  config.py             environment -> validated Settings (contract variables)
  app.py                aiohttp application factory, /healthz, shutdown
  ws.py                 rooms: origin guard, limits, validation, relay, cleanup
  messages.py           wire formats and validation for chat / draw / game
  static.py             development-only static serving of public/
  revision.py           REVISION file -> reported revision
public/                 the frontend, served by Caddy in production
  index.html, chat/, draw/, game/, shared/ (demo-socket.js, demo.css)
requirements.txt        hash-pinned RUNTIME lock (shipped in every release)
requirements-dev.txt    hash-pinned development/CI lock (never on the server)
requirements/*.in       the top-level inputs for the two locks
scripts/                release build/inspection, lock regeneration, checks
scripts/deploy/         deployment driver, server-side activation, smoke check
tests/                  pytest suite (unit, integration, browser, Caddy, deploy sandbox)
docs/deployment.md      the deployment contract, runbook and rollback
.github/workflows/      ci.yml (PRs and main), deploy.yml (main only)
```

## Local development

Requirements: Python 3.10, 3.11 or 3.12 (the server runs Ubuntu 24.04 with
3.12; 3.13 is tested but not required), `git`, and for the optional browser
and Caddy tests Chromium (via Playwright) and a `caddy` binary.

### Linux / macOS

```bash
git clone https://github.com/pcowhill/lightsail.git
cd lightsail
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.txt
python main.py
```

### Windows (PowerShell)

```powershell
git clone https://github.com/pcowhill/lightsail.git
cd lightsail
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --require-hashes -r requirements-dev.txt
python main.py
```

If activation is blocked, allow scripts for the current session first:
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned`.

`python main.py` with no environment set is **development mode**:

* listens on `http://127.0.0.1:8080` (loopback only);
* serves the frontend from `public/` itself (`SERVE_STATIC=1`), so the whole
  demo works from one process: open http://127.0.0.1:8080/ in two browser
  windows to try the interaction;
* accepts WebSocket upgrades from loopback `http://` origins only;
* reports `"revision": "development"` on `/healthz` because the checkout has
  no `REVISION` file.

Stop it with Ctrl+C. It never binds a wildcard interface and never picks a
different port: if 8080 is busy, it exits with status 1 and says so.

### Configuration

| Variable | Default | Production (fixed by the contract) | Meaning |
|---|---|---|---|
| `HOST` | `127.0.0.1` | `127.0.0.1` | IP address to bind. Wildcards (`0.0.0.0`, `::`) and host names are refused. |
| `PORT` | `8080` | `8101` | TCP port. An occupied port is a startup failure. |
| `SERVE_STATIC` | `1` | `0` | `1`: serve `public/` (development). `0`: backend endpoints only; Caddy serves the files. |
| `ALLOWED_ORIGINS` | unset (development mode) | `https://lightsail-demo.cowhill.dev` | Exact, comma/space separated list of `scheme://host[:port]` origins allowed to open WebSockets. Unset = loopback development policy and the `development` revision. |
| `LOG_LEVEL` | `INFO` | | `DEBUG` also enables the aiohttp access log. |
| `WS_MAX_MESSAGE_BYTES` | `4096` | | Largest WebSocket text frame accepted; bigger frames close the connection (1009). |
| `WS_MAX_CONNECTIONS` | `64` | | Simultaneous connections per applet; more are refused with HTTP 503. |
| `WS_RATE_LIMIT_PER_SECOND` / `WS_RATE_LIMIT_BURST` | `120` / `240` | | Per-connection token bucket; a flooding client is closed (1008). |
| `WS_SEND_QUEUE_LIMIT` | `256` | | Outbound messages buffered per connection; a receiver that cannot keep up is dropped (1008) instead of slowing everyone else. |
| `WS_HEARTBEAT_SECONDS` | `30` | | Server ping interval; unanswered pings close the connection. `0` disables. |

Limits are per connection and per applet. The backend deliberately does not
apply per-IP limits: behind Caddy every connection comes from 127.0.0.1, and
forwarded headers are not trusted for anything.

### Tests

```bash
ruff check . && ruff format --check .
python -m pytest                       # everything that is available locally
python -m pytest -m "not browser and not caddy"   # what deploy.yml runs before deploying
python -m pytest -m browser            # needs Chromium: python -m playwright install chromium
python -m pytest -m caddy              # needs a caddy binary on PATH (or CADDY_BIN=/path/to/caddy)
python -m pytest -m deploy             # sandboxed runs of the server-side activation script
node scripts/check-frontend.js         # frontend static checks
python scripts/check-workflows.py      # workflow guard rails
```

What the suite covers, and how honestly:

* **Real behaviour:** multi-client chat relay, drawing and clear, game
  connect/movement/eat/disconnect, malformed messages, bounds, size/rate/
  connection/slow-receiver limits, Origin handling, graceful shutdown (1001),
  occupied-port and invalid-configuration failures, `/healthz` and revision
  (including that a running process keeps its own revision after `current`
  is switched), public file access, forbidden files and traversal, dev
  static mode and production static-off mode.
* **Browser tests** (`-m browser`): two independent Chromium contexts per
  applet; HTML-like chat text is proven inert in both; manual Reconnect after
  a backend restart restores game registration without duplicates.
* **Caddy tests** (`-m caddy`): an isolated loopback Caddy with a copy of the
  production site block (ACME disabled) serving a release-shaped `public/`
  and proxying `/ws/*` and `/healthz` unstripped to the real backend.
* **Deployment sandbox** (`-m deploy`): the unchanged server-side activation
  script against temporary directories, a fake systemd helper that runs the
  real `main.py`, and real virtual environments installed from the lock.
  **Mocked/not covered:** SSH, sudo, systemd, the real VM.

Every test binds ephemeral loopback ports; nothing tests against production.

### Dependencies

Runtime: `aiohttp` only (plus its own dependencies). `websockets` from the
original `requirements.txt` was unused and has been dropped. The runtime lock
`requirements.txt` is generated with `uv` for Python 3.10 to 3.12 with every
distribution hash (`--require-hashes`), so the VM installs exactly what CI
tested. To change dependencies edit `requirements/runtime.in` (or `dev.in`)
and run `scripts/lock-requirements.sh`; CI fails if the locks are stale and
runs `pip-audit` plus GitHub dependency review on every pull request.

## Production

Production is the Lightsail VM provisioned by `pcowhill/cowhill-infrastructure`
(deployment contract version 1). In short:

* Caddy terminates TLS for `lightsail-demo.cowhill.dev`, serves
  `/srv/apps/lightsail-demo/current/public` directly (no directory listing,
  dotfiles 404) and proxies only `/ws/*` and `/healthz`, paths unchanged, to
  the backend on `127.0.0.1:8101`.
* `lightsail-demo.service` runs **one** process,
  `current/.venv/bin/python current/main.py`, as the unprivileged runtime user
  with the contract environment (`HOST=127.0.0.1 PORT=8101 SERVE_STATIC=0
  ALLOWED_ORIGINS=https://lightsail-demo.cowhill.dev`).
* Every merge to `main` builds an allowlisted, checksummed release artifact,
  inspects it, uploads it through the restricted `deploy-lightsail-demo`
  account, builds the virtual environment on the VM, switches `current`
  atomically, restarts the service through the one permitted helper command
  and requires `/healthz` to report the new commit; otherwise it rolls back.

Why Caddy serves the static files: it is already the TLS terminator, it
serves files efficiently with correct caching and range support, and the
Python process then exposes nothing but the three WebSocket endpoints and
`/healthz`. That is also why `SERVE_STATIC=0` in production.

**An atomic symlink switch is not a zero-downtime WebSocket rollout.** Each
deployment restarts the single process: connected users are disconnected
(close code 1001), the chat scrollback, canvas and worm positions reset, and
users press Reconnect. There is no blue-green or multi-worker setup in this
pilot, by design.

The full contract, repository settings, runbook, logs and rollback commands
are in **[docs/deployment.md](docs/deployment.md)**. The normal path is:

```
edit -> pull request -> CI green -> merge to main -> Deploy workflow -> https://lightsail-demo.cowhill.dev/healthz shows the new revision
```

### Historical note (pre-2026 workflow, no longer used)

The demo used to be run by hand on the VM with `python ./main.py` inside a
`screen` session, serving the repository root on `0.0.0.0:8080`. That mode no
longer exists: the process is a systemd service, binds loopback only, and
never serves the repository. If you find old notes about `screen -r`, ignore
them and use the runbook in `docs/deployment.md`.
