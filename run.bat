@echo off
setlocal
cd /d "%~dp0"

py -3 -c "import requests" >nul 2>nul
if errorlevel 1 (
  echo.
  echo Missing Python dependency: requests
  echo Install it once with:
  echo   py -3 -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

py -3 batch_remix.py
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%EXIT_CODE%"=="0" echo Batch finished with errors. See logs\ and the summary above.
pause
exit /b %EXIT_CODE%
