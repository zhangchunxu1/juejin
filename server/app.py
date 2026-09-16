# -*- coding: utf-8 -*-
"""
掘金沸点数据可视化 - 后端 API
读取本机 MySQL juejin 库，为前端页面提供数据
密码从环境变量 MYSQL_PASSWORD 读取（不落盘）

启动: $env:MYSQL_PASSWORD='你的密码'; python D:\11\server\app.py
访问: http://127.0.0.1:5000
"""
import os
import sys
import subprocess
import threading
import datetime
import pandas as pd
import pymysql
import pymysql.cursors
from flask import Flask, jsonify, request, send_file, send_from_directory

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRAPER_PATH = r"D:\11\juejin\juejin_pins.py"
DATA_DIR = r"D:\11\juejin\data"

# 抓取任务状态（单任务，简单内存态即可）
scrape_job = {"running": False, "log": ""}
job_lock = threading.Lock()


def get_conn():
    return pymysql.connect(
        host="127.0.0.1", port=3306, user="root",
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database="juejin", charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


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
    """沸点分页列表，支持关键词搜索和排序"""
    page = max(int(request.args.get("page", 1)), 1)
    size = min(max(int(request.args.get("size", 10)), 1), 100)
    keyword = request.args.get("keyword", "").strip()
    sort = request.args.get("sort", "publish_time")
    order = "ASC" if request.args.get("order") == "asc" else "DESC"
    sort_col = {"digg_count": "digg_count", "comment_count": "comment_count",
                "publish_time": "publish_time", "crawl_time": "crawl_time"}.get(sort, "publish_time")

    where, params = "", []
    if keyword:
        where = "WHERE content LIKE %s OR author LIKE %s"
        params = [f"%{keyword}%", f"%{keyword}%"]

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


@app.route("/api/scrape", methods=["POST"])
def scrape():
    """启动抓取任务（后台线程跑脚本，前端轮询进度）"""
    if scrape_job["running"]:
        return jsonify({"ok": False, "msg": "已有抓取任务在运行中"}), 409

    data = request.get_json(force=True, silent=True) or {}
    total = min(max(int(data.get("total", 50)), 1), 500)
    comments = min(max(int(data.get("comments", 20)), -1), 100)  # -1 = 全部评论

    def run():
        with job_lock:
            scrape_job.update(running=True, log=f"启动: 沸点 {total} 条, 评论 {'全部' if comments < 0 else str(comments) + ' 条/帖'}\n")
        proc = subprocess.Popen(
            [sys.executable, SCRAPER_PATH, "--total", str(total), "--comments", str(comments)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=os.environ.copy(),  # 继承 MYSQL_PASSWORD
        )
        for line in proc.stdout:
            with job_lock:
                # 日志只保留尾部，防止过长
                scrape_job["log"] = (scrape_job["log"] + line)[-10000:]
        proc.wait()
        with job_lock:
            scrape_job["log"] += f"\n{'抓取完成' if proc.returncode == 0 else f'抓取失败(退出码 {proc.returncode})'}"
            scrape_job["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/scrape/status")
def scrape_status():
    """抓取任务进度"""
    with job_lock:
        return jsonify({"running": scrape_job["running"], "log": scrape_job["log"]})


@app.route("/api/export")
def export():
    """导出 MySQL 中的沸点+评论为 Excel（格式与抓取脚本一致：评论合并进主表）"""
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
    if not os.environ.get("MYSQL_PASSWORD"):
        print("请先设置环境变量 MYSQL_PASSWORD")
        raise SystemExit(1)
    print("启动成功: http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
