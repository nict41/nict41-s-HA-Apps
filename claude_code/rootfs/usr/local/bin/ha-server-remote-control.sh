#!/usr/bin/env bash
# Run `claude remote-control` (Remote Control server mode) for the life of
# the app, so this Home Assistant box shows as a device in the Claude app
# and new sessions can be started on it from a phone.
#
#   ha-server-remote-control.sh start <workdir>   called at boot; returns at once
#   ha-server-remote-control.sh run <workdir>     the restart loop (inside tmux)
#   ha-server-remote-control.sh stop              shut the server down cleanly
#   ha-server-remote-control.sh hostname          print the hostname override (tests)
#   ha-server-remote-control.sh args              print the CLI arguments (tests)
#   ha-server-remote-control.sh wait-update       wait for a boot update (tests)
#
# The server draws a live status screen, so it runs in a detached tmux
# session named "remote-control" rather than writing to the app log. Attach
# to it from the web terminal with `tmux attach -t remote-control` to see the
# status or the QR code. Start, exit and restart lines go to $LOG_FILE, which
# `start` follows into the app log.
#
# `stop` runs when the app is stopped. It sends the server SIGTERM and waits
# for it to exit. On SIGTERM the server stops each session's work, keeps its
# environment and marks it offline, so the next start picks the existing
# sessions straight back up. Killed outright instead, the server leaves the
# sessions held by a worker that no longer exists, and messages sent to them
# go unanswered for several minutes until that hold expires.
set -u

OPTIONS_FILE=${HA_SERVER_OPTIONS:-/data/options.json}
CREDENTIALS_FILE=${HA_SERVER_CREDENTIALS:-/root/.claude/.credentials.json}
PRELOAD_LIB=${HA_SERVER_PRELOAD:-/usr/local/lib/ha-server/hostname_override.so}
LOG_FILE=${HA_SERVER_RC_LOG:-/tmp/ha-server-remote-control.log}
UPDATE_MARKER=${HA_SERVER_UPDATE_MARKER:-/tmp/claude-update.running}
PID_FILE=${HA_SERVER_RC_PID:-/tmp/ha-server-remote-control.pid}
STOP_FLAG=${HA_SERVER_RC_STOP:-/tmp/ha-server-remote-control.stopping}
STOP_TIMEOUT=${HA_SERVER_RC_STOP_TIMEOUT:-45}
TMUX_SESSION=remote-control

opt() {
    jq -r "$1" "${OPTIONS_FILE}" 2>/dev/null
}

# Inside tmux, stdout is the tmux pane, so status lines also go to
# $LOG_FILE, which the boot-time `tail` copies into the app log.
log() {
    echo "$*" >> "${LOG_FILE}"
    echo "$*"
}

enabled() {
    [ "$(opt 'if .remote_control_server == true then "true" else "false" end')" = "true" ]
}

device_name() {
    opt '.remote_control_device_name // ""'
}

# The name the server reports as this machine's hostname, which is the
# device name in the Claude app. Empty when the override is off: the server
# then sees the container hostname the Supervisor assigns.
hostname_override() {
    local name
    [ "$(opt 'if .remote_control_hostname_override == false then "off" else "on" end')" = "on" ] || return 0
    name=$(opt '.remote_control_hostname // ""')
    [ -n "${name}" ] || name=$(device_name)
    printf '%s' "${name}"
}

build_args() {
    local spawn capacity mode name
    spawn=$(opt '.remote_control_spawn // "same-dir"')
    case "${spawn}" in same-dir | worktree | session) ;; *) spawn=same-dir ;; esac

    capacity=$(opt '.remote_control_capacity // 8')
    case "${capacity}" in '' | *[!0-9]*) capacity=8 ;; esac
    [ "${capacity}" -ge 1 ] || capacity=1

    mode=$(opt '.remote_control_permission_mode // "default"')
    case "${mode}" in
        default | acceptEdits | plan | auto) ;;
        *)
            # bypassPermissions in particular is never passed through: phone
            # sessions must still meet the permission prompts and the guard.
            log "[WARN] remote_control_permission_mode '${mode}' is not allowed; using 'default'"
            mode=default
            ;;
    esac

    ARGS=(remote-control --spawn "${spawn}" --capacity "${capacity}" --permission-mode "${mode}")
    name=$(device_name)
    if [ -n "${name}" ]; then
        ARGS+=(--remote-control-session-name-prefix "${name}")
    fi
}

# The boot script updates Claude Code in the background and holds
# $UPDATE_MARKER while npm rewrites the CLI. A session spawned in that
# window fails with EACCES on the half-written binary, so the server is not
# started until the update ends (bounded by the update timeout).
wait_for_update() {
    local limit waited=0
    [ -e "${UPDATE_MARKER}" ] || return 0
    limit=$(opt '.claude_update_timeout // 300')
    case "${limit}" in '' | *[!0-9]*) limit=300 ;; esac
    limit=$((limit + 30))
    log "[INFO] Remote Control server waiting for the Claude Code update to finish"
    while [ -e "${UPDATE_MARKER}" ] && [ "${waited}" -lt "${limit}" ]; do
        sleep 2
        waited=$((waited + 2))
    done
    if [ -e "${UPDATE_MARKER}" ]; then
        log "[WARN] Claude Code update still running after ${waited}s; starting the Remote Control server anyway"
    fi
}

stopping() {
    [ -e "${STOP_FLAG}" ]
}

run_loop() {
    local workdir=$1 host started ran backoff=10
    cd "${workdir}" || exit 1
    build_args
    host=$(hostname_override)
    if [ -n "${host}" ] && [ ! -r "${PRELOAD_LIB}" ]; then
        log "[WARN] Hostname override library missing; the device keeps the container hostname"
        host=""
    fi

    while ! stopping; do
        if [ ! -s "${CREDENTIALS_FILE}" ]; then
            log "[WARN] Remote Control server: Claude Code is not signed in. Open the web terminal, run 'claude' and sign in; retrying in 60s."
            sleep 60
            continue
        fi

        wait_for_update
        stopping && break
        log "[INFO] Remote Control server starting in ${workdir} as '${host:-$(hostname)}' (claude ${ARGS[*]})"
        started=$(date +%s)
        # Run in the foreground so the server keeps the tmux pane as its
        # terminal; the inner shell records its PID and then becomes claude,
        # so `stop` can signal the server itself.
        if [ -n "${host}" ]; then
            LD_PRELOAD="${PRELOAD_LIB}" HA_SERVER_HOSTNAME="${host}" \
                bash -c 'echo $$ > "$0"; exec claude "$@"' "${PID_FILE}" "${ARGS[@]}"
        else
            bash -c 'echo $$ > "$0"; exec claude "$@"' "${PID_FILE}" "${ARGS[@]}"
        fi
        rc=$?
        rm -f "${PID_FILE}"
        ran=$(($(date +%s) - started))
        if stopping; then
            log "[INFO] Remote Control server stopped (code ${rc}); existing sessions reconnect on the next start"
            break
        fi

        # Back off when it keeps failing quickly (no network, auth error),
        # but come straight back after a long healthy run.
        if [ "${ran}" -ge 300 ]; then backoff=10; fi
        log "[WARN] Remote Control server exited (code ${rc}) after ${ran}s; restarting in ${backoff}s"
        sleep "${backoff}"
        backoff=$((backoff * 2))
        [ "${backoff}" -le 300 ] || backoff=300
    done
}

stop_server() {
    local pid waited=0
    touch "${STOP_FLAG}"
    pid=$(cat "${PID_FILE}" 2>/dev/null)
    case "${pid}" in '' | *[!0-9]*) return 0 ;; esac
    kill -0 "${pid}" 2>/dev/null || return 0
    log "[INFO] Stopping the Remote Control server so its sessions can reconnect after the restart"
    kill -TERM "${pid}" 2>/dev/null
    while kill -0 "${pid}" 2>/dev/null && [ "${waited}" -lt "${STOP_TIMEOUT}" ]; do
        sleep 1
        waited=$((waited + 1))
    done
    if kill -0 "${pid}" 2>/dev/null; then
        log "[WARN] Remote Control server still running after ${waited}s; leaving it to be killed"
        return 1
    fi
    return 0
}

case "${1:-}" in
    start)
        workdir=${2:-/homeassistant}
        if ! enabled; then
            echo "[INFO] Remote Control server disabled (remote_control_server: false)"
            exit 0
        fi
        if tmux has-session -t "${TMUX_SESSION}" 2>/dev/null; then
            echo "[INFO] Remote Control server already running in tmux session '${TMUX_SESSION}'"
            exit 0
        fi
        : > "${LOG_FILE}"
        rm -f "${STOP_FLAG}" "${PID_FILE}"
        # Follow the loop's status lines into the app log. Started before
        # the boot script execs ttyd, so it keeps the container's stdout.
        tail -n +1 -F "${LOG_FILE}" 2>/dev/null &
        tmux new-session -d -s "${TMUX_SESSION}" -x 120 -y 30 \
            "bash $(readlink -f "$0") run '${workdir}'"
        echo "[INFO] Remote Control server launched in tmux session '${TMUX_SESSION}' (attach: tmux attach -t ${TMUX_SESSION})"
        ;;
    run)
        run_loop "${2:-/homeassistant}"
        ;;
    stop)
        stop_server
        ;;
    hostname)
        hostname_override
        echo
        ;;
    args)
        build_args
        printf '%s\n' "${ARGS[@]}"
        ;;
    wait-update)
        wait_for_update
        ;;
    *)
        echo "usage: $0 start|run <workdir> | stop | args | hostname" >&2
        exit 2
        ;;
esac
