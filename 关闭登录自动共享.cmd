@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 share.py --remove-startup
pause
