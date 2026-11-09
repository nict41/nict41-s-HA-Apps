#!/usr/bin/env bash
# Run `claude remote-control` (Remote Control server mode) for the life of
# the app, so this Home Assistant box shows as a device in the Claude app
# and new sessions can be started on it from a phone.
#
#   ha-server-remote-control.sh start <workdir>   called at boot; returns at once
#   ha-server-remote-control.sh run <workdir>     the restart loop (inside tmux)
#   ha-server-remote-control.sh args              print the CLI arguments (tests)
#
# The server draws a live status screen, so it runs in a detached tmux
# session named "remote-control" rather than writing to the app log. Attach
# to it from the web terminal with `tmux attach -t remote-control` to see the
# status or the QR code. Start, exit and restart lines go to $LOG_FILE, which
# `start` follows into the app log.
set -u

OPTIONS_FILE=${HA_SERVER_OPTIONS:-/data/options.json}
CREDENTIALS_FILE=${HA_SERVER_CREDENTIALS:-/root/.claude/.credentials.json}
PRELOAD_LIB=${HA_SERVER_PRELOAD:-/usr/local/lib/ha-server/hostname_override.so}
LOG_FILE=${HA_SERVER_RC_LOG:-/tmp/ha-server-remote-control.log}
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

run_loop() {
    local workdir=$1 name started ran backoff=10
    cd "${workdir}" || exit 1
    build_args
    name=$(device_name)

    while true; do
        if [ ! -s "${CREDENTIALS_FILE}" ]; then
            log "[WARN] Remote Control server: Claude Code is not signed in. Open the web terminal, run 'claude' and sign in; retrying in 60s."
            sleep 60
            continue
        fi

        log "[INFO] Remote Control server starting in ${workdir} as '${name:-$(hostname)}' (claude ${ARGS[*]})"
        started=$(date +%s)
        if [ -n "${name}" ] && [ -r "${PRELOAD_LIB}" ]; then
            LD_PRELOAD="${PRELOAD_LIB}" HA_SERVER_HOSTNAME="${name}" claude "${ARGS[@]}"
        else
            claude "${ARGS[@]}"
        fi
        rc=$?
        ran=$(($(date +%s) - started))

        # Back off when it keeps failing quickly (no network, auth error),
        # but come straight back after a long healthy run.
        if [ "${ran}" -ge 300 ]; then backoff=10; fi
        log "[WARN] Remote Control server exited (code ${rc}) after ${ran}s; restarting in ${backoff}s"
        sleep "${backoff}"
        backoff=$((backoff * 2))
        [ "${backoff}" -le 300 ] || backoff=300
    done
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
    args)
        build_args
        printf '%s\n' "${ARGS[@]}"
        ;;
    *)
        echo "usage: $0 start|run <workdir> | args" >&2
        exit 2
        ;;
esac
