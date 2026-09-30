@echo off
setlocal enabledelayedexpansion
REM ===========================================================================
REM  Unattended daily run: yesterday's clips -> filtered -> video -> YouTube.
REM  Called by Windows Task Scheduler (see setup_schedule.bat) as
REM      run_daily_auto.bat [HH:MM]      the time the task is scheduled for
REM
REM  Missed nights: the task starts as soon as the PC is back on. If that is more
REM  than LATE_MINUTES after HH:MM and someone is at the PC, a window asks "run
REM  now, or tonight?" (no answer = run). Every date since the last finished one
REM  is then rendered, oldest first (schedule.catch_up_days in config.yaml), and
REM  their uploads are spaced upload.min_gap_hours apart.
REM
REM  Logs to data\logs\auto_YYYY-MM-DD.log. One retry per date after 10 min if it
REM  fails (Ollama still starting, network hiccup, API rate limit...) - the
REM  pipeline resumes from its caches, so the retry only redoes what's missing.
REM
REM  DON'T EDIT THE SETTINGS BELOW. Copy auto_run.local.cmd.example to
REM  auto_run.local.cmd and put your changes there: that file is gitignored, so
REM  your machine's choices survive a `git pull` instead of becoming a conflict.
REM  (Same idea as config.local.*.yaml overlays for the pipeline itself.)
REM ===========================================================================

REM ---- defaults ------------------------------------------------------------
REM Config overlay(s) passed to the pipeline, comma-separated. Empty = plain
REM config.yaml, which does NOT upload. Paths must not contain spaces.
set "CONFIG="

REM Sleep the PC when the run finishes. 0 = leave it running (default: nothing
REM should power down a machine you did not ask it to).
set "SLEEP_AFTER=0"

REM Seconds the "going to sleep" window waits for you to cancel it.
set "SLEEP_PROMPT_SECONDS=120"

REM Skip that window and sleep straight away once the session has been idle this
REM long - at 03:00 there is nobody to ask. 0 = always ask.
set "SLEEP_IDLE_MINUTES=5"

REM Seconds to wait before the single retry.
set "RETRY_SECONDS=600"

REM A start this many minutes after the scheduled time counts as a missed run
REM and asks first (only when someone is at the PC). LATE_PROMPT_SECONDS = how
REM long that window waits before running anyway.
set "LATE_MINUTES=60"
set "LATE_PROMPT_SECONDS=90"

cd /d "%~dp0"
REM The ".\" is load-bearing: `call "auto_run.local.cmd"` makes cmd search PATH for
REM a command by that name rather than running the file next to this one.
if exist ".\auto_run.local.cmd" call ".\auto_run.local.cmd"

if not exist data\logs mkdir data\logs

REM datestamp for the log file (locale-independent via PowerShell)
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i
set "LOG=data\logs\auto_%TODAY%.log"

set "CFGARG="
if defined CONFIG set "CFGARG=--config %CONFIG%"

echo ==== run started %DATE% %TIME% ==== >> "%LOG%"

REM ---- late start? ask first (scripts\missed_run_prompt.ps1) ----------------
set "SCHEDULED_AT=%~1"
if defined SCHEDULED_AT (
    powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\missed_run_prompt.ps1" ^
        -ScheduledAt %SCHEDULED_AT% -LateMinutes %LATE_MINUTES% ^
        -Seconds %LATE_PROMPT_SECONDS% -IdleMinutes %SLEEP_IDLE_MINUTES% >> "%LOG%" 2>&1
    if !ERRORLEVEL! EQU 2 (
        echo ==== postponed to the next scheduled run %DATE% %TIME% ==== >> "%LOG%"
        exit /b 0
    )
)

REM ---- which dates: yesterday plus any missed nights (pipeline\catchup.py) ---
set "DATES="
"%~dp0venv\Scripts\python.exe" -m pipeline.catchup %CFGARG% > "data\logs\catchup_dates.txt" 2>> "%LOG%"
for /f "usebackq" %%d in (`findstr /r "^20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]$" "data\logs\catchup_dates.txt"`) do set "DATES=!DATES! %%d"
del "data\logs\catchup_dates.txt" > NUL 2>&1
if not defined DATES echo ==== nothing to do - yesterday is already finished ==== >> "%LOG%"
if defined DATES echo ==== dates:!DATES! ==== >> "%LOG%"

set "EXITCODE=0"
for %%d in (!DATES!) do (
    set "PYARGS=-m pipeline.run_daily --date %%d %CFGARG%"
    call :run
    if !ERRORLEVEL! NEQ 0 (
        echo ==== %%d failed, retrying in %RETRY_SECONDS%s ==== >> "%LOG%"
        timeout /t %RETRY_SECONDS% /nobreak > NUL
        call :run
    )
    if !ERRORLEVEL! NEQ 0 set "EXITCODE=!ERRORLEVEL!"
)
echo ==== run finished %DATE% %TIME% (exit !EXITCODE!) ==== >> "%LOG%"

if "%SLEEP_AFTER%"=="1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\sleep_prompt.ps1" ^
        -Seconds %SLEEP_PROMPT_SECONDS% -IdleMinutes %SLEEP_IDLE_MINUTES% >> "%LOG%" 2>&1
)

exit /b %EXITCODE%

REM ---------------------------------------------------------------------------
:run
REM Run through keep_awake.ps1: a PC woken by a wake timer goes back to sleep
REM after ~2 minutes of "unattended" idle, which would kill the run long before
REM it finishes. The wrapper holds the system-required flag until python exits.
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\keep_awake.ps1" ^
    -Exe "%~dp0venv\Scripts\python.exe" -Arguments "!PYARGS!" >> "%LOG%" 2>&1
exit /b %ERRORLEVEL%
