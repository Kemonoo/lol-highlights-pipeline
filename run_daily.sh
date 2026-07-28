#!/usr/bin/env bash
# Unattended daily run (macOS / Linux). POSIX counterpart to run_daily_auto.bat.
#
#   crontab -e
#   0 3 * * *  cd /path/to/lol-highlights-pipeline && ./run_daily.sh
#
# One retry after 10 minutes if the run fails — covers Ollama still starting, a network
# blip, or a rate limit. Per-stage caches mean the retry only redoes what's missing.
#
# Settings come from the environment, or from auto_run.local.sh if you create one
# (gitignored, so `git pull` stays clean):
#
#   CONFIG=config.mychannel.yaml   pipeline overlay(s); empty renders but does NOT upload
#   SLEEP_AFTER=1                  suspend the machine when the run finishes (default 0)
#   SLEEP_PROMPT_SECONDS=120       countdown before it does; Ctrl-C or any key cancels
#   RETRY_SECONDS=600              wait before the single retry
#
# NB: cron does not wake a sleeping machine. To have the box wake itself, use
# `rtcwake` (Linux) or `pmset repeat wakeorpoweron` (macOS) — see docs/setup.md.
set -uo pipefail

cd "$(dirname "$0")"
[ -f auto_run.local.sh ] && . ./auto_run.local.sh

PYTHON="${PYTHON:-venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3 || command -v python)"

CONFIG="${CONFIG:-}"
SLEEP_AFTER="${SLEEP_AFTER:-0}"
SLEEP_PROMPT_SECONDS="${SLEEP_PROMPT_SECONDS:-120}"
RETRY_SECONDS="${RETRY_SECONDS:-600}"

ARGS=()
[ -n "$CONFIG" ] && ARGS+=(--config "$CONFIG")

# Keep the machine awake for the duration, the same reason keep_awake.ps1 exists on
# Windows: an idle-suspend halfway through leaves a half-rendered day behind.
KEEP_AWAKE=()
if command -v systemd-inhibit >/dev/null 2>&1; then
    KEEP_AWAKE=(systemd-inhibit --what=idle:sleep --why="daily highlights render")
elif command -v caffeinate >/dev/null 2>&1; then
    KEEP_AWAKE=(caffeinate -s)
fi

mkdir -p data/logs
LOG="data/logs/auto_$(date +%F).log"

# ${a[@]+"${a[@]}"} rather than "${a[@]}": under `set -u`, bash before 4.4 treats an
# empty array as unbound and aborts the script.
run_once() {
    ${KEEP_AWAKE[@]+"${KEEP_AWAKE[@]}"} "$PYTHON" -m pipeline.run_daily \
        ${ARGS[@]+"${ARGS[@]}"} ${EXTRA[@]+"${EXTRA[@]}"}
}

EXTRA=("$@")

{
    echo "==== run started $(date) ===="
    run_once
    EXITCODE=$?
    if [ "$EXITCODE" -ne 0 ]; then
        echo "==== run failed, retrying in ${RETRY_SECONDS}s ===="
        sleep "$RETRY_SECONDS"
        run_once
        EXITCODE=$?
    fi
    echo "==== run finished $(date) (exit $EXITCODE) ===="
} >> "$LOG" 2>&1

if [ "$SLEEP_AFTER" = "1" ]; then
    # No GUI prompt here — a terminal countdown that any keypress cancels. On a
    # headless box nobody is watching, which is the same as not answering: it sleeps.
    echo "Run finished. Suspending in ${SLEEP_PROMPT_SECONDS}s — press any key to cancel."
    if read -r -n 1 -t "$SLEEP_PROMPT_SECONDS"; then
        echo "Cancelled — staying awake." | tee -a "$LOG"
    elif command -v systemctl >/dev/null 2>&1; then
        echo "Suspending." | tee -a "$LOG"; systemctl suspend
    elif command -v pmset >/dev/null 2>&1; then
        echo "Suspending." | tee -a "$LOG"; pmset sleepnow
    else
        echo "No suspend command found (systemctl/pmset) — staying awake." | tee -a "$LOG"
    fi
fi

exit "${EXITCODE:-0}"
