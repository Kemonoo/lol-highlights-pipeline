@echo off
REM Livestream the finished daily videos back to back (pipeline\tools\stream.py).
REM Needs YT_STREAM_KEY in .env. Close this window to stop the stream.
REM   stream.bat --dry-run      shows the plan without going live
REM   stream.bat --hours 2      a shorter stream
cd /d "%~dp0"
set "CONFIG="
if exist "%~dp0auto_run.local.cmd" call "%~dp0auto_run.local.cmd"
set "CFGARG="
if defined CONFIG set "CFGARG=--config %CONFIG%"
"%~dp0venv\Scripts\python.exe" -m pipeline.tools.stream %CFGARG% %*
