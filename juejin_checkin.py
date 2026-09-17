# -*- coding: utf-8 -*-
r"""
掘金每日自动签到 + 免费抽奖（签到/抽奖可分开执行）
- 签到:  --mode checkin
- 抽奖:  --mode lottery  只在有免费次数时抽（绝不花积分），抽完返回剩余积分
- 两者:  --mode all      （默认，网页"签到+抽奖"按钮同款）
前置: 先在网页看板点"保存掘金登录"（或命令行 python login_save.py）
记录: data\checkin_log.json
"""
import argparse
import datetime
import json
import os
import sys

from playwright.sync_api import sync_playwright

from notify import notify_once

# Windows 控制台 GBK 兼容
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJ_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PROJ_DIR, "data")
AUTH_PATH = os.path.join(DATA_DIR, "auth_juejin.json")
LOG_PATH = os.path.join(DATA_DIR, "checkin_log.json")
SIGNIN_URL = "https://juejin.cn/user/center/signin"
LOTTERY_URL = "https://juejin.cn/user/center/lottery"


def apply_auth(ctx, auth):
    """注入保存的 cookie / localStorage / sessionStorage"""
    if auth.get("cookies"):
        ctx.add_cookies(auth["cookies"])
    storage_json = json.dumps({
        "local": auth.get("localStorage", {}),
        "session": auth.get("sessionStorage", {}),
    }, ensure_ascii=False)
    ctx.add_init_script(f"""
        (() => {{
            const data = {storage_json};
            for (const [k, v] of Object.entries(data.local)) {{
                try {{ localStorage.setItem(k, v); }} catch (e) {{}}
            }}
            for (const [k, v] of Object.entries(data.session)) {{
                try {{ sessionStorage.setItem(k, v); }} catch (e) {{}}
            }}
        }})();
    """)


def api(page, method, path, body=None):
    """在页面上下文里调掘金 API（自动带 cookie 和来源）"""
    return page.evaluate(
        """async ([method, path, bodyStr]) => {
            try {
                const r = await fetch('https://api.juejin.cn' + path, {
                    method, credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    ...(method === 'POST' ? {body: bodyStr || '{}'} : {}),
                });
                return await r.json();
            } catch (e) { return {error: String(e)}; }
        }""",
        [method, path, json.dumps(body) if body else None],
    )


def append_log(record):
    log = []
    if os.path.exists(LOG_PATH):
        try:
            with open(LOG_PATH, "r", encoding="utf-8") as f:
                log = json.load(f)
        except Exception:
            log = []
    log.append(record)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def get_points(page):
    """当前矿石（积分）数；失败返回 None"""
    r = api(page, "GET", "/growth_api/v1/get_cur_point")
    if r and r.get("err_no") == 0:
        return r["data"]
    return None


def do_checkin(page, result):
    """签到部分"""
    print("正在打开掘金签到页...")
    page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)

    status = api(page, "GET", "/growth_api/v1/get_today_status")
    if status and status.get("err_no") != 0:
        result["status"] = f"登录状态异常: {status.get('err_msg')}，请重新登录"
        print(result["status"])
        notify_once("checkin_cookie", "掘金签到异常", "登录状态（cookie）已过期，请到网页看板重新登录")
        return False

    if status["data"]:
        result["status"] = "今日已签到（无需重复操作）"
    else:
        r = api(page, "POST", "/growth_api/v1/check_in")
        if r and r.get("err_no") == 0:
            result["status"] = "签到成功"
        else:
            result["status"] = f"签到失败: {(r or {}).get('err_msg') or (r or {}).get('error')}"
            notify_once("checkin_fail", "掘金签到异常", result["status"])
    print(result["status"])

    counts = api(page, "GET", "/growth_api/v1/get_counts")
    if counts and counts.get("err_no") == 0:
        d = counts["data"]
        result["cont_days"] = d.get("cont_count")
        result["total_days"] = d.get("sum_count")
        print(f"连续签到 {d.get('cont_count')} 天，累计签到 {d.get('sum_count')} 天")
    return True


def do_lottery(page, result):
    """抽奖部分：API 查免费次数，仅免费时抽，绝不消耗积分"""
    print("查询免费抽奖次数...")
    page.goto(LOTTERY_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)

    state = api(page, "GET", "/growth_api/v1/lottery_config/get")
    free_cnt = None
    if state and state.get("err_no") == 0:
        # data 可能为 {lottery: [...]} 列表结构，逐项找 free_count>0 的
        items = state.get("data") or {}
        if isinstance(items, dict):
            items = items.get("lottery", [])
        for it in items if isinstance(items, list) else []:
            if it.get("type") == "free" or (it.get("free_count", 0) or 0) > 0 or it.get("point", 0) == 0:
                free_cnt = max(free_cnt or 0, int(it.get("free_count", 0) or 0))
                break
    if free_cnt is None:
        # 兜底: 页面文本里找（旧逻辑）
        text = page.evaluate("document.body.innerText")
        if "免费抽奖次数：0" in text:
            free_cnt = 0
        elif "免费抽奖次数：" in text:
            free_cnt = 1  # 有次数但解析不到具体值, 按至少 1 次尝试

    points_before = get_points(page)
    if points_before is not None:
        print(f"当前矿石: {points_before}")

    if not free_cnt:
        result["lottery"] = "今日免费次数已用完，未抽奖（不消耗积分）"
        result["points"] = points_before
        print(result["lottery"])
        return

    # 有免费次数才抽，一次用掉全部免费机会
    drew = 0
    prizes = []
    for _ in range(free_cnt):
        r = api(page, "POST", "/growth_api/v1/lottery/draw")
        if not r or r.get("err_no") != 0:
            msg = (r or {}).get("err_msg") or (r or {}).get("error") or "未知错误"
            print(f"抽奖请求失败: {msg}")
            break
        drew += 1
        prize_name = (r.get("data") or {}).get("lottery_name") or "未知奖品"
        prizes.append(prize_name)
        print(f"抽中: {prize_name}")
        page.wait_for_timeout(1500)  # 抽奖冷却, 防连点被风控

    points_after = get_points(page)
    result["lottery"] = f"免费抽奖 {drew} 次: " + ("、".join(prizes) if prizes else "无结果")
    result["points"] = points_after
    result["points_before"] = points_before
    if points_after is not None:
        print(f"剩余矿石: {points_after}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["all", "checkin", "lottery"], default="all",
                        help="all=签到+抽奖(默认), checkin=只签到, lottery=只抽奖")
    args = parser.parse_args()

    if not os.path.exists(AUTH_PATH):
        print("未找到登录状态，请先到网页看板点「保存掘金登录」（或命令行 python login_save.py）")
        notify_once("checkin_no_auth", "掘金签到异常", "未找到登录状态文件，请到网页看板重新登录")
        return

    with open(AUTH_PATH, "r", encoding="utf-8") as f:
        auth = json.load(f)

    result = {"time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "mode": args.mode}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1366, "height": 900},
            )
            apply_auth(ctx, auth)
            page = ctx.new_page()

            if args.mode in ("all", "checkin"):
                ok = do_checkin(page, result)
                if not ok:
                    append_log(result)
                    return

            if args.mode in ("all", "lottery"):
                do_lottery(page, result)

            # 签到-only 模式也补一份积分
            if args.mode == "checkin":
                pts = get_points(page)
                if pts is not None:
                    result["points"] = pts
        finally:
            browser.close()

    append_log(result)
    print(f"\n记录已保存: {LOG_PATH}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        notify_once("checkin_crash", "掘金签到脚本报错", str(e)[:150])
        raise
