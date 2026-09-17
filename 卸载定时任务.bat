@echo off
rem 卸载沸点自动抓取的 Windows 计划任务
chcp 65001 >nul
schtasks /delete /tn "JuejinPinsScrape" /f
echo 已卸载定时抓取任务。
pause
