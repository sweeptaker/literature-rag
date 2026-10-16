@echo off
REM ===========================================================================
REM  ragmini - Windows step 1/6 : install environment
REM  Double-click this file. Message text is ASCII only on purpose
REM  (cmd.exe code pages can garble non-ASCII script content);
REM  detailed Chinese output is printed by Python.
REM ===========================================================================
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."

echo ============================================================
echo  ragmini - install environment
echo  (create .venv, install packages, download models)
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 goto nopython

python install.py %*
echo.
echo [DONE] Next: run 2_ingest.bat  (or see README_Windows.md)
pause
exit /b 0

:nopython
echo [ERROR] Python not found in PATH.
echo.
echo  1. Download Python 3.11 or 3.12 from https://www.python.org/downloads/
echo  2. During setup CHECK the box "Add python.exe to PATH"
echo  3. Re-open this window and double-click this file again
echo.
pause
exit /b 1
