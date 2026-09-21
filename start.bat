@echo off
REM Double-clickable launcher for Windows: runs start.ps1 with the execution
REM policy bypassed, so PowerShell's default script-blocking doesn't get in the
REM way. Pass -NoSetup to skip the dependency install on later runs.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
