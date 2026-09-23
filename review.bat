@echo off
REM Review a few of the filter's decisions per night: opens http://localhost:8766.
REM Reviews are saved to data\reviews\reviews.jsonl. Close this window to stop.
REM   review.bat --stats   prints the per-gate results instead.
REM Uses the same CONFIG overlay as the nightly run (auto_run.local.cmd), so the
REM blacklist and language rules shown match what the pipeline actually applied.
cd /d "%~dp0"
set "CONFIG="
if exist "%~dp0auto_run.local.cmd" call "%~dp0auto_run.local.cmd"
set "CFGARG="
if defined CONFIG set "CFGARG=--config %CONFIG%"
"%~dp0venv\Scripts\python.exe" -m pipeline.tools.review_queue %CFGARG% %*
