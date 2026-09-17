@echo off
rem 一键启动数据看板: 相对路径, 双击即用
chcp 65001 >nul
cd /d "%~dp0"
start "" http://127.0.0.1:5000
python "%~dp0server\app.py"
pause
