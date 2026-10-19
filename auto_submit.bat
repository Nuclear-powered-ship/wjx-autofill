@echo off
rem Scheduled entry: wait for the survey to open, then submit if everything
rem can be filled automatically. No pause, safe for Task Scheduler.
setlocal
cd /d "%~dp0"
set "VPY=%~dp0.venv\Scripts\python.exe"
if not exist "%VPY%" exit /b 1
if not exist "wjx_url.txt" exit /b 1

set /p URL=<wjx_url.txt
echo Survey: %URL%
"%VPY%" wjx_signup.py --url "%URL%" --wait-open --submit-now --max-wait 2400
exit /b %ERRORLEVEL%
