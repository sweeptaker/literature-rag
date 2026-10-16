@echo off
REM ===========================================================================
REM  ragmini - Windows : start the web demo (http://127.0.0.1:8765)
REM  Keeps a console window running the server; close it to stop.
REM ===========================================================================
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."

echo ============================================================
echo  Web demo - starting server on http://127.0.0.1:8765
echo  First start loads the model, please wait 20-60 seconds.
echo  Close the new window (or press Ctrl-C) to stop the server.
echo ============================================================
echo.

start "ragmini-web" cmd /k python run.py serve %*

echo Waiting for the server to become ready ...
timeout /t 30 /nobreak >nul
start "" http://127.0.0.1:8765/
exit /b 0
