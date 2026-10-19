@echo off
rem Step 2: fill and submit NOW. Stops if any question cannot be auto-filled.
setlocal
cd /d "%~dp0"
set "VPY=%~dp0.venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo [ERROR] Run setup.bat first.
  pause
  exit /b 1
)
"%VPY%" wjx_signup.py --submit-now
echo.
echo Exit code: %ERRORLEVEL%
echo Report: logs\report_latest.txt
pause
endlocal
