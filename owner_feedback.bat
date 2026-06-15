@echo off
cd /d %~dp0
venv\Scripts\python.exe -m pipeline.feedback.owner_feedback --open
if %ERRORLEVEL% NEQ 0 pause
