@echo off
setlocal enabledelayedexpansion
REM ===========================================================================
REM  Unattended daily run: yesterday's clips -> filtered -> video -> YouTube.
REM  Called by Windows Task Scheduler (see setup_schedule.bat). No prompts.
REM
REM  Logs to data\logs\auto_YYYY-MM-DD.log. One retry after 10 min if the run
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

cd /d "%~dp0"
REM The ".\" is load-bearing: `call "auto_run.local.cmd"` makes cmd search PATH for
REM a command by that name rather than running the file next to this one.
if exist ".\auto_run.local.cmd" call ".\auto_run.local.cmd"

if not exist data\logs mkdir data\logs

REM datestamp for the log file (locale-independent via PowerShell)
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i
set "LOG=data\logs\auto_%TODAY%.log"

set "PYARGS=-m pipeline.run_daily"
if defined CONFIG set "PYARGS=%PYARGS% --config %CONFIG%"

echo ==== run started %DATE% %TIME% ==== >> "%LOG%"
call :run
if !ERRORLEVEL! NEQ 0 (
    echo ==== run failed, retrying in %RETRY_SECONDS%s ==== >> "%LOG%"
    timeout /t %RETRY_SECONDS% /nobreak > NUL
    call :run
)
set "EXITCODE=!ERRORLEVEL!"
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
    -Exe "%~dp0venv\Scripts\python.exe" -Arguments "%PYARGS%" >> "%LOG%" 2>&1
exit /b %ERRORLEVEL%
