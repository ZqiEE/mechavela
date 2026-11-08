@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 launch.py %*
if errorlevel 1 pause
exit /b
:use_python
python launch.py %*
if errorlevel 1 pause
