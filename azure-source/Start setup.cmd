@echo off
setlocal
cd /d "%~dp0"
set "CLIENT_PYTHON="
set "CLIENT_PY_ARGS="
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; assert sys.version_info >= (3,11)" >nul 2>nul
  if not errorlevel 1 (
    set "CLIENT_PYTHON=py"
    set "CLIENT_PY_ARGS=-3"
  )
)
if not defined CLIENT_PYTHON if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" set "CLIENT_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not defined CLIENT_PYTHON set "CLIENT_PYTHON=python"
"%CLIENT_PYTHON%" %CLIENT_PY_ARGS% -c "import sys; assert sys.version_info >= (3,11)" >nul 2>nul
if errorlevel 1 (
  echo Python 3.11 ya newer install karein: https://www.python.org/downloads/windows/
  echo Installation mein Add Python to PATH select karein, phir yeh file dobara kholein.
  pause
  exit /b 1
)
echo Opening DriveCast local control. Keep this window open.
"%CLIENT_PYTHON%" %CLIENT_PY_ARGS% bootstrap.py --chrome
if errorlevel 1 goto failed
exit /b 0
:failed
echo Setup complete nahi hua. Upar error check karein, phir dobara try karein.
pause
exit /b 1
