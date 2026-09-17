@echo off
rem 环境安装（新电脑第一次用跑这个）: 装依赖 + 浏览器内核
chcp 65001 >nul
echo ==== 掘金工具集 环境安装 ====
echo 1/2 安装 Python 依赖...
python -m pip install -r "%~dp0requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
echo 2/2 下载浏览器内核（签到/登录用, 已装会自动跳过）...
python -m playwright install chromium
echo.
echo 安装完成! 请确认 config.json 里已填 MySQL 密码, 然后运行 一键启动看板.bat
pause
