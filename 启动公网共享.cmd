@echo off
chcp 65001 >nul
cd /d "%~dp0"
pyw -3 share.py --open
