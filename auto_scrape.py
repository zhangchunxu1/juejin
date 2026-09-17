# -*- coding: utf-8 -*-
"""
定时任务/命令行用的静默抓取入口。
自动从 config.json 读 MySQL 密码（也可用环境变量覆盖），无需在 bat 里写密码。

用法:
    python auto_scrape.py                      # 默认: 100 条沸点, 不抓评论
    python auto_scrape.py --total 200 --comments -1
"""
import subprocess
import sys
import os

PROJ_DIR = os.path.dirname(os.path.abspath(__file__))

# Windows 控制台 GBK 兼容
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if __name__ == "__main__":
    args = sys.argv[1:]
    if "--total" not in args:
        args += ["--total", "100"]
    if "--comments" not in args:
        args += ["--comments", "0"]
    args += ["--no-excel"]  # 定时任务不导 Excel, 防文件堆积

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    rc = subprocess.call([sys.executable, os.path.join(PROJ_DIR, "juejin_pins.py")] + args, env=env)
    sys.exit(rc)
