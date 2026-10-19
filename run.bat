@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>nul
cd /d "%~dp0"

rem ??: ??????? "? ASCII + CRLF".
rem ???? .bat ?? cmd ????????, ???????, ??????.

set "PY="
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
  where python3 >nul 2>nul
  if not errorlevel 1 set "PY=python3"
)
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)

if not defined PY (
  echo.
  echo [ERROR] Python not found in PATH.
  echo         Install Python 3.8+ from https://www.python.org/downloads/
  echo         and tick "Add python.exe to PATH" during setup.
  echo.
  pause
  exit /b 1
)

rem ????????? ^(Windows Store ?? python.exe ???????^)
%PY% -c "import sys;print(sys.version_info[0])" >"%TEMP%\api_tester_pycheck.txt" 2>nul
if errorlevel 1 (
  echo.
  echo [ERROR] "%PY%" exists but cannot run Python.
  echo         If it is the Microsoft Store stub, turn off
  echo         "App execution aliases" for python.exe or install real Python.
  echo.
  pause
  exit /b 1
)
for /f "usebackq delims=" %%v in ("%TEMP%\api_tester_pycheck.txt") do set "PYVER=%%v"
if not "%PYVER%"=="3" (
  echo.
  echo [ERROR] Python 3 is required, but this interpreter reports Python %PYVER%.
  echo.
  pause
  exit /b 1
)
del "%TEMP%\api_tester_pycheck.txt" >nul 2>nul

echo ============================================
echo   Local API Tester  -  starting...
echo ============================================
echo.

if "%~1"=="" (
  %PY% api_tester.py --gui
) else (
  %PY% api_tester.py %*
)
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo [exit code %RC%]
  pause
)
exit /b %RC%
