@echo off
setlocal enabledelayedexpansion
REM ===========================================================================
REM  One-time setup: registers a Windows Task Scheduler job for the daily run.
REM
REM      setup_schedule.bat            registers it for 03:00
REM      setup_schedule.bat 05:30      ...or whatever time you want (24h HH:MM)
REM
REM  To remove later:
REM      schtasks /delete /tn "LoL Daily Highlights" /f
REM ===========================================================================

cd /d "%~dp0"

set "RUNTIME=%~1"
if "%RUNTIME%"=="" set "RUNTIME=03:00"

REM Read back what the run itself is configured to do, so the summary below
REM describes reality rather than the defaults.
set "CONFIG="
set "SLEEP_AFTER=0"
set "SLEEP_PROMPT_SECONDS=120"
set "SLEEP_IDLE_MINUTES=5"
if exist ".\auto_run.local.cmd" call ".\auto_run.local.cmd"

echo.
echo  Registering "LoL Daily Highlights" to run every day at %RUNTIME%.
echo.

schtasks /create ^
  /tn "LoL Daily Highlights" ^
  /tr "cmd.exe /c \"\"%~dp0run_daily_auto.bat\"\"" ^
  /sc daily ^
  /st %RUNTIME% ^
  /ru "%USERNAME%" ^
  /f

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  Failed to register - try running this file as administrator.
    pause
    exit /b 1
)

REM WakeToRun is what lets the job start on a sleeping PC. schtasks cannot set it,
REM hence the round-trip through the ScheduledTasks module.
powershell -NoProfile -Command ^
  "$t = Get-ScheduledTask -TaskName 'LoL Daily Highlights';" ^
  "$t.Settings.WakeToRun = $true; $t.Settings.Hidden = $true;" ^
  "Set-ScheduledTask -InputObject $t" >NUL 2>&1
if %ERRORLEVEL% NEQ 0 echo  NOTE: could not set wake-to-run; the PC must be awake at %RUNTIME%.

echo.
echo  ---------------------------------------------------------------------
echo   What this will do, every day at %RUNTIME%:
echo.
echo    - wake the PC if it is asleep (wake timers must be allowed in the
echo      active power plan: powercfg /q ^| findstr /i "wake")
echo    - stay awake for the whole run - a few hours on a cold start
if defined CONFIG (
    echo    - run the pipeline with:  --config %CONFIG%
) else (
    echo    - run the pipeline with plain config.yaml, which RENDERS BUT DOES
    echo      NOT UPLOAD. Set CONFIG in auto_run.local.cmd to change that.
)
if "%SLEEP_AFTER%"=="1" (
    echo    - PUT THE PC TO SLEEP afterwards, after a %SLEEP_PROMPT_SECONDS%s
    echo      countdown window you can cancel. If the session has been idle
    echo      %SLEEP_IDLE_MINUTES% min it sleeps without asking.
) else (
    echo    - leave the PC running afterwards. Set SLEEP_AFTER=1 in
    echo      auto_run.local.cmd to have it sleep instead.
)
echo.
echo   Settings live in auto_run.local.cmd
echo   ^(copy auto_run.local.cmd.example to create it^).
echo   Logs land in data\logs\.
echo  ---------------------------------------------------------------------
echo.
echo  Test it right now:  schtasks /run /tn "LoL Daily Highlights"
echo.
pause
