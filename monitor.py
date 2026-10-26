# -*- coding: utf-8 -*-
"""
关键词监控推送（推送中心的引擎）。

思路:
  每次抓取完成后巡检一遍 MySQL:
    1) 新沸点  : first_crawl_time 在上次巡检之后入库的 pin
    2) 新评论  : comment_time 在上次巡检之后发布的 pin_comment
  内容命中任一关注关键词 -> 去重(同一条只推一次) -> 桌面通知 + 可选微信(Server酱)

  去重记录   : data/push_sent.json   { msg_id|comment_id : 推送时间 }
  水位       : data/push_watermark.json { "pin": "...", "comment": "..." }
  推送历史   : data/push_log.json    [ {time, channel, title, message, matched, type, target} ]
  配置       : config.json 的 push 节（网页"推送中心"可视化编辑，见 server/app.py）

手动巡检: python monitor.py           （读库即可，不触发抓取）
"""
import json
import os
import sys
import datetime

from notify import notify

PROJ_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PROJ_DIR, "data")
SENT_PATH = os.path.join(DATA_DIR, "push_sent.json")
WATERMARK_PATH = os.path.join(DATA_DIR, "push_watermark.json")
LOG_PATH = os.path.join(DATA_DIR, "push_log.json")

PUSH_DEFAULTS = {
    "enabled": True,
    "keywords": ["内推", "招聘"],
    "desktop": True,
    "serverchan_key": "",
    "serverchan": False,
    "scan_comments": True,
    "scan_pins": True,
}

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def load_push_conf():
    """读 config.json 的 push 节，缺的键用默认值补齐"""
    from jjconfig import load_config
    cfg = load_config()
    push = dict(PUSH_DEFAULTS)
    push.update(cfg.get("push") or {})
    push["keywords"] = [str(k).strip() for k in (push.get("keywords") or [])
                        if str(k).strip()]
    return push


def save_push_conf(push):
    from jjconfig import load_config, save_config
    cfg = load_config()
    cfg["push"] = push
    save_config(cfg)


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def append_push_log(entry):
    """记录最近一次推送（上限 500 条，超出裁最旧的一半）"""
    logs = _read_json(LOG_PATH, [])
    if not isinstance(logs, list):
        logs = []
    logs.append(entry)
    if len(logs) > 500:
        logs = logs[len(logs) - 500:]
    _write_json(LOG_PATH, logs)


def match_keywords(text, keywords):
    """返回 text 命中的关键词列表（大小写不敏感；text 为空返回空）"""
    if not text or not keywords:
        return []
    t = str(text).lower()
    return [k for k in keywords if k.lower() in t]


def serverchan_send(key, title, message):
    """Server酱·推送配置消息（key 为 SendKey，形如 SCTxxxx）；失败静默返回 False"""
    try:
        import requests
        r = requests.post(
            f"https://sctapi.ftqq.com/{key}.send",
            data={"title": title[:100], "desp": message[:10000]},
            timeout=10,
        )
        return r.status_code == 200 and (r.json().get("code") == 0)
    except Exception:
        return False


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def get_conn():
    import pymysql
    import pymysql.cursors
    from jjconfig import get_mysql_conf
    conf = get_mysql_conf()
    if conf is None:
        return None
    return pymysql.connect(
        host=conf["host"], port=conf["port"], user=conf["user"],
        password=conf["password"], database=conf["database"],
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    )


def check_once(verbose=True, force_notify=False):
    """巡检一次。返回 (命中条数, 描述文字)。
    verbose=True 打印过程；force_notify=True 忽略桌面通知的开关（网页"测试推送"用）"""
    push = load_push_conf()
    if not push["keywords"]:
        if verbose:
            print("[推送] 尚未设置关键词，跳过巡检（可在网页推送中心添加）")
        return 0, "未设置关键词"

    sent = _read_json(SENT_PATH, {})            # 已推送去重表
    wm = _read_json(WATERMARK_PATH, {})         # 上次巡检水位
    now = datetime.datetime.now()
    now_str = _fmt(now)

    hits = []   # 待推送: (type, id, title, message, matched)
    new_pin_wm = wm.get("pin") or ""
    new_comment_wm = wm.get("comment") or ""

    conn = get_conn()
    if conn is None:
        if verbose:
            print("[推送] 未配置 MySQL 密码，跳过巡检")
        return 0, "数据库未配置"
    try:
        with conn.cursor() as cur:
            # 新沸点: 首次入库时间晚于水位（水位为空 = 首次巡检，只建基线）
            if push["scan_pins"]:
                if new_pin_wm:
                    cur.execute(
                        """SELECT msg_id, author, content, digg_count, comment_count,
                                  pin_url, first_crawl_time
                           FROM pin WHERE first_crawl_time > %s
                           ORDER BY first_crawl_time ASC LIMIT 2000""",
                        (new_pin_wm,),
                    )
                else:
                    cur.execute(
                        """SELECT msg_id, author, content, digg_count, comment_count,
                                  pin_url, first_crawl_time
                           FROM pin ORDER BY first_crawl_time ASC LIMIT 2000""",
                    )
                for r in cur.fetchall():
                    fct = _fmt(r["first_crawl_time"])
                    if fct > new_pin_wm:
                        new_pin_wm = fct
                    mid = str(r["msg_id"])
                    if mid in sent:
                        continue
                    matched = match_keywords(r["content"], push["keywords"])
                    if matched:
                        hits.append(("pin", mid,
                                     f"沸点 · {r['author']}",
                                     f"{r['content']}\n\n点赞 {r['digg_count']} | 评论 {r['comment_count']}\n{r['pin_url']}",
                                     matched))
            # 新评论: 评论发布时间晚于水位
            if push["scan_comments"]:
                if new_comment_wm:
                    cur.execute(
                        """SELECT c.comment_id, c.author, c.content, c.digg_count,
                                  c.comment_time, p.author AS pin_author, p.content AS pin_content
                           FROM pin_comment c LEFT JOIN pin p ON p.msg_id = c.msg_id
                           WHERE c.comment_time > %s
                           ORDER BY c.comment_time ASC LIMIT 2000""",
                        (new_comment_wm,),
                    )
                else:
                    cur.execute(
                        """SELECT c.comment_id, c.author, c.content, c.digg_count,
                                  c.comment_time, p.author AS pin_author, p.content AS pin_content
                           FROM pin_comment c LEFT JOIN pin p ON p.msg_id = c.msg_id
                           ORDER BY c.comment_time ASC LIMIT 2000""",
                    )
                for r in cur.fetchall():
                    ct = _fmt(r["comment_time"])
                    if ct > new_comment_wm:
                        new_comment_wm = ct
                    cid = str(r["comment_id"])
                    if cid in sent:
                        continue
                    matched = match_keywords(r["content"], push["keywords"])
                    if not matched and r["pin_content"]:
                        # 评论本身没命中时, 若其所属沸点命中(如"内推"帖下的新回复)也算
                        matched = match_keywords(r["pin_content"], push["keywords"])
                        matched = [f"{k}(沸点)" for k in matched]
                    if matched:
                        hits.append(("comment", cid,
                                     f"评论 · {r['author']} @ {r['pin_author'] or '(已删除沸点)'}",
                                     f"评论: {r['content']}\n\n沸点: {(r['pin_content'] or '')[:80]}\n点赞 {r['digg_count']}",
                                     matched))
    finally:
        conn.close()

    # 首次运行: 只建水位不补推历史（避免一次弹几十条通知）
    first_run = not wm
    if first_run:
        hits = []

    # 推送
    ok_count = 0
    sc_key = (push.get("serverchan_key") or "").strip()
    for typ, rid, title, message, matched in hits[:10]:   # 每轮最多推 10 条防轰炸
        kw = "、".join(matched[:3])
        line = f"[{kw}] {title}"
        sent_desktop = False
        if push["desktop"] or force_notify:
            notify(f"掘金推送 · 命中 {kw}", f"{title}\n{message}")
            sent_desktop = True
        sent_wechat = False
        if push["serverchan"] and sc_key:
            sent_wechat = serverchan_send(sc_key, f"掘金推送 · 命中 {kw}",
                                          f"**{title}**\n\n{message}\n\n关键词: {kw}")
        if sent_desktop or sent_wechat:
            ok_count += 1
            sent[rid] = now_str
            append_push_log({"time": now_str,
                             "channel": ("微信" if sent_wechat else "") + ("+桌面" if sent_desktop else ""),
                             "title": title, "message": message[:200], "matched": kw,
                             "type": typ, "target": rid})
    # 未推送但已巡检过的也标记，防止下次重复判断
    for typ, rid, title, message, matched in hits[10:]:
        sent[rid] = now_str

    # 更新水位（只在确有新数据时前移）
    if wm.get("pin") != new_pin_wm or wm.get("comment") != new_comment_wm:
        _write_json(WATERMARK_PATH, {"pin": new_pin_wm, "comment": new_comment_wm,
                                     "updated_at": now_str})
    if sent:
        _write_json(SENT_PATH, sent)

    summary = (f"首次巡检: 已建基线，从下一次抓取起新数据才开始推送" if first_run
               else (f"命中 {len(hits)} 条，推送 {ok_count} 条" if hits else "无新命中"))
    if verbose:
        print(f"[推送] 巡检完成: {summary}")
    return len(hits), summary


def run_check_async():
    """给 server/app.py 调: 抓取完成后异步巡检一次（不阻塞）"""
    def _run():
        try:
            check_once(verbose=False)
        except Exception:
            pass
    import threading
    threading.Thread(target=_run, daemon=True).start()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="掘金关键词推送巡检")
    parser.add_argument("--test-notify", action="store_true", help="弹一条测试通知（不看关键词）")
    args = parser.parse_args()
    if args.test_notify:
        notify("掘金推送中心", "测试通知：看到我说明桌面通知通道正常")
        print("[推送] 测试通知已发出")
    else:
        check_once(verbose=True)
