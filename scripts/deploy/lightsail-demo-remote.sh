#!/usr/bin/env bash
# lightsail-demo: release activation on the server (deployment contract v1).
#
# Runs on the Lightsail VM as the unprivileged deployment account
# (deploy-lightsail-demo), piped over SSH by scripts/deploy/deploy.sh:
#
#   ssh lightsail-demo bash -s -- preflight                 < this file
#   ssh lightsail-demo bash -s -- activate <sha> <upload>   < this file
#   ssh lightsail-demo bash -s -- rollback [<sha>]          < this file
#   ssh lightsail-demo bash -s -- status                    < this file
#
# It only ever touches this application's own directories under
# /srv/apps/lightsail-demo, and the one privileged operation it performs is
# the exact helper invocation the infrastructure permits:
#   sudo -n /usr/local/sbin/cowhill-lightsail-demo-service restart|stop|status
# No other sudo, no package installation, no changes to the unit, environment
# file, Caddy or anything root-owned.
#
# activate <sha> <upload>:
#   1. verify the uploaded artifact (SHA-256 file, then member inspection with
#      the stdlib inspector uploaded next to it) as this account;
#   2. create releases/<sha> fresh, extract, build .venv *at that path*, install
#      the hash-pinned lock, import/compile check, permission fix, mark ready;
#   3. under the shared lock: remember the previous target, switch "current"
#      atomically, restart through the helper, require /healthz to report the
#      NEW sha;
#   4. on failure: switch back to the previous release and restart it (or, on a
#      first deployment, stop the service and remove only the new link) and
#      exit non-zero: a successful rollback is still a failed deployment;
#   5. on success: keep at most three releases (always current and the
#      previous one), clean this account's staging uploads.
#
# Sandbox: with LIGHTSAIL_DEMO_SANDBOX=1 the contract paths, helper, backend URL
# and interpreter can be overridden through the environment so the exact same
# logic can be exercised in tests against temporary directories and a fake
# helper. Without it every override is ignored and the contract values apply.

set -Eeuo pipefail
umask 022

# ---------------------------------------------------------------- contract --
CONTRACT_VERSION=1
APP_ID=lightsail-demo
DEPLOY_USER=deploy-lightsail-demo
APP_ROOT=/srv/apps/lightsail-demo
HELPER=/usr/local/sbin/cowhill-lightsail-demo-service
LOCK_FILE=/run/lock/cowhill-lightsail-demo.lock
ENV_FILE=/etc/cowhill/apps/lightsail-demo.env
BACKEND_URL=http://127.0.0.1:8101
PYTHON3=/usr/bin/python3
PYTHON_MIN_MINOR=10
PYTHON_MAX_MINOR=12
SUDO=sudo
KEEP_RELEASES=3
HEALTH_TIMEOUT=60
LOCK_TIMEOUT=300
MIN_FREE_KB=204800   # 200 MiB

if [[ "${LIGHTSAIL_DEMO_SANDBOX:-0}" == "1" ]]; then
  APP_ROOT="${SANDBOX_APP_ROOT:-${APP_ROOT}}"
  HELPER="${SANDBOX_HELPER:-${HELPER}}"
  LOCK_FILE="${SANDBOX_LOCK_FILE:-${LOCK_FILE}}"
  ENV_FILE="${SANDBOX_ENV_FILE:-${ENV_FILE}}"
  BACKEND_URL="${SANDBOX_BACKEND_URL:-${BACKEND_URL}}"
  PYTHON3="${SANDBOX_PYTHON3:-${PYTHON3}}"
  DEPLOY_USER="${SANDBOX_DEPLOY_USER:-$(id -un)}"
  SUDO="${SANDBOX_SUDO:-}"          # empty: run the fake helper directly
  HEALTH_TIMEOUT="${SANDBOX_HEALTH_TIMEOUT:-${HEALTH_TIMEOUT}}"
  PYTHON_MAX_MINOR="${SANDBOX_PYTHON_MAX_MINOR:-${PYTHON_MAX_MINOR}}"
  MIN_FREE_KB="${SANDBOX_MIN_FREE_KB:-${MIN_FREE_KB}}"
  echo "*** SANDBOX MODE: contract paths overridden for testing; never use against production ***" >&2
fi

INCOMING_DIR="${APP_ROOT}/incoming"
RELEASES_DIR="${APP_ROOT}/releases"
CURRENT_LINK="${APP_ROOT}/current"

# ------------------------------------------------------------------- utils --
log()  { printf '[remote] %s\n' "$*"; }
warn() { printf '[remote] WARNING: %s\n' "$*" >&2; }
die()  { printf '[remote] ERROR: %s\n' "$*" >&2; exit 1; }

is_sha() { [[ "$1" =~ ^[0-9a-f]{40}$ ]]; }

helper() {
  # The only privileged call this script makes. Arguments are one fixed word.
  case "${1:-}" in restart|stop|status) ;; *) die "internal: bad helper action '${1:-}'";; esac
  if [[ -n "${SUDO}" ]]; then
    "${SUDO}" -n "${HELPER}" "$1"
  else
    "${HELPER}" "$1"
  fi
}

health_json() {
  # Prints "<status> <revision>" from the local backend, or nothing on failure.
  curl -fsS --max-time 3 "${BACKEND_URL}/healthz" 2>/dev/null \
    | "${PYTHON3}" -c 'import json,sys
try:
    d=json.load(sys.stdin)
    if d.get("service")!="lightsail-demo": raise SystemExit(1)
    print(d.get("status",""), d.get("revision",""))
except Exception:
    raise SystemExit(1)' 2>/dev/null || true
}

wait_for_revision() {
  # wait_for_revision <sha> <timeout-seconds>: 0 once /healthz says ok + sha.
  local want="$1" timeout="$2" deadline now out
  deadline=$(( $(date +%s) + timeout ))
  while :; do
    out="$(health_json)"
    if [[ "${out}" == "ok ${want}" ]]; then
      return 0
    fi
    now=$(date +%s)
    if (( now >= deadline )); then
      log "health check timed out after ${timeout}s (last answer: '${out:-none}')"
      return 1
    fi
    sleep 1
  done
}

current_target() {
  # Real path of the active release, or empty when there is no current link.
  if [[ -L "${CURRENT_LINK}" ]]; then
    readlink -f -- "${CURRENT_LINK}" || true
  fi
}

release_sha_of() {
  # release_sha_of <release-dir>: the REVISION content if valid, else empty.
  local rev
  rev="$(head -c 41 -- "$1/REVISION" 2>/dev/null | tr -d '\n' || true)"
  if is_sha "${rev}"; then printf '%s' "${rev}"; fi
}

is_release_dir() {
  # A directory directly under releases/ whose name is a full SHA.
  local real
  [[ -d "$1" && ! -L "$1" ]] || return 1
  real="$(readlink -f -- "$1")"
  [[ "$(dirname -- "${real}")" == "$(readlink -f -- "${RELEASES_DIR}")" ]] || return 1
  is_sha "$(basename -- "${real}")"
}

switch_current() {
  # switch_current <release-dir>: atomic replace of the current symlink.
  local target="$1" tmp="${CURRENT_LINK}.tmp"
  is_release_dir "${target}" || die "refusing to point current at ${target}"
  if [[ -e "${tmp}" && ! -L "${tmp}" ]]; then
    die "${tmp} exists and is not a symlink; refusing to continue"
  fi
  ln -sfn -- "${target}" "${tmp}"
  mv -T -- "${tmp}" "${CURRENT_LINK}"
  log "current -> ${target}"
}

acquire_lock() {
  [[ -e "${LOCK_FILE}" ]] || die "shared lock ${LOCK_FILE} does not exist (infrastructure not applied?)"
  [[ -f "${LOCK_FILE}" && ! -L "${LOCK_FILE}" ]] || die "shared lock ${LOCK_FILE} is not a regular file"
  exec 9<"${LOCK_FILE}" || die "cannot open ${LOCK_FILE} for locking"
  log "waiting for ${LOCK_FILE} (up to ${LOCK_TIMEOUT}s)"
  flock -w "${LOCK_TIMEOUT}" 9 || die "could not acquire ${LOCK_FILE} within ${LOCK_TIMEOUT}s"
  log "holding ${LOCK_FILE}"
}

release_lock() {
  flock -u 9 2>/dev/null || true
  exec 9<&- 2>/dev/null || true
}

# ---------------------------------------------------------------- preflight --
check_dir() {
  # check_dir <path> <expected-mode-regex> <description>
  local path="$1" mode_re="$2" what="$3" owner mode
  [[ -d "${path}" && ! -L "${path}" ]] || die "${what} ${path} is missing or not a directory"
  owner="$(stat -c %U -- "${path}")"
  mode="$(stat -c %a -- "${path}")"
  [[ "${owner}" == "$(id -un)" ]] || die "${what} ${path} is owned by ${owner}, not $(id -un)"
  [[ "${mode}" =~ ${mode_re} ]] || die "${what} ${path} has mode ${mode}, expected ${mode_re}"
  [[ -w "${path}" ]] || die "${what} ${path} is not writable"
}

preflight() {
  local fatal=0
  log "preflight for ${APP_ID} (contract v${CONTRACT_VERSION}) as $(id -un)@$(hostname)"

  [[ "$(id -un)" == "${DEPLOY_USER}" ]] || die "must run as ${DEPLOY_USER}, not $(id -un)"
  for tool in flock tar gzip sha256sum curl find sort stat df readlink mktemp; do
    command -v "${tool}" >/dev/null 2>&1 || die "required tool missing on the server: ${tool}"
  done

  check_dir "${APP_ROOT}"     '^755$' "application root"
  check_dir "${INCOMING_DIR}" '^7[05]0$' "upload staging"
  check_dir "${RELEASES_DIR}" '^755$' "releases directory"

  [[ -e "${LOCK_FILE}" ]] || die "shared lock ${LOCK_FILE} missing"
  [[ -f "${LOCK_FILE}" && ! -L "${LOCK_FILE}" && -r "${LOCK_FILE}" ]] || die "shared lock ${LOCK_FILE} is not a readable regular file"
  ( exec 8<"${LOCK_FILE}"; flock -n 8 ) || warn "shared lock is currently held by another process (an apply or deploy is running)"

  [[ -e "${HELPER}" ]] || die "service helper ${HELPER} is missing"
  [[ -x "${HELPER}" ]] || die "service helper ${HELPER} is not executable"
  if [[ -z "${SUDO}" ]]; then
    :  # sandbox: the fake helper runs directly
  else
    [[ "$(stat -c %u -- "${HELPER}")" == "0" ]] || die "service helper ${HELPER} is not owned by root"
    [[ ! -w "${HELPER}" ]] || die "service helper ${HELPER} is writable by this account"
    # sudo -l with the exact command lists whether *this* invocation is
    # permitted without running anything; general sudo is never tested.
    "${SUDO}" -n -l "${HELPER}" status >/dev/null 2>&1 \
      || die "sudo does not permit '${HELPER} status' for $(id -un) (sudoers fragment missing?)"
  fi
  local status_out
  if status_out="$(helper status 2>&1)"; then
    grep -q '^Id=lightsail-demo.service$' <<<"${status_out}" \
      || die "helper status did not report lightsail-demo.service:"$'\n'"${status_out}"
    log "service: $(grep -E '^(ActiveState|SubState|ConditionResult)=' <<<"${status_out}" | tr '\n' ' ')"
  else
    die "helper status failed:"$'\n'"${status_out}"
  fi

  [[ -x "${PYTHON3}" ]] || die "interpreter ${PYTHON3} not found"
  local pyver minor
  pyver="$("${PYTHON3}" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
  minor="$("${PYTHON3}" -c 'import sys; print(sys.version_info[1])')"
  [[ "$("${PYTHON3}" -c 'import sys; print(sys.version_info[0])')" == "3" ]] || die "Python 3 required"
  if (( minor < PYTHON_MIN_MINOR || minor > PYTHON_MAX_MINOR )); then
    die "Python ${pyver} is outside the supported range 3.${PYTHON_MIN_MINOR}-3.${PYTHON_MAX_MINOR}"
  fi
  "${PYTHON3}" -c 'import venv, ensurepip' 2>/dev/null || die "python3-venv / ensurepip is not available"
  log "python: ${pyver} at ${PYTHON3}"

  local free_kb
  free_kb="$(df -Pk -- "${RELEASES_DIR}" | awk 'NR==2 {print $4}')"
  (( free_kb >= MIN_FREE_KB )) || die "only ${free_kb} KiB free under ${RELEASES_DIR}; need ${MIN_FREE_KB} KiB"
  log "disk: ${free_kb} KiB free"

  if [[ -e "${ENV_FILE}" ]]; then
    log "environment file present: ${ENV_FILE} (root-owned; not readable by this account by design)"
  else
    warn "environment file ${ENV_FILE} not visible; the infrastructure apply may not have run"
    fatal=1
  fi

  local cur
  cur="$(current_target)"
  if [[ -n "${cur}" ]]; then
    is_release_dir "${cur}" || die "current points outside releases/: ${cur}"
    [[ -f "${cur}/.release-ready" ]] || warn "current release ${cur} has no .release-ready marker"
    log "current: ${cur} (revision $(release_sha_of "${cur}" || echo unknown)); backend: $(health_json || echo 'no answer')"
  else
    log "current: none (first deployment)"
  fi
  (( fatal == 0 )) || die "preflight found blocking problems"
  log "preflight ok"
}

# ------------------------------------------------------------------- build --
build_release() {
  # build_release <sha> <artifact> : creates RELEASES_DIR/<sha> ready to run.
  local sha="$1" artifact="$2" dir="${RELEASES_DIR}/$1"
  [[ ! -e "${dir}" ]] || die "internal: ${dir} already exists"
  log "creating ${dir}"
  mkdir -m 0755 -- "${dir}"
  tar --no-same-owner --no-same-permissions --no-overwrite-dir \
      -xzf "${artifact}" -C "${dir}"

  # Belt and braces after the inspector: nothing but files and directories,
  # no hidden entries, and the revision file is the one we expect.
  if find "${dir}" ! -type f ! -type d -print -quit | grep -q .; then
    die "extracted release contains a non-regular file"
  fi
  if find "${dir}" -name '.*' -print -quit | grep -q .; then
    die "extracted release contains a hidden entry"
  fi
  [[ "$(release_sha_of "${dir}")" == "${sha}" ]] || die "REVISION in the release does not match ${sha}"
  for f in main.py requirements.txt lightsail_demo/app.py public/index.html \
           public/chat/index.html public/draw/index.html public/game/index.html; do
    [[ -f "${dir}/${f}" ]] || die "release is missing ${f}"
  done

  log "creating virtual environment at ${dir}/.venv with ${PYTHON3}"
  "${PYTHON3}" -m venv -- "${dir}/.venv"
  log "installing the hash-pinned runtime lock"
  "${dir}/.venv/bin/python" -m pip install \
      --require-virtualenv --require-hashes --no-deps --no-cache-dir \
      --disable-pip-version-check --no-input --quiet \
      -r "${dir}/requirements.txt"

  log "verifying imports, bytecode and production configuration"
  ( cd "${dir}" && "${dir}/.venv/bin/python" -m compileall -q -f main.py lightsail_demo >/dev/null )
  ( cd "${dir}" && "${dir}/.venv/bin/python" - "${dir}" <<'PYCHECK'
import pathlib, sys
import aiohttp  # noqa: F401
from lightsail_demo.app import REVISION_KEY, create_app
from lightsail_demo.config import load_settings

release = pathlib.Path(sys.argv[1])
settings = load_settings(
    {"HOST": "127.0.0.1", "PORT": "8101", "SERVE_STATIC": "0",
     "ALLOWED_ORIGINS": "https://lightsail-demo.cowhill.dev"},
    app_dir=release,
)
app = create_app(settings)
assert app[REVISION_KEY] == release.joinpath("REVISION").read_text().strip()
print("import/config check ok, aiohttp", aiohttp.__version__)
PYCHECK
  )

  # Runtime user and Caddy read; nobody but this account writes.
  chmod -R u=rwX,go=rX -- "${dir}"
  find "${dir}" -type d -exec chmod 0755 {} +
  : > "${dir}/.release-ready"
  chmod 0644 -- "${dir}/.release-ready"
  log "release ${sha} prepared and marked ready"
}

remove_release_dir() {
  # remove_release_dir <dir>: rm -rf with guards (never current/previous).
  local dir="$1" real cur
  is_release_dir "${dir}" || die "refusing to remove ${dir}: not a release directory"
  real="$(readlink -f -- "${dir}")"
  cur="$(current_target)"
  [[ -z "${cur}" || "${real}" != "${cur}" ]] || die "refusing to remove the active release ${real}"
  [[ -z "${PROTECTED_RELEASE:-}" || "${real}" != "${PROTECTED_RELEASE}" ]] || die "refusing to remove the rollback release ${real}"
  log "removing ${real}"
  rm -rf -- "${real}"
}

prune_releases() {
  # Keep KEEP_RELEASES directories at most: current, the rollback target, then
  # the newest ready ones. Failed (unready) leftovers go first.
  local cur="$1" prev="$2" dir real keep=() candidates=()
  while IFS= read -r -d '' dir; do
    is_release_dir "${dir}" || continue
    real="$(readlink -f -- "${dir}")"
    [[ "${real}" == "${cur}" || "${real}" == "${prev}" ]] && continue
    candidates+=("${real}")
  done < <(find "${RELEASES_DIR}" -mindepth 1 -maxdepth 1 -type d -print0)
  (( ${#candidates[@]} > 0 )) || return 0

  # Slots left after current (and the rollback target, when there is one).
  local budget=$(( KEEP_RELEASES - 1 ))
  if [[ -n "${prev}" ]]; then budget=$(( budget - 1 )); fi
  (( budget < 0 )) && budget=0
  # Ready releases newest first; unready ones are never kept.
  while IFS= read -r real; do
    [[ -n "${real}" ]] || continue
    if [[ -f "${real}/.release-ready" ]] && (( ${#keep[@]} < budget )); then
      keep+=("${real}")
    else
      PROTECTED_RELEASE="${prev}" remove_release_dir "${real}"
    fi
  done < <(for real in "${candidates[@]}"; do printf '%s %s\n' "$(stat -c %Y -- "${real}")" "${real}"; done | sort -rn | cut -d' ' -f2-)
  log "retained releases: $(find "${RELEASES_DIR}" -mindepth 1 -maxdepth 1 -type d | wc -l) (limit ${KEEP_RELEASES})"
}

clean_incoming() {
  # Remove this account's staging uploads (deploy-* directories only).
  local entry
  while IFS= read -r -d '' entry; do
    [[ -d "${entry}" && ! -L "${entry}" ]] || continue
    case "$(basename -- "${entry}")" in
      deploy-*) log "cleaning staging $(basename -- "${entry}")"; rm -rf -- "${entry}" ;;
    esac
  done < <(find "${INCOMING_DIR}" -mindepth 1 -maxdepth 1 -print0)
}

# ---------------------------------------------------------------- activate --
activate() {
  local sha="${1:-}" upload="${2:-}"
  is_sha "${sha}" || die "activate: first argument must be a full lower-case commit SHA"
  [[ -n "${upload}" && "${upload}" != */* && "${upload}" != .* ]] || die "activate: upload must be a plain directory name under incoming/"
  local upload_dir="${INCOMING_DIR}/${upload}"
  local artifact="${upload_dir}/lightsail-demo-${sha}.tar.gz"
  local checksum="${artifact}.sha256"
  local inspector="${upload_dir}/inspect_release.py"

  preflight
  [[ -d "${upload_dir}" && ! -L "${upload_dir}" ]] || die "upload directory ${upload_dir} not found"
  for f in "${artifact}" "${checksum}" "${inspector}"; do
    [[ -f "${f}" && ! -L "${f}" ]] || die "missing upload file: ${f}"
  done

  acquire_lock
  trap 'release_lock' EXIT

  log "verifying artifact checksum"
  ( cd "${upload_dir}" && sha256sum --check --strict --quiet -- "$(basename -- "${checksum}")" ) \
    || die "SHA-256 verification failed for ${artifact}"
  log "inspecting artifact members"
  "${PYTHON3}" "${inspector}" "${artifact}" --sha "${sha}" --checksum "${checksum}" --quiet \
    || die "artifact inspection rejected ${artifact}"

  local previous release_dir="${RELEASES_DIR}/${sha}" previous_sha=""
  previous="$(current_target)"
  if [[ -n "${previous}" ]]; then
    previous_sha="$(release_sha_of "${previous}" || true)"
    log "previous release: ${previous} (revision ${previous_sha:-unknown})"
  fi

  if [[ -n "${previous}" && "${previous}" == "$(readlink -f -- "${release_dir}" 2>/dev/null || true)" ]]; then
    if [[ "$(health_json)" == "ok ${sha}" ]]; then
      log "release ${sha} is already active and healthy; nothing to do"
      clean_incoming
      return 0
    fi
    log "release ${sha} is already current but not healthy; restarting it"
    helper restart || die "helper restart failed for the already-current release"
    wait_for_revision "${sha}" "${HEALTH_TIMEOUT}" || die "current release ${sha} did not become healthy"
    log "release ${sha} healthy after restart"
    clean_incoming
    return 0
  fi

  if [[ -e "${release_dir}" ]]; then
    if [[ -f "${release_dir}/.release-ready" && "$(release_sha_of "${release_dir}")" == "${sha}" \
          && -x "${release_dir}/.venv/bin/python" ]]; then
      log "complete release ${sha} already exists; reusing it without rebuilding"
    else
      log "incomplete release ${sha} left over from an earlier run"
      PROTECTED_RELEASE="${previous}" remove_release_dir "${release_dir}"
    fi
  fi
  if [[ ! -e "${release_dir}" ]]; then
    build_release "${sha}" "${artifact}"
  fi

  log "switching current and restarting the service"
  switch_current "${release_dir}"
  local helper_rc=0 helper_out=""
  helper_out="$(helper restart 2>&1)" || helper_rc=$?
  printf '%s\n' "${helper_out}" | sed 's/^/[helper] /'

  if (( helper_rc == 0 )) && wait_for_revision "${sha}" "${HEALTH_TIMEOUT}"; then
    log "backend reports revision ${sha}"
    helper status | grep -E '^(ActiveState|SubState|MainPID)=' | sed 's/^/[helper] /' || true
    prune_releases "$(readlink -f -- "${release_dir}")" "${previous}"
    clean_incoming
    log "DEPLOYED ${sha}${previous_sha:+ (previous ${previous_sha} retained for rollback)}"
    return 0
  fi

  # ---- failed activation: roll back ----------------------------------------
  warn "activation of ${sha} failed (helper exit ${helper_rc}); rolling back"
  rm -f -- "${release_dir}/.release-ready"   # never start this release again
  if [[ -n "${previous}" && -f "${previous}/.release-ready" ]]; then
    switch_current "${previous}"
    local rb_rc=0
    helper restart 2>&1 | sed 's/^/[helper] /' || rb_rc=$?
    if [[ -n "${previous_sha}" ]] && wait_for_revision "${previous_sha}" "${HEALTH_TIMEOUT}"; then
      die "DEPLOYMENT FAILED: ${sha} did not become healthy; rolled back to ${previous_sha} (healthy again)"
    fi
    die "DEPLOYMENT FAILED: ${sha} did not become healthy AND rollback to ${previous_sha:-previous} did not report healthy (helper exit ${rb_rc}); manual attention required"
  fi
  # First deployment: nothing to roll back to. Stop the unit and remove the
  # link we introduced so the server is back to "no release".
  helper stop 2>&1 | sed 's/^/[helper] /' || true
  if [[ -L "${CURRENT_LINK}" && "$(readlink -f -- "${CURRENT_LINK}")" == "$(readlink -f -- "${release_dir}")" ]]; then
    rm -f -- "${CURRENT_LINK}"
    log "removed ${CURRENT_LINK} (first deployment failed; no previous release)"
  fi
  die "DEPLOYMENT FAILED: first release ${sha} did not become healthy; service stopped, no current release"
}

# ---------------------------------------------------------------- rollback --
rollback() {
  local want="${1:-}" cur cur_sha target target_sha dir
  preflight
  acquire_lock
  trap 'release_lock' EXIT
  cur="$(current_target)"
  [[ -n "${cur}" ]] || die "rollback: there is no current release"
  cur_sha="$(release_sha_of "${cur}" || true)"

  if [[ -n "${want}" ]]; then
    is_sha "${want}" || die "rollback: argument must be a full commit SHA"
    target="${RELEASES_DIR}/${want}"
  else
    target=""
    while IFS= read -r dir; do
      [[ -n "${dir}" ]] || continue
      [[ "$(readlink -f -- "${dir}")" == "${cur}" ]] && continue
      [[ -f "${dir}/.release-ready" ]] || continue
      target="${dir}"; break
    done < <(for d in "${RELEASES_DIR}"/*/; do d="${d%/}"; is_release_dir "${d}" && printf '%s %s\n' "$(stat -c %Y -- "${d}")" "${d}"; done | sort -rn | cut -d' ' -f2-)
    [[ -n "${target}" ]] || die "rollback: no other ready release to roll back to"
  fi
  is_release_dir "${target}" || die "rollback: ${target} is not a release directory"
  [[ -f "${target}/.release-ready" ]] || die "rollback: ${target} is not marked ready"
  target_sha="$(release_sha_of "${target}")"
  [[ "${target_sha}" == "$(basename -- "$(readlink -f -- "${target}")")" ]] || die "rollback: REVISION mismatch in ${target}"
  [[ "$(readlink -f -- "${target}")" != "${cur}" ]] || die "rollback: ${target_sha} is already the current release"

  log "rolling back from ${cur_sha:-unknown} to ${target_sha}"
  switch_current "${target}"
  helper restart 2>&1 | sed 's/^/[helper] /' || true
  if wait_for_revision "${target_sha}" "${HEALTH_TIMEOUT}"; then
    log "ROLLED BACK to ${target_sha}; backend healthy"
    return 0
  fi
  warn "rollback target ${target_sha} did not become healthy; restoring ${cur_sha:-previous current}"
  switch_current "${cur}"
  helper restart 2>&1 | sed 's/^/[helper] /' || true
  if [[ -n "${cur_sha}" ]] && wait_for_revision "${cur_sha}" "${HEALTH_TIMEOUT}"; then
    die "ROLLBACK FAILED: ${target_sha} unhealthy; ${cur_sha} restored and healthy"
  fi
  die "ROLLBACK FAILED: neither ${target_sha} nor ${cur_sha:-the previous current} reports healthy; manual attention required"
}

status() {
  local cur
  cur="$(current_target)"
  log "current: ${cur:-none}${cur:+ (revision $(release_sha_of "${cur}" || echo unknown))}"
  log "backend: $(health_json || echo 'no answer')"
  helper status | sed 's/^/[helper] /' || true
  log "releases:"
  find "${RELEASES_DIR}" -mindepth 1 -maxdepth 1 -type d -printf '%TY-%Tm-%Td %TH:%TM %f\n' 2>/dev/null | sort || true
}

# -------------------------------------------------------------------- main --
case "${1:-}" in
  preflight) preflight ;;
  activate)  activate "${2:-}" "${3:-}" ;;
  rollback)  rollback "${2:-}" ;;
  status)    status ;;
  *) echo "usage: $0 preflight | activate <sha> <upload-dir-name> | rollback [<sha>] | status" >&2; exit 64 ;;
esac
