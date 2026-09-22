# Deployment: lightsail-demo on the Lightsail server

This document is the application side of **deployment contract version 1**.
The server side (accounts, unit, helper, lock, Caddy, permissions) is owned
by [`pcowhill/cowhill-infrastructure`](https://github.com/pcowhill/cowhill-infrastructure)
and documented in its `docs/lightsail-demo.md`. Ordinary application
deployments never need a change there, and nothing in this repository can
change it: the deployment account has no administrator rights.

## The contract (fixed values)

| Item | Value |
|---|---|
| Contract version | 1 |
| Application ID | `lightsail-demo` |
| Public hostname | `lightsail-demo.cowhill.dev` |
| Private backend | `127.0.0.1:8101` (loopback only) |
| Deployment login | `deploy-lightsail-demo` (SSH key only, restricted) |
| Runtime user | `app-lightsail-demo` (nologin) |
| Service | `lightsail-demo.service` |
| Application root | `/srv/apps/lightsail-demo` |
| Upload staging | `/srv/apps/lightsail-demo/incoming` (0750, private to the deployment account) |
| Releases | `/srv/apps/lightsail-demo/releases/<full-commit-sha>` |
| Active symlink | `/srv/apps/lightsail-demo/current` |
| Public files (Caddy root) | `current/public` |
| Python environment | `current/.venv` (created on the VM by the deployment) |
| Entrypoint | `current/main.py` |
| Readiness marker | `current/.release-ready` (created last; the unit will not start without it) |
| Revision file | `current/REVISION` (full commit SHA) |
| Root-owned service helper | `/usr/local/sbin/cowhill-lightsail-demo-service` (`restart`, `stop`, `status`) |
| Shared lock | `/run/lock/cowhill-lightsail-demo.lock` |
| Root-owned environment | `/etc/cowhill/apps/lightsail-demo.env` |

Environment installed by the infrastructure and honoured by `main.py`:

```
HOST=127.0.0.1
PORT=8101
SERVE_STATIC=0
ALLOWED_ORIGINS=https://lightsail-demo.cowhill.dev
```

The application must not choose other paths, ports or variable names. If a
value here ever disagrees with the infrastructure repository, that is a
contract mismatch to report and fix on both sides, never something to patch
around on the server.

The server runs Ubuntu 24.04 LTS with the distribution `python3` 3.12.x;
supported range 3.10 to 3.12. The server-side preflight refuses anything
outside that range before a release is built.

## Repository settings (set once by the repository owner)

Settings → Secrets and variables → Actions, **repository** scope:

| Type | Name | Value |
|---|---|---|
| Variable | `LIGHTSAIL_HOST` | The VM's static IPv4 address |
| Variable | `LIGHTSAIL_USER` | `deploy-lightsail-demo` (the workflow refuses anything else) |
| Secret | `LIGHTSAIL_SSH_PRIVATE_KEY` | The **application** deployment private key (Ed25519, no passphrase). Generated on the VM through the Lightsail browser terminal; its public half is `server/apps/lightsail-demo/deploy-key.pub` in the infrastructure repository. Never the infrastructure key. |
| Secret | `LIGHTSAIL_KNOWN_HOSTS` | The independently verified host-key line for the server: `<ip> ssh-ed25519 AAAA...` (verify the fingerprint from the browser terminal with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` against `ssh-keyscan -t ed25519 <ip>` run from your own machine) |

Never paste these into chat, commits, issues or logs. The workflow only checks
that they exist; their values are written to files under `RUNNER_TEMP` with
mode 0600 for the duration of the deploy step and deleted afterwards.

Other prerequisites (manual, outside this repository): the Route 53 `A`
record `lightsail-demo.cowhill.dev → <static IPv4>` (TTL 300) and the
infrastructure apply having run. Until DNS resolves, Caddy cannot obtain a
certificate; the deployment then activates the release on the server but its
**public** checks fail with an explicit DNS/TLS message (exit status 2) and no
rollback, because the server side is fine.

## How a commit reaches production

```
edit -> pull request -> CI (ci.yml) green -> merge to main
      -> Deploy (deploy.yml): build job  -> lint + tests + artifact + inspection
                              deploy job -> stale check -> SSH preflight -> upload
                                         -> activate on the VM -> public verification
```

* **Pull requests** run only `ci.yml`. They receive no production variables
  or secrets and never contact the server.
* **`deploy.yml`** runs on `push` to `main` and on manual *Run workflow* from
  `main`. Both jobs carry the gate
  `github.ref == 'refs/heads/main' && (github.event_name == 'push' || github.event_name == 'workflow_dispatch')`,
  so a manual run can never deploy another branch. Deployments are
  serialized (`concurrency: deploy-lightsail-demo-production`,
  `cancel-in-progress: false`); a queued run for a commit that is no longer
  the head of `main` refuses to deploy. Every action is pinned to a commit
  SHA, checkouts do not persist credentials, and permissions are
  `contents: read` only.

### What the artifact is

`scripts/build-release.sh` builds `dist/lightsail-demo-<sha>.tar.gz` from a
clean checkout at `<sha>`: only `main.py`, `requirements.txt`, `REVISION`,
`lightsail_demo/**/*.py` and `public/**`, as 0644 files / 0755 directories
owned by 0:0 with the commit timestamp, so the same commit gives identical
bytes (CI checks this). `scripts/inspect_release.py` (standard library only)
verifies the SHA-256, then every member: relative normalized names on the
allowlist, no symlinks/hard links/devices, no hidden files, no executables,
bounded size, `REVISION` equal to the expected SHA, hash-pinned
`requirements.txt`. It runs on the runner **and again on the VM** before
extraction. The same verified artifact is deployed; the VM never clones or
fetches `main`.

### What happens on the server (`scripts/deploy/lightsail-demo-remote.sh`)

Piped over SSH (`ssh ... bash -s -- <subcommand>`) and run as
`deploy-lightsail-demo` with `StrictHostKeyChecking yes` against the pinned
host key. The only privileged operation it ever performs is
`sudo -n /usr/local/sbin/cowhill-lightsail-demo-service restart|stop|status`.

1. **preflight**: correct account; required tools; `incoming/`, `releases/`
   and the application root exist with the contract owner/modes; the lock is
   a regular readable file; the helper exists, is root-owned, not writable,
   and `sudo -n -l` permits exactly the helper `status` call (general sudo is
   never tested); `helper status` reports `lightsail-demo.service`; Python
   3.10 to 3.12 with `venv`/`ensurepip`; at least 200 MiB free; the
   environment file is present; a `current` link, if any, points into
   `releases/` and is marked ready.
2. **activate `<sha>` `<upload>`** (under the shared lock, `flock -w 300`):
   verify the checksum and inspect the artifact; if `<sha>` is already
   current and healthy, stop (idempotent re-run); remove an incomplete
   leftover `releases/<sha>` (never `current`, never the rollback target) or
   reuse a complete one; otherwise create `releases/<sha>`, extract, reject
   non-regular or hidden entries, create `.venv` **at that final path** with
   the system `python3`, `pip install --require-virtualenv --require-hashes
   --no-deps -r requirements.txt`, byte-compile, import `aiohttp` and the
   application with the production configuration, `chmod -R u=rwX,go=rX`,
   then `touch .release-ready` last. Record the previous target, switch
   `current` atomically (`ln -sfn` + `mv -T`), run the helper `restart`, and
   poll `http://127.0.0.1:8101/healthz` until it reports `status=ok` **and**
   `revision=<sha>` (up to 60 s). A 200 with the old revision is not success.
3. **On failure**: remove the new release's ready marker, switch back to the
   previous release, restart it and verify *its* revision; the run exits 1
   and reports "DEPLOYMENT FAILED ... rolled back to <previous>". On a first
   deployment with nothing to roll back to: helper `stop`, remove only the
   `current` link that was just created, exit 1. A successful rollback is
   still a failed deployment.
4. **On success**: keep at most three releases, always including `current`
   and the previous (rollback) release, removing only verified
   `releases/<40-hex>` directories; delete this account's `incoming/deploy-*`
   uploads; never touch shared roots.

The runner then checks DNS resolves to `LIGHTSAIL_HOST`, HTTPS answers,
`https://lightsail-demo.cowhill.dev/healthz` reports the new SHA, the landing
page, the three applet pages (both `/x/` and `/x/index.html`), the shared
script/stylesheet and a game asset return 200, `/.release-ready`, `/main.py`,
`/REVISION` and `/requirements.txt` return 404, and a WebSocket upgrade to
`/ws/game` with the production Origin succeeds (the server's initial worm
message proves the relay path without sending anything to real users) while a
foreign Origin is refused with 403.

### Expectations

* Each deployment **restarts the single process**: connected users are
  disconnected with close code 1001 and the in-memory state (chat, canvas,
  worms) resets. Users press **Reconnect**. This is not a zero-downtime
  rollout and is not meant to be in this pilot.
* The Origin allowlist is a browser cross-site guard, **not authentication**.
  Any non-browser client can send the allowed Origin. Limits are per
  connection and per applet, not per IP.
* Both Unix accounts share the VM's kernel and network with Caddy; the
  isolation is Unix permissions plus systemd hardening, not containers (see
  the infrastructure documentation's security model).

## Runbook

All commands from a machine holding the application deployment key
(`~/.ssh/lightsail-demo-deploy`) and the verified host key in
`~/.ssh/known_hosts`. Replace `<ip>` with the static IPv4.

```bash
SSH='ssh -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -i ~/.ssh/lightsail-demo-deploy deploy-lightsail-demo@<ip>'

# Deployed revision and public health
curl -s https://lightsail-demo.cowhill.dev/healthz

# Server-side status: current link, local health, unit properties, releases on disk
$SSH bash -s -- status < scripts/deploy/lightsail-demo-remote.sh

# Contract preflight (read-only)
$SSH bash -s -- preflight < scripts/deploy/lightsail-demo-remote.sh

# Service control (the only privileged commands the account has)
$SSH sudo -n /usr/local/sbin/cowhill-lightsail-demo-service status
$SSH sudo -n /usr/local/sbin/cowhill-lightsail-demo-service restart
```

**Rollback** (same lock, same health checks as a deployment; exits non-zero
and restores the original release if the target does not come up healthy):

```bash
# to the previous ready release
$SSH bash -s -- rollback < scripts/deploy/lightsail-demo-remote.sh
# or to a specific retained release
$SSH bash -s -- rollback <full-commit-sha> < scripts/deploy/lightsail-demo-remote.sh
```

Alternatively, revert the offending commit on `main` through a pull request:
the Deploy workflow then ships the reverted code as a normal release, which is
the preferred, auditable way when time allows.

**Logs.** The deployment account has no journal access by design. An
administrator (`ubuntu` or the infrastructure account) reads them with
`sudo journalctl -u lightsail-demo -n 200 --no-pager` (`-f` to follow) and
Caddy's with `sudo journalctl -u caddy -n 200 --no-pager`. The application
logs connection counts, close reasons and startup configuration, never message
contents. `LOG_LEVEL` is not part of the contract environment, so it stays
`INFO` in production.

**Re-deploying the current commit** (for example after fixing repository
settings): *Actions → Deploy → Run workflow* on `main`. An already active,
healthy release is detected and left alone; an unhealthy one is restarted.

## Troubleshooting

| Symptom | Meaning / action |
|---|---|
| Deploy job: `Missing required GitHub Actions configuration` | Add the two variables and two secrets above; nothing was deployed. |
| Deploy job: `main is now at <sha>; this run is for <sha> and is superseded` | A newer commit landed; its own run deploys. Nothing to do. |
| `Permission denied (publickey)` | The private key secret does not match `deploy-key.pub` on the server, or the infrastructure apply that installs it has not run. |
| `Host key verification failed` | `LIGHTSAIL_KNOWN_HOSTS` does not match the server. Re-verify the fingerprint through the browser terminal; never switch to `accept-new`. |
| `sudo: a password is required` / `sudo does not permit` | The sudoers fragment is missing or changed on the server side (infrastructure). |
| `Python 3.x is outside the supported range` | The server interpreter changed; coordinate a new contract version. |
| `artifact inspection rejected` | The archive contains something outside the allowlist (a symlink, an executable, a hidden file). Fix the repository, never the server. |
| `DEPLOYMENT FAILED ... rolled back to <sha> (healthy again)` | The new release did not report its revision within 60 s. Read the journal as an administrator, fix, redeploy. The site kept running the previous release. |
| `PUBLIC CHECK FAILED: ... does not resolve` / `HTTPS ... did not succeed` | The release is active on the server, but DNS or the certificate is not ready. Create/fix the `A` record; re-run Deploy once `dig +short lightsail-demo.cowhill.dev` prints the IP. |
| `/healthz` is 502 | Caddy is up, the backend is not: check the unit state through the helper `status`, then the journal as an administrator. |
