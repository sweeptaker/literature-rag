@echo off
REM ===========================================================================
REM  ragmini - Windows step 2/6 : ingest PDFs into SQLite
REM  Usage: drag a PDF folder onto this file, or run it directly to use .\pdf
REM  Note : journal metrics (JCR quartile / JIF -> tier) are enriched
REM         automatically at the end of a successful ingest.
REM ===========================================================================
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."

echo ============================================================
echo  Step 2 - ingest PDFs  (PDF -^> sections -^> chunks -^> SQLite)
echo  Journal metrics (JCR quartile / JIF -^> tier) are enriched
echo  automatically when the ingest finishes.
echo ============================================================
echo.

if "%~1"=="" (
  echo No folder given, using default: pdf\
  python run.py ingest
) else (
  python run.py ingest %*
)

echo.
echo [DONE] Next: run 3_embed.bat  (generate vectors)
pause
exit /b 0
