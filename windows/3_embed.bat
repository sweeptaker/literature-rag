@echo off
REM ===========================================================================
REM  ragmini - Windows step 3/6 : build embeddings (chunks -> bge-m3 vectors)
REM ===========================================================================
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."

echo ============================================================
echo  Step 3 - build embeddings
echo ============================================================
echo.

python run.py embed %*

echo.
echo [DONE] Next: run 4_ask.bat  (ask questions)
pause
exit /b 0
