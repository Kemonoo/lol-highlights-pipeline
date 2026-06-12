@echo off
REM Remaining test days (2026-06-09 already done + uploaded — not in this list).
REM Each day: fetch -> filter -> judge -> commentary -> TTS -> assemble -> upload.
REM Pauses between days so you can check the video/report before continuing.

cd /d %~dp0

echo ======== 2026-06-10 ========
venv\Scripts\python.exe -m pipeline.run_daily --date 2026-06-10
echo.
echo Check: data\work\2026-06-10\report.html and the uploaded video link above.
pause

echo ======== 2026-06-11 ========
venv\Scripts\python.exe -m pipeline.run_daily --date 2026-06-11
echo.
echo Both days done. Videos in data\output\ and on your channel.
pause
