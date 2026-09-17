# -*- coding: utf-8 -*-
r"""
掘金每日自动签到 + 免费抽奖
前置: 先运行 python login_save.py juejin 手动登录一次
用法: python juejin_checkin.py
签到记录保存在 项目目录\data\checkin_log.json
"""
import os
import json
import datetime
from playwright.sync_api import sync_playwright

from notify import notify, notify_once

# 数据目录: 脚本所在目录下的 data（项目整体搬家后无需再改）
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


def api(page, method, path):
    """在页面上下文里调掘金 API（自动带 cookie 和来源）"""
    return page.evaluate(
        """async ([method, path]) => {
            try {
                const r = await fetch('https://api.juejin.cn' + path, {
                    method, credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    ...(method === 'POST' ? {body: '{}'} : {}),
                });
                return await r.json();
            } catch (e) { return {error: String(e)}; }
        }""",
        [method, path],
    )


def append_log(record):
    log = []
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            log = json.load(f)
    log.append(record)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def main():
    if not os.path.exists(AUTH_PATH):
        print("未找到登录状态，请先运行: python D:\\11\\scripts\\login_save.py juejin")
        notify_once("checkin_no_auth", "掘金签到异常",
                    "未找到登录状态文件，请运行 python D:\\11\\scripts\\login_save.py juejin 重新登录")
        return

    with open(AUTH_PATH, "r", encoding="utf-8") as f:
        auth = json.load(f)

    result = {"time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
        )
        apply_auth(ctx, auth)
        page = ctx.new_page()

        # ============ 1. 签到 ============
        print("正在打开掘金签到页...")
        page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)

        status = api(page, "GET", "/growth_api/v1/get_today_status")
        if status and status.get("err_no") != 0:
            result["status"] = f"登录状态异常: {status.get('err_msg')}，请重新登录"
            print(result["status"])
            notify_once("checkin_cookie", "掘金签到异常",
                        "登录状态（cookie）已过期，请运行 python D:\\11\\scripts\\login_save.py juejin 重新登录")
            browser.close()
            append_log(result)
            return

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

        # ============ 2. 免费抽奖 ============
        print("进入抽奖页...")
        page.goto(LOTTERY_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        # 可能有风控弹窗，先关掉
        try:
            page.locator("button:has-text('我知道了')").click(timeout=2000)
            page.wait_for_timeout(1000)
        except Exception:
            pass

        lottery_text = page.evaluate("document.body.innerText")
        if "免费抽奖次数：0" in lottery_text:
            result["lottery"] = "今日免费抽奖次数已用完"
            print(result["lottery"])
        elif "免费抽奖次数：" in lottery_text:
            try:
                page.locator(".turntable-item.lottery",
                             has_text="免费抽奖次数").first.click(timeout=5000)
                page.wait_for_timeout(6000)  # 等转盘动画和结果弹窗
                after_text = page.evaluate("document.body.innerText")
                # 从结果弹窗中提取奖品名
                prize = None
                for line in after_text.splitlines():
                    line = line.strip()
                    if "恭喜" in line and "抽中" in line:
                        prize = line
                        break
                # 关掉可能的获奖弹窗
                try:
                    page.locator("button:has-text('我知道了'), "
                                 "button:has-text('开心收下'), "
                                 ".modal-close").first.click(timeout=1500)
                except Exception:
                    pass
                result["lottery"] = f"抽奖完成: {prize}" if prize else "抽奖完成（未捕获到奖品名，见日志页）"
                print(result["lottery"])
            except Exception as e:
                result["lottery"] = f"抽奖点击失败: {e}"
                print(result["lottery"])
                notify_once("lottery_fail", "掘金抽奖异常", str(e)[:150])
        else:
            result["lottery"] = "未找到抽奖入口"
            print(result["lottery"])

        # ============ 3. 统计 ============
        counts = api(page, "GET", "/growth_api/v1/get_counts")
        point = api(page, "GET", "/growth_api/v1/get_cur_point")
        if counts and counts.get("err_no") == 0:
            d = counts["data"]
            result["cont_days"] = d.get("cont_count")
            result["total_days"] = d.get("sum_count")
            print(f"连续签到 {d.get('cont_count')} 天，累计签到 {d.get('sum_count')} 天")
        if point and point.get("err_no") == 0:
            result["points"] = point["data"]
            print(f"当前矿石数: {point['data']}")

        browser.close()

    append_log(result)
    print(f"\n记录已保存: {LOG_PATH}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        notify_once("checkin_crash", "掘金签到脚本报错", str(e)[:150])
        raise
