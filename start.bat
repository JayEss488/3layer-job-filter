@echo off
REM Double-clickable launcher: runs start.ps1 with execution policy bypassed
REM so PowerShell's default script-blocking doesn't get in the way.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
