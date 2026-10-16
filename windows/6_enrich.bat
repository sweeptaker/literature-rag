@echo off
REM ===========================================================================
REM  ragmini - Windows extra step : enrich journal metrics (JCR / JIF -> tier)
REM  NOTE: "2_ingest.bat" already runs this automatically on success.
REM        Run this file only to re-run it manually, e.g. after editing
REM        journal_metrics.yaml.  Use --no-llm to skip the AI fallback.
REM ===========================================================================
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."

echo ============================================================
echo  Extra - enrich journal metrics (JCR quartile / JIF -^> tier 1-6)
echo ============================================================
echo.

python run.py enrich %*

echo.
echo [DONE] Tier / JIF / quartile written to the journals table.
pause
exit /b 0
