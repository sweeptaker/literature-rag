@echo off
REM ===========================================================================
REM  ragmini - Windows step 4/6 : ask a question (search + generate)
REM  Tip: set DEEPSEEK_API_KEY first to get generated answers.
REM ===========================================================================
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."

echo ============================================================
echo  Step 4 - ask a question
echo ============================================================
echo.

if "%~1"=="" (
  set /p Q=Type your question: 
) else (
  set Q=%*
)

if "%Q%"=="" (
  echo [SKIP] empty question
  pause
  exit /b 0
)

python run.py ask "%Q%"
echo.
pause
exit /b 0
