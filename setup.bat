@echo off
rem One-click environment setup for Windows 10/11.
rem Creates an isolated .venv and installs dependencies. No system changes.
setlocal
cd /d "%~dp0"

set "VPY=%~dp0.venv\Scripts\python.exe"
if exist "%VPY%" (
  "%VPY%" setup_env.py %*
  goto done
)

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found.
  echo Install Python 3.8+ from https://www.python.org/downloads/windows/
  echo IMPORTANT: tick "Add python.exe to PATH" during install.
  pause
  exit /b 1
)
python setup_env.py %*

:done
echo.
pause
endlocal
