# -*- coding: utf-8 -*-
r"""
掘金沸点数据可视化 - 后端 API
读取本机 MySQL juejin 库，为前端页面提供数据
MySQL 密码来源: 环境变量 MYSQL_PASSWORD > config.json（见 jjconfig.py）

启动: python server\app.py   （无需再设置环境变量）
访问: http://127.0.0.1:5000
"""
import json
import os
import subprocess
import sys
import threading
import time
import datetime
import pandas as pd
import pymysql
import pymysql.cursors
from flask import Flask, jsonify, request, send_file, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))   # ...\juejin\server
PROJ_DIR = os.path.dirname(BASE_DIR)                    # ...\juejin（项目根）
sys.path.insert(0, PROJ_DIR)                            # 让 app 能 import 项目根的模块
from jjconfig import DATA_DIR, AUTH_PATH, load_config, save_config, get_mysql_conf  # noqa: E402

SCRAPER_PATH = os.path.join(PROJ_DIR, "juejin_pins.py")
CHECKIN_PATH = os.path.join(PROJ_DIR, "juejin_checkin.py")
LOGIN_PATH = os.path.join(PROJ_DIR, "login_save.py")

app = Flask(__name__)


def get_conn():
    conf = get_mysql_conf()
    if conf is None:
        raise pymysql.err.OperationalError(1045, "未配置 MySQL 密码（config.json 或环境变量 MYSQL_PASSWORD）")
    return pymysql.connect(
        host=conf["host"], port=conf["port"], user=conf["user"],
        password=conf["password"], database=conf["database"],
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    )


@app.errorhandler(pymysql.err.OperationalError)
def db_error(e):
    """数据库连接类错误统一返回友好 JSON"""
    code = e.args[0] if e.args else 0
    if code == 1045:
        msg = "数据库连接失败: 密码错误或未配置, 请检查 config.json 里 mysql.password"
    elif code == 2003:
        msg = "数据库连接失败: 无法连接 MySQL, 请确认服务已启动"
    elif code == 1049:
        msg = "数据库连接失败: 不存在 juejin 库, 请先运行一次抓取脚本建库"
    else:
        msg = f"数据库连接失败: {e}"
    return jsonify({"ok": False, "msg": msg}), 502


# ---------------- 后台任务框架（抓取 / 签到 共用模式） ----------------
def make_job():
    return {"running": False, "log": "", "started_at": ""}


scrape_job = make_job()
checkin_job = make_job()
job_lock = threading.Lock()


def run_script_job(job, script_path, args, finish_line):
    """在后台线程跑一个 py 脚本并把 stdout 逐行收集进 job['log']"""
    def run():
        with job_lock:
            job.update(running=True, log=(job["log"].splitlines()[-1] + "\n" if job["log"] else ""),
                       started_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        try:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"  # 与父进程解码对齐, 防 GBK 乱码
            proc = subprocess.Popen(
                [sys.executable, script_path] + args,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                env=env, cwd=PROJ_DIR,
            )
            for line in proc.stdout:
                with job_lock:
                    job["log"] = (job["log"] + line)[-10000:]
            proc.wait()
            with job_lock:
                job["log"] += "\n" + finish_line(proc.returncode)
        except Exception as exc:
            with job_lock:
                job["log"] += f"\n任务异常: {exc}"
        finally:
            with job_lock:
                job["running"] = False

    threading.Thread(target=run, daemon=True).start()


# ---------------- 页面 ----------------
@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


# ---------------- 数据查询 ----------------
@app.route("/api/stats")
def stats():
    """总览统计 + 点赞TOP10 + 活跃作者TOP10"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) AS pin_total,
                       COALESCE(SUM(digg_count), 0) AS digg_total,
                       COALESCE(SUM(comment_count), 0) AS comment_total,
                       MAX(crawl_time) AS last_crawl
                FROM pin
            """)
            overview = cur.fetchone()

            cur.execute("SELECT COUNT(*) AS c FROM pin_comment")
            overview["comment_fetched"] = cur.fetchone()["c"]

            if overview.get("last_crawl") is not None:
                overview["last_crawl"] = overview["last_crawl"].strftime("%Y-%m-%d %H:%M:%S")
            for k in ("digg_total", "comment_total"):
                overview[k] = int(overview[k] or 0)

            cur.execute("""
                SELECT msg_id, author, LEFT(content, 30) AS content, digg_count
                FROM pin ORDER BY digg_count DESC LIMIT 10
            """)
            top_pins = cur.fetchall()

            cur.execute("""
                SELECT author, COUNT(*) AS cnt, SUM(digg_count) AS diggs
                FROM pin GROUP BY author ORDER BY cnt DESC, diggs DESC LIMIT 10
            """)
            top_authors = cur.fetchall()
        return jsonify({"overview": overview, "top_pins": top_pins, "top_authors": top_authors})
    finally:
        conn.close()


@app.route("/api/pins")
def pins():
    """沸点分页列表，支持关键词搜索、排序、发布时间范围筛选
    days 参数: 0/缺省=全部, n=近 n 天（含今天）"""
    page = max(int(request.args.get("page", 1)), 1)
    size = min(max(int(request.args.get("size", 10)), 1), 100)
    keyword = request.args.get("keyword", "").strip()
    sort = request.args.get("sort", "publish_time")
    order = "ASC" if request.args.get("order") == "asc" else "DESC"
    sort_col = {"digg_count": "digg_count", "comment_count": "comment_count",
                "publish_time": "publish_time", "crawl_time": "crawl_time"}.get(sort, "publish_time")
    try:
        days = int(request.args.get("days", 0) or 0)
    except ValueError:
        days = 0
    if days < 0:
        days = 0

    conds, params = [], []
    if keyword:
        conds.append("(content LIKE %s OR author LIKE %s)")
        params += [f"%{keyword}%", f"%{keyword}%"]
    if days > 0:
        conds.append("publish_time >= DATE_SUB(CURDATE(), INTERVAL %s DAY)")
        params.append(days - 1)  # 近1天=今天(减0天), 近3天=前天起(减2天)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS c FROM pin {where}", params)
            total = cur.fetchone()["c"]
            cur.execute(
                f"""SELECT msg_id, author, job_title, company, content,
                           digg_count, comment_count, pic_count,
                           publish_time, pin_url, crawl_time
                    FROM pin {where}
                    ORDER BY {sort_col} {order}
                    LIMIT %s OFFSET %s""",
                params + [size, (page - 1) * size],
            )
            rows = cur.fetchall()
        for r in rows:
            for k in ("publish_time", "crawl_time"):
                if r[k] is not None:
                    r[k] = r[k].strftime("%Y-%m-%d %H:%M:%S")
        return jsonify({"total": total, "rows": rows})
    finally:
        conn.close()


@app.route("/api/pins/<msg_id>/comments")
def pin_comments(msg_id):
    """某条沸点的评论列表"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT comment_id, author, content, digg_count, reply_count, comment_time
                   FROM pin_comment WHERE msg_id = %s
                   ORDER BY digg_count DESC, comment_time ASC""",
                (msg_id,),
            )
            rows = cur.fetchall()
        for r in rows:
            if r["comment_time"] is not None:
                r["comment_time"] = r["comment_time"].strftime("%Y-%m-%d %H:%M:%S")
        return jsonify({"rows": rows})
    finally:
        conn.close()


# ---------------- 抓取 ----------------
@app.route("/api/scrape", methods=["POST"])
def scrape():
    """启动抓取任务（后台线程跑脚本，前端轮询进度）"""
    if scrape_job["running"]:
        return jsonify({"ok": False, "msg": "已有抓取任务在运行中"}), 409

    data = request.get_json(force=True, silent=True) or {}
    total = min(max(int(data.get("total", 50)), 1), 500)
    comments = min(max(int(data.get("comments", 20)), -1), 100)

    if not os.path.isfile(SCRAPER_PATH):
        return jsonify({"ok": False, "msg": f"未找到抓取脚本: {SCRAPER_PATH}"}), 400

    head = f"启动: 沸点 {total} 条, 评论 {'全部' if comments < 0 else str(comments) + ' 条/帖'}\n"
    with job_lock:
        scrape_job["log"] = head
    run_script_job(scrape_job, SCRAPER_PATH,
                   ["--total", str(total), "--comments", str(comments)],
                   lambda rc: "抓取完成" if rc == 0 else f"抓取失败(退出码 {rc})")
    return jsonify({"ok": True})


@app.route("/api/scrape/status")
def scrape_status():
    with job_lock:
        return jsonify({"running": scrape_job["running"], "log": scrape_job["log"]})


# ---------------- 签到 ----------------
CHECKIN_LOG_PATH = os.path.join(DATA_DIR, "checkin_log.json")


@app.route("/api/checkin", methods=["POST"])
def checkin():
    """启动签到任务（后台跑 juejin_checkin.py）
    body: {"mode": "all"|"checkin"|"lottery"}  默认 all"""
    if checkin_job["running"]:
        return jsonify({"ok": False, "msg": "已有签到任务在运行中"}), 409
    if not os.path.isfile(CHECKIN_PATH):
        return jsonify({"ok": False, "msg": f"未找到签到脚本: {CHECKIN_PATH}"}), 400

    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("mode", "all")
    if mode not in ("all", "checkin", "lottery"):
        mode = "all"
    label = {"all": "签到 + 免费抽奖", "checkin": "仅签到",
             "lottery": "免费抽奖" if not data.get("use_points") else "抽奖(允许矿石补抽)"}[mode]

    cmd = ["--mode", mode]
    if data.get("use_points"):
        cmd += ["--use-points"]
        try:
            max_paid = int(data.get("max_paid", 0) or 0)
            if max_paid > 0:
                cmd += ["--max-paid", str(max_paid)]
        except (TypeError, ValueError):
            pass

    with job_lock:
        checkin_job["log"] = f"启动: {label}...\n"
    run_script_job(checkin_job, CHECKIN_PATH, cmd,
                   lambda rc: f"{label}完成" if rc == 0 else f"{label}失败(退出码 {rc})")
    return jsonify({"ok": True})


@app.route("/api/checkin/status")
def checkin_status():
    with job_lock:
        running, log = checkin_job["running"], checkin_job["log"]
    # 附带最近一次签到结果
    last = None
    try:
        with open(CHECKIN_LOG_PATH, "r", encoding="utf-8") as f:
            records = json.load(f)
        if records:
            last = records[-1]
    except Exception:
        pass
    return jsonify({"running": running, "log": log, "last": last})


# ---------------- 登录态 ----------------
@app.route("/api/auth/status")
def auth_status():
    """登录态文件是否存在 + 保存时间 + 保存时的用户名"""
    if not os.path.exists(AUTH_PATH):
        return jsonify({"exists": False})
    try:
        with open(AUTH_PATH, "r", encoding="utf-8") as f:
            auth = json.load(f)
        return jsonify({"exists": True,
                        "user_name": auth.get("user_name", ""),
                        "saved_at": auth.get("saved_at", "")})
    except Exception as e:
        return jsonify({"exists": True, "user_name": "", "saved_at": "", "error": str(e)})


# ---------------- 抓取节奏（高级设置） ----------------
SPEED_DEFAULTS = {"workers": 2, "page_delay": 1.0, "pin_delay": 1.5}
SPEED_RANGES = {"workers": (1, 10), "page_delay": (0, 30), "pin_delay": (0, 30)}


@app.route("/api/scrape/speed", methods=["GET", "POST"])
def scrape_speed():
    """GET 读当前抓取节奏参数(含默认值); POST 保存(缺省键回落默认, 恢复默认传 reset=true)"""
    if request.method == "GET":
        cfg = load_config()
        sc = dict(SPEED_DEFAULTS)
        sc.update({k: v for k, v in (cfg.get("scrape") or {}).items() if k in SPEED_DEFAULTS})
        return jsonify({"speed": sc, "defaults": SPEED_DEFAULTS})
    data = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    if data.get("reset"):
        cfg["scrape"] = dict(SPEED_DEFAULTS)
        save_config(cfg)
        return jsonify({"ok": True, "speed": dict(SPEED_DEFAULTS), "msg": "已恢复默认节奏"})
    sc = dict(SPEED_DEFAULTS)
    sc.update(cfg.get("scrape") or {})
    for key in SPEED_DEFAULTS:
        if key in data:
            try:
                val = float(data[key])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "msg": f"{key} 必须是数字"}), 400
            lo, hi = SPEED_RANGES[key]
            val = int(val) if key == "workers" else val
            if not (lo <= val <= hi):
                return jsonify({"ok": False, "msg": f"{key} 超出范围 [{lo}, {hi}]"}), 400
            sc[key] = val
    cfg["scrape"] = sc
    save_config(cfg)
    return jsonify({"ok": True, "speed": sc, "msg": "抓取节奏已保存"})


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    """弹出浏览器登录窗口（login_save.py），登录成功自动保存"""
    if not os.path.isfile(LOGIN_PATH):
        return jsonify({"ok": False, "msg": f"未找到登录脚本: {LOGIN_PATH}"}), 400
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:  # 有头浏览器, 独立进程, 不收集输出（用户在浏览器里操作）
        subprocess.Popen([sys.executable, LOGIN_PATH],
                         env=env, cwd=PROJ_DIR,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        return jsonify({"ok": False, "msg": f"启动登录窗口失败: {e}"}), 500
    return jsonify({"ok": True, "msg": "已打开浏览器，登录成功后会自动保存"})


# ---------------- 网页内定时抓取 ----------------
scheduler_state = {"thread": None, "stop_event": threading.Event(), "next_run": None}


def _scrape_once_scheduled():
    """定时器触发的静默抓取（不抓评论, 刷新榜单即可）"""
    with job_lock:
        if scrape_job["running"]:
            return
        scrape_job["log"] = "定时抓取: 启动\n"
    run_script_job(scrape_job, SCRAPER_PATH, ["--total", "100", "--comments", "0", "--no-excel"],
                   lambda rc: f"定时抓取{'完成' if rc == 0 else f'失败(退出码 {rc})'}")


def scheduler_loop(interval):
    """常驻线程: 每 interval 秒触发一次抓取"""
    while not scheduler_state["stop_event"].is_set():
        scheduler_state["next_run"] = datetime.datetime.now() + datetime.timedelta(seconds=interval)
        if scheduler_state["stop_event"].wait(timeout=interval):
            break
        _scrape_once_scheduled()


def start_scheduler(interval):
    stop_scheduler()
    if interval <= 0:
        return
    scheduler_state["stop_event"] = threading.Event()
    t = threading.Thread(target=scheduler_loop, args=(interval,), daemon=True)
    scheduler_state["thread"] = t
    t.start()


def stop_scheduler():
    scheduler_state["stop_event"].set()
    scheduler_state["thread"] = None
    scheduler_state["next_run"] = None


@app.route("/api/schedule", methods=["GET", "POST"])
def schedule():
    """GET 查询定时抓取状态; POST 设置间隔（秒, 0=关闭），持久化到 config.json"""
    if request.method == "GET":
        interval = int(load_config().get("scrape_interval", 0) or 0)
        return jsonify({"interval": interval,
                        "active": scheduler_state["thread"] is not None,
                        "next_run": scheduler_state["next_run"].strftime("%H:%M:%S") if scheduler_state["next_run"] else None})
    data = request.get_json(force=True, silent=True) or {}
    try:
        interval = max(int(data.get("interval", 0)), 0)
    except Exception:
        return jsonify({"ok": False, "msg": "间隔必须是数字"}), 400
    cfg = load_config()
    cfg["scrape_interval"] = interval
    save_config(cfg)
    start_scheduler(interval)
    return jsonify({"ok": True, "interval": interval,
                    "msg": f"定时抓取已{'设置为每 ' + str(interval) + ' 秒一次' if interval > 0 else '关闭'}"})


# ---------------- 导出 ----------------
@app.route("/api/export")
def export():
    """导出 MySQL 中的沸点+评论为 Excel（评论合并进主表）"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT msg_id, author, job_title, company, content,
                       digg_count, comment_count, pic_count, publish_time, pin_url
                FROM pin ORDER BY publish_time DESC
            """)
            pin_rows = cur.fetchall()
            cur.execute("""
                SELECT msg_id, parent_id, author, content, digg_count,
                       reply_count, reply_to_user, comment_time
                FROM pin_comment ORDER BY id ASC
            """)
            comment_rows = cur.fetchall()
    finally:
        conn.close()

    if not pin_rows:
        return jsonify({"ok": False, "msg": "数据库暂无数据，请先抓取"}), 404

    comments_map = {}
    for c in comment_rows:
        comments_map.setdefault(c["msg_id"], []).append(c)

    def fmt(t):
        return t.strftime("%Y-%m-%d %H:%M:%S") if t else ""

    merged_rows = []
    for idx, p in enumerate(pin_rows, 1):
        pin_part = {
            "序号": idx, "作者": p["author"], "职业": p["job_title"], "公司": p["company"],
            "内容": p["content"], "点赞数": p["digg_count"], "评论数": p["comment_count"],
            "图片数": p["pic_count"], "发布时间": fmt(p["publish_time"]), "沸点链接": p["pin_url"],
        }
        cs = comments_map.get(p["msg_id"], [])
        if cs:
            for c in cs:
                merged_rows.append({**pin_part,
                                    "类型": "回复" if c["parent_id"] else "评论",
                                    "评论作者": c["author"], "评论内容": c["content"],
                                    "评论点赞数": c["digg_count"], "评论回复数": c["reply_count"],
                                    "评论时间": fmt(c["comment_time"]), "回复对象": c["reply_to_user"]})
        else:
            merged_rows.append({**pin_part, "类型": "", "评论作者": "", "评论内容": "",
                                "评论点赞数": "", "评论回复数": "", "评论时间": "", "回复对象": ""})

    df = pd.DataFrame(merged_rows)
    filename = f"沸点热门_{datetime.datetime.now():%Y%m%d_%H%M%S}.xlsx"
    os.makedirs(DATA_DIR, exist_ok=True)
    out_path = os.path.join(DATA_DIR, filename)
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="沸点热门")
        ws = writer.sheets["沸点热门"]
        widths = {"A": 6, "B": 16, "C": 18, "D": 20, "E": 50, "F": 8, "G": 8,
                  "H": 8, "I": 20, "J": 36, "K": 8, "L": 16, "M": 50, "N": 10, "O": 10,
                  "P": 20, "Q": 14}
        for col, w in widths.items():
            ws.column_dimensions[col].width = w
    return send_file(out_path, as_attachment=True, download_name=filename)


if __name__ == "__main__":
    try:  # 启动预检: 配置/密码/服务 在这里直接报出来
        get_conn().close()
    except pymysql.err.OperationalError as e:
        code = e.args[0] if e.args else 0
        hint = {1045: "MySQL 密码未配置或错误, 请编辑 config.json 填 mysql.password",
                2003: "无法连接 MySQL, 请确认服务已启动"}.get(code, str(e))
        print(f"MySQL 预检失败: {hint}")
        raise SystemExit(1)
    # 恢复上次保存的定时抓取设置
    _interval = int(load_config().get("scrape_interval", 0) or 0)
    if _interval > 0:
        start_scheduler(_interval)
        print(f"已恢复定时抓取: 每 {_interval} 秒一次")
    print("MySQL 预检通过, 启动成功: http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
