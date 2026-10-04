@echo off
setlocal enableextensions
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"%~dp0tongle.exe" guide %*
if errorlevel 1 (
    echo Tongle failed to start. See the message above for details.
    pause
)
endlocal
