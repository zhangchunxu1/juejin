@echo off
rem Juejin pins scheduled scrape: hot list 100 pins (list only, --comments 0),
rem comments handled by refresh patrol (last 3 days, full comments+replies), no Excel export.
set MYSQL_PASSWORD=zc123456
"D:\Program File\python.exe" D:\11\juejin\juejin_pins.py --total 100 --comments 0 --refresh-days 3 --no-excel >> D:\11\juejin\data\scrape_task.log 2>&1
