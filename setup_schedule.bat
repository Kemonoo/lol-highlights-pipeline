@echo off
REM One-time setup: registers a Windows Task Scheduler job that runs the pipeline
REM every day at 06:00 (edit /st below to change the time).
REM Run this once by double-clicking. To remove later:
REM     schtasks /delete /tn "LoL Daily Highlights" /f

schtasks /create ^
  /tn "LoL Daily Highlights" ^
  /tr "cmd.exe /c \"\"%~dp0run_daily_auto.bat\"\"" ^
  /sc daily ^
  /st 03:00 ^
  /ru "%USERNAME%" ^
  /f

if %ERRORLEVEL% EQU 0 (
    powershell -NoProfile -Command "$t = Get-ScheduledTask -TaskName 'LoL Daily Highlights'; $t.Settings.WakeToRun = $true; $t.Settings.Hidden = $true; Set-ScheduledTask -InputObject $t" >NUL 2>&1
    echo.
    echo Scheduled: every day at 03:00 -^> run_daily_auto.bat
    echo Test it right now with:  schtasks /run /tn "LoL Daily Highlights"
    echo Logs land in data\logs\ ; upload behavior follows upload.privacy in config.yaml.
) else (
    echo.
    echo Failed to register — try running this file as administrator.
)
pause
