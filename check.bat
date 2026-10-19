@echo off
rem Step 1: decode QR code (if any) and scan the survey WITHOUT submitting.
setlocal
cd /d "%~dp0"
set "VPY=%~dp0.venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo [ERROR] Run setup.bat first.
  pause
  exit /b 1
)

dir /b qrcode\*.png qrcode\*.jpg >nul 2>nul
if not errorlevel 1 (
  echo ==== Decoding QR code ====
  "%VPY%" decode_qr.py --dir qrcode
  echo.
)

"%VPY%" wjx_signup.py --dry-run
echo.
echo Report: logs\report_latest.txt
pause
endlocal
