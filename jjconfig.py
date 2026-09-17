# -*- coding: utf-8 -*-
"""
统一配置模块：所有脚本共用的配置读取入口。

优先级: 环境变量 MYSQL_PASSWORD  >  config.json 的 mysql.password
config.json 不存在时自动生成模板（含提示），改完即可用。

项目内任何脚本这样用:
    from jjconfig import PROJ_DIR, DATA_DIR, AUTH_PATH, get_mysql_conf
"""
import copy
import json
import os
import sys

# 项目根目录 = 本文件所在目录（整个项目无论移到哪、改什么名，这里都自动适应）
PROJ_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PROJ_DIR, "data")
CONFIG_PATH = os.path.join(PROJ_DIR, "config.json")
AUTH_PATH = os.path.join(DATA_DIR, "auth_juejin.json")

TEMPLATE = {
    "mysql": {
        "host": "127.0.0.1",
        "port": 3306,
        "user": "root",
        "password": "",
        "database": "juejin",
    },
    # 网页看板里的定时抓取（秒）。0 = 关闭。
    "scrape_interval": 0,
    # 抓取节奏（网页"高级设置"可改；不配则用脚本默认 workers=5 / page_delay=0.3 / pin_delay=0.5）
    "scrape": {
        "workers": 5,
        "page_delay": 0.3,
        "pin_delay": 0.5,
    },
}


def _ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_config():
    """读 config.json；不存在则按模板生成一份并提示填写密码。"""
    _ensure_dirs()
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(TEMPLATE, f, ensure_ascii=False, indent=2)
        print(f"[配置] 未找到 config.json，已生成模板: {CONFIG_PATH}")
        print("[配置] 请用记事本打开，把 mysql.password 填成你的 MySQL 密码后重新运行")
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        print(f"[配置] config.json 解析失败({e})，先用默认模板")
        cfg = copy.deepcopy(TEMPLATE)
    # 兜底合并缺的键
    base = copy.deepcopy(TEMPLATE)
    for k, v in base.items():
        cfg.setdefault(k, v)
    return cfg


def get_mysql_conf():
    """返回 pymysql 连接参数 dict；环境变量 MYSQL_PASSWORD 优先，找不到密码返回 None。"""
    cfg = load_config()
    mysql = dict(cfg["mysql"])
    env_pw = (os.environ.get("MYSQL_PASSWORD") or "").strip()
    if env_pw:
        mysql["password"] = env_pw
    mysql["password"] = (mysql.get("password") or "").strip()
    mysql["port"] = int(mysql.get("port") or 3306)
    if not mysql["password"]:
        return None
    return mysql


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def mysql_conf_for_pymysql():
    """pymysql 用的完整参数（db 字段名对齐）。"""
    conf = get_mysql_conf()
    if conf is None:
        return None
    return {
        "host": conf["host"], "port": conf["port"],
        "user": conf["user"], "password": conf["password"],
        "db": conf["database"],
    }


if __name__ == "__main__":
    # 自检: python jjconfig.py 显示当前配置(密码打码)
    c = load_config()
    shown = copy.deepcopy(c)
    if shown["mysql"].get("password"):
        shown["mysql"]["password"] = "*" * len(shown["mysql"]["password"])
    print(json.dumps(shown, ensure_ascii=False, indent=2))
    conf = get_mysql_conf()
    print("MySQL 密码来源:", "环境变量" if (os.environ.get("MYSQL_PASSWORD") or "").strip()
          else ("config.json" if conf else "未配置"))
