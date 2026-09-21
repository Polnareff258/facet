@echo off
REM ============================================================
REM  youyoumonitor - one-click launcher for Windows
REM  (double-click this file, or run from a terminal)
REM
REM  Usage:
REM    start.cmd                 prepare env, then start collecting + dashboard
REM    start.cmd --setup         prepare environment only
REM    start.cmd --check         run the environment self-check
REM    start.cmd --daemon        run in background
REM    start.cmd --stop          stop the background instance
REM    start.cmd --status        show background instance status
REM
REM  NOTE ON ENCODING -- this file is intentionally ASCII-ONLY.
REM  cmd.exe reads .cmd files using the system OEM code page (936 on
REM  Chinese Windows). A UTF-8 Chinese comment gets mis-decoded, and a
REM  stray byte pair can swallow the line break, merging two lines into
REM  one. The result is a cascade of "'xxx' is not recognized as an
REM  internal or external command" errors that have nothing to do with
REM  the actual logic. All Chinese output is produced by bootstrap.py
REM  instead, where Python handles the encoding properly.
REM ============================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "PYEXE="
set "PYVER="

REM --- 1) Reuse an existing virtualenv (fastest) ---
if exist ".venv\Scripts\python.exe" (
    set "PYEXE=%CD%\.venv\Scripts\python.exe"
    goto :run
)

REM --- 2) Use the py launcher to pick a suitable 3.x ---
where py >nul 2>nul
if not errorlevel 1 (
    for %%V in (3.13 3.12 3.11 3.10) do (
        if not defined PYEXE (
            py -%%V -c "import sys" >nul 2>nul
            if not errorlevel 1 (
                set "PYEXE=py"
                set "PYVER=-%%V"
            )
        )
    )
)

REM --- 3) Fall back to python on PATH ---
if not defined PYEXE (
    where python >nul 2>nul
    if not errorlevel 1 set "PYEXE=python"
)

if not defined PYEXE goto :nopython

:run
"%PYEXE%" %PYVER% bootstrap.py %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo   Launcher exited with code %RC%.
    echo   Fix whatever is reported above, then double-click start.cmd again.
    echo.
    pause
)

endlocal & exit /b %RC%

:nopython
echo.
echo   [X] Python 3.10 or newer was not found.
echo.
echo   Install one of these ways:
echo     1^) Microsoft Store - search for "Python 3.12" and install
echo     2^) https://www.python.org/downloads/ - during setup, be sure to
echo        tick "Add python.exe to PATH"
echo.
echo   Then close this window and double-click start.cmd again.
echo.
pause
endlocal & exit /b 2
