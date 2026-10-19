@echo off
rem Step 3: generate Task Scheduler XML from the registration time.
setlocal
cd /d "%~dp0"
set "VPY=%~dp0.venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo [ERROR] Run setup.bat first.
  pause
  exit /b 1
)
"%VPY%" make_task.py %*
echo.
echo Next: Win+R - taskschd.msc - Import Task - pick wjx_task.xml
pause
endlocal
