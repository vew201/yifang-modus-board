@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if exist "..\..\work\test-runtime\playwright" set "PYTHONPATH=%~dp0..\..\work\test-runtime"
py -3 tests/run_all.py --browser --output "..\..\work\test-results-all"
pause
