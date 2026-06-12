@echo off
REM Unattended daily run: yesterday's clips -> filtered -> video -> YouTube upload.
REM Called by Windows Task Scheduler (see setup_schedule.bat). No prompts, no pauses.
REM Logs to data\logs\auto_YYYY-MM-DD.log. One retry after 10 min if the run fails
REM (Ollama still starting, network hiccup, API rate limit...) — the pipeline resumes
REM from its caches, so the retry only redoes what's missing.

cd /d %~dp0
if not exist data\logs mkdir data\logs

REM datestamp for the log file (locale-independent via PowerShell)
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i
set LOG=data\logs\auto_%TODAY%.log

echo ==== run started %DATE% %TIME% ==== >> "%LOG%"
venv\Scripts\python.exe -m pipeline.run_daily >> "%LOG%" 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ==== run failed, retrying in 10 min ==== >> "%LOG%"
    timeout /t 600 /nobreak > NUL
    venv\Scripts\python.exe -m pipeline.run_daily >> "%LOG%" 2>&1
)
echo ==== run finished %DATE% %TIME% (exit %ERRORLEVEL%) ==== >> "%LOG%"
