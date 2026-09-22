#!/usr/bin/env bash
# lightsail-demo: runner-side deployment driver (deployment contract v1).
#
# Called by .github/workflows/deploy.yml after the release artifact for the
# exact main commit has been built, checksummed and inspected. Everything on
# the server happens through scripts/deploy/lightsail-demo-remote.sh as the
# unprivileged deployment account over SSH with a pinned host key.
#
# Required environment:
#   RELEASE_SHA        full commit SHA being deployed
#   ARTIFACT           path to dist/lightsail-demo-<sha>.tar.gz
#   LIGHTSAIL_HOST     the VM's static IPv4 address (repository variable)
#   LIGHTSAIL_USER     must be deploy-lightsail-demo (repository variable)
#   SSH_KEY_FILE       private key written by the workflow (mode 0600)
#   SSH_KNOWN_HOSTS    known_hosts file with the independently verified entry
# Optional:
#   PUBLIC_URL         default https://lightsail-demo.cowhill.dev
#   DEPLOY_RUN_ID      used to name the staging upload (default: timestamp)
#   SKIP_PUBLIC_CHECKS=1  activation only (tests / DNS not yet live)
#
# Exit status: 0 deployed and verified; 1 deployment failed (the server has
# been rolled back or left without a release, as reported); 2 the release is
# active locally but the public HTTPS checks did not pass (DNS/TLS/Caddy
# prerequisite, reported separately, no rollback).
set -Eeuo pipefail

log()  { printf '[deploy] %s\n' "$*"; }
die()  { printf '[deploy] ERROR: %s\n' "$*" >&2; exit "${2:-1}"; }

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "${here}/../.." && pwd)"
REMOTE_SCRIPT="${here}/lightsail-demo-remote.sh"
INSPECTOR="${repo}/scripts/inspect_release.py"
SMOKE="${here}/ws_smoke.py"

CONTRACT_DEPLOY_USER=deploy-lightsail-demo
CONTRACT_INCOMING=/srv/apps/lightsail-demo/incoming
PUBLIC_URL="${PUBLIC_URL:-https://lightsail-demo.cowhill.dev}"
PUBLIC_HOST="${PUBLIC_URL#https://}"; PUBLIC_HOST="${PUBLIC_HOST%%/*}"

# ------------------------------------------------------------ inputs -------
: "${RELEASE_SHA:?RELEASE_SHA is required}"
: "${ARTIFACT:?ARTIFACT is required}"
: "${LIGHTSAIL_HOST:?LIGHTSAIL_HOST is required}"
: "${LIGHTSAIL_USER:?LIGHTSAIL_USER is required}"
: "${SSH_KEY_FILE:?SSH_KEY_FILE is required}"
: "${SSH_KNOWN_HOSTS:?SSH_KNOWN_HOSTS is required}"

[[ "${RELEASE_SHA}" =~ ^[0-9a-f]{40}$ ]] || die "RELEASE_SHA must be a full lower-case commit SHA"
[[ "${LIGHTSAIL_USER}" == "${CONTRACT_DEPLOY_USER}" ]] \
  || die "LIGHTSAIL_USER is '${LIGHTSAIL_USER}'; the contract deployment login is ${CONTRACT_DEPLOY_USER} (never the infrastructure account)"
[[ "${LIGHTSAIL_HOST}" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "LIGHTSAIL_HOST must be the VM's IPv4 address"
[[ -f "${ARTIFACT}" ]] || die "artifact ${ARTIFACT} not found"
[[ "$(basename -- "${ARTIFACT}")" == "lightsail-demo-${RELEASE_SHA}.tar.gz" ]] || die "artifact name does not match RELEASE_SHA"
CHECKSUM="${ARTIFACT}.sha256"
[[ -f "${CHECKSUM}" ]] || die "checksum file ${CHECKSUM} not found"
[[ -f "${SSH_KEY_FILE}" && "$(stat -c %a -- "${SSH_KEY_FILE}")" == "600" ]] || die "SSH_KEY_FILE must exist with mode 0600"
[[ -f "${SSH_KNOWN_HOSTS}" ]] || die "SSH_KNOWN_HOSTS file not found"
for tool in ssh rsync sha256sum python3 curl getent; do
  command -v "${tool}" >/dev/null 2>&1 || die "required tool missing on the runner: ${tool}"
done

log "release ${RELEASE_SHA} -> ${LIGHTSAIL_USER}@${LIGHTSAIL_HOST} (${PUBLIC_URL})"

# ------------------------------------------------ verify the artifact ------
( cd "$(dirname -- "${ARTIFACT}")" && sha256sum --check --strict --quiet -- "$(basename -- "${CHECKSUM}")" ) \
  || die "artifact checksum mismatch on the runner"
python3 "${INSPECTOR}" "${ARTIFACT}" --sha "${RELEASE_SHA}" --checksum "${CHECKSUM}" --quiet \
  || die "artifact inspection failed on the runner"
log "artifact verified on the runner"

# --------------------------------------------------- SSH configuration -----
ssh-keygen -F "${LIGHTSAIL_HOST}" -f "${SSH_KNOWN_HOSTS}" >/dev/null 2>&1 \
  || die "known_hosts has no entry for ${LIGHTSAIL_HOST}; refusing to connect without a pinned host key"
ssh_config="$(mktemp)"
trap 'rm -f "${ssh_config}"' EXIT
cat > "${ssh_config}" <<CONFIG
Host lightsail-demo
  HostName ${LIGHTSAIL_HOST}
  User ${LIGHTSAIL_USER}
  Port 22
  IdentityFile ${SSH_KEY_FILE}
  IdentitiesOnly yes
  UserKnownHostsFile ${SSH_KNOWN_HOSTS}
  StrictHostKeyChecking yes
  UpdateHostKeys no
  HashKnownHosts no
  BatchMode yes
  PasswordAuthentication no
  KbdInteractiveAuthentication no
  ForwardAgent no
  ForwardX11 no
  RequestTTY no
  ConnectTimeout 30
  ServerAliveInterval 30
  ServerAliveCountMax 4
  LogLevel ERROR
CONFIG
ssh_cmd=(ssh -F "${ssh_config}" lightsail-demo)

remote() {
  # remote <subcommand> [args]: pipe the remote script over SSH.
  "${ssh_cmd[@]}" bash -s -- "$@" < "${REMOTE_SCRIPT}"
}

# -------------------------------------------------------- preflight --------
log "connecting and running the server-side preflight"
remote preflight || die "server-side preflight failed; nothing was changed"

# ------------------------------------------------------------ upload -------
upload="deploy-${RELEASE_SHA:0:12}-${DEPLOY_RUN_ID:-$(date +%s)}"
[[ "${upload}" =~ ^deploy-[0-9a-f]{12}-[0-9A-Za-z_-]+$ ]] || die "internal: bad upload name ${upload}"
stage="$(mktemp -d)"
trap 'rm -f "${ssh_config}"; rm -rf "${stage}"' EXIT
cp -- "${ARTIFACT}" "${CHECKSUM}" "${INSPECTOR}" "${stage}/"
log "uploading artifact, checksum and inspector to ${CONTRACT_INCOMING}/${upload}/"
rsync --archive --no-links --no-owner --no-group --chmod=D0750,F0640 \
      --rsh="ssh -F ${ssh_config}" --quiet \
      "${stage}/" "lightsail-demo:${CONTRACT_INCOMING}/${upload}/" \
  || die "upload failed"

# ---------------------------------------------------------- activate -------
log "activating ${RELEASE_SHA} on the server"
if ! remote activate "${RELEASE_SHA}" "${upload}"; then
  die "activation failed; see the [remote] output above for the rollback result" 1
fi

# ------------------------------------------------ public verification ------
if [[ "${SKIP_PUBLIC_CHECKS:-0}" == "1" ]]; then
  log "public HTTPS checks skipped (SKIP_PUBLIC_CHECKS=1)"
  exit 0
fi

public_fail() { printf '[deploy] PUBLIC CHECK FAILED: %s\n' "$*" >&2; exit 2; }

log "checking the DNS/TLS prerequisite for ${PUBLIC_HOST}"
resolved="$(getent ahostsv4 "${PUBLIC_HOST}" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ' || true)"
[[ -n "${resolved}" ]] || public_fail "${PUBLIC_HOST} does not resolve. Create the Route 53 A record (${PUBLIC_HOST} -> ${LIGHTSAIL_HOST}); the release is active on the server."
if [[ " ${resolved}" != *" ${LIGHTSAIL_HOST} "* ]]; then
  public_fail "${PUBLIC_HOST} resolves to ${resolved}, not ${LIGHTSAIL_HOST}. Fix the A record; the release is active on the server."
fi
tls_ok=0
for attempt in $(seq 1 12); do
  if curl -fsS --max-time 10 -o /dev/null "${PUBLIC_URL}/healthz"; then tls_ok=1; break; fi
  log "TLS/HTTP not ready yet (attempt ${attempt}/12); waiting 10s (Caddy needs a resolving name to obtain the certificate)"
  sleep 10
done
(( tls_ok == 1 )) || public_fail "HTTPS to ${PUBLIC_URL} did not succeed. If DNS was just created, Caddy may still be obtaining the certificate; re-run 'Deploy' once it resolves."

log "checking that the public health endpoint reports ${RELEASE_SHA}"
health_ok=0
for attempt in $(seq 1 12); do
  body="$(curl -fsS --max-time 10 "${PUBLIC_URL}/healthz" || true)"
  if python3 - "${RELEASE_SHA}" "${body}" <<'PY'
import json, sys
sha, body = sys.argv[1], sys.argv[2]
try:
    d = json.loads(body)
except Exception:
    sys.exit(1)
sys.exit(0 if d.get("status") == "ok" and d.get("service") == "lightsail-demo" and d.get("revision") == sha else 1)
PY
  then health_ok=1; break; fi
  sleep 5
done
(( health_ok == 1 )) || public_fail "${PUBLIC_URL}/healthz does not report revision ${RELEASE_SHA} (got: ${body:-nothing})"
log "public /healthz reports revision ${RELEASE_SHA}"

check_url() {
  # check_url <path> <expected-status> [<substring>]
  local path="$1" want="$2" needle="${3:-}" tmp code
  tmp="$(mktemp)"
  code="$(curl -sS --max-time 15 -o "${tmp}" -w '%{http_code}' "${PUBLIC_URL}${path}" || echo 000)"
  if [[ "${code}" != "${want}" ]]; then rm -f "${tmp}"; public_fail "${path} returned ${code}, expected ${want}"; fi
  if [[ -n "${needle}" ]] && ! grep -q -- "${needle}" "${tmp}"; then rm -f "${tmp}"; public_fail "${path} did not contain '${needle}'"; fi
  rm -f "${tmp}"
  log "ok ${want} ${path}"
}
check_url "/"                        200 "Lightsail Tower"
check_url "/chat/"                   200 "WebSocket Chat"
check_url "/chat/index.html"         200 "WebSocket Chat"
check_url "/draw/"                   200 "WebSocket Draw"
check_url "/draw/index.html"         200 "WebSocket Draw"
check_url "/game/"                   200 "gameCanvas"
check_url "/game/index.html"         200 "gameCanvas"
check_url "/game/Robin.png"          200
check_url "/shared/demo-socket.js"   200 "DemoSocket"
check_url "/shared/demo.css"         200
check_url "/.release-ready"          404
check_url "/main.py"                 404
check_url "/REVISION"                404
check_url "/requirements.txt"        404

log "WebSocket smoke check through Caddy with the production Origin"
python3 "${SMOKE}" "${PUBLIC_URL}" --timeout 15 || public_fail "WebSocket smoke check failed"

log "DEPLOYED AND VERIFIED: ${PUBLIC_URL} serves revision ${RELEASE_SHA}"
