@echo off
rem 掘金沸点定时抓取（计划任务入口）: 相对路径版, 项目移动/改名都不受影响
rem 密码从 config.json 读取, 本文件不再包含密码
cd /d "%~dp0"
"%~dp0auto_scrape.py" %* >> "%~dp0data\scrape_task.log" 2>&1
