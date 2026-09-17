@echo off
rem 安装 Windows 计划任务: 每 30 分钟自动抓取一次沸点榜单
chcp 65001 >nul
schtasks /create /tn "JuejinPinsScrape" /tr "\"%~dp0run_scrape.bat\"" /sc minute /mo 30 /f
echo.
echo 已安装: 每 30 分钟自动抓取一次（任务名 JuejinPinsScrape）
echo 卸载请运行: 卸载定时任务.bat
pause
