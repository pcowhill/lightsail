#!/usr/bin/env bash
# Test double for /usr/local/sbin/cowhill-lightsail-demo-service.
#
# Emulates what systemd + the real helper do for lightsail-demo.service, in a
# sandbox: "restart" (re)starts current/.venv/bin/python current/main.py with
# the contract environment (but a test port), honouring the unit's
# ConditionPathExists rules (no ready release => clean skip, exit 3); "stop"
# terminates it; "status" prints the same property names the real helper does.
# Never used outside the test suite.
set -Eeuo pipefail

root="${SANDBOX_APP_ROOT:?}"
state="${SANDBOX_STATE_DIR:?}"
port="${SANDBOX_PORT:?}"
current="${root}/current"
pidfile="${state}/main.pid"
logfile="${state}/service.log"
mkdir -p "${state}"

running_pid() {
  local pid
  [[ -f "${pidfile}" ]] || return 1
  pid="$(cat "${pidfile}")"
  [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null && printf '%s' "${pid}"
}

stop_unit() {
  local pid
  if pid="$(running_pid)"; then
    kill -TERM "${pid}" 2>/dev/null || true
    for _ in $(seq 1 50); do kill -0 "${pid}" 2>/dev/null || break; sleep 0.1; done
    kill -KILL "${pid}" 2>/dev/null || true
  fi
  rm -f "${pidfile}"
}

conditions_ok() {
  [[ -f "${current}/.release-ready" && -x "${current}/.venv/bin/python" && -f "${current}/main.py" ]]
}

case "${1:-}" in
  restart)
    echo "restart" >> "${state}/helper.calls"
    stop_unit
    rm -f "${state}/failed"
    if ! conditions_ok; then
      echo "lightsail-demo.service: start skipped, no ready release (ConditionResult=no)"
      exit 3
    fi
    (
      cd "${current}"
      HOST=127.0.0.1 PORT="${port}" SERVE_STATIC=0 ALLOWED_ORIGINS="http://127.0.0.1:${port}" \
      PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
        exec "${current}/.venv/bin/python" "${current}/main.py"
    ) >> "${logfile}" 2>&1 &
    echo $! > "${pidfile}"
    sleep 0.5
    if running_pid >/dev/null; then
      echo "lightsail-demo.service: active (restart requested)"
      exit 0
    fi
    touch "${state}/failed"
    echo "lightsail-demo.service: not active after restart (ActiveState=failed)" >&2
    exit 1
    ;;
  stop)
    echo "stop" >> "${state}/helper.calls"
    stop_unit
    echo "lightsail-demo.service: inactive"
    ;;
  status)
    echo "status" >> "${state}/helper.calls"
    if pid="$(running_pid)"; then active=active; sub=running; else active=inactive; sub=dead; pid=0; fi
    [[ -f "${state}/failed" && "${active}" == "inactive" ]] && { active=failed; sub=failed; }
    if conditions_ok; then cond=yes; else cond=no; fi
    printf 'Id=lightsail-demo.service\nLoadState=loaded\nActiveState=%s\nSubState=%s\nUnitFileState=enabled\nResult=success\nConditionResult=%s\nMainPID=%s\nNRestarts=0\n' \
      "${active}" "${sub}" "${cond}" "${pid}"
    ;;
  *)
    echo "usage: fake-helper restart|stop|status" >&2
    exit 64
    ;;
esac
