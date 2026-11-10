# -*- coding: utf-8 -*-
r"""
掘金每日自动签到 + 免费抽奖（签到/抽奖可分开执行）
- 签到:  --mode checkin
- 抽奖:  --mode lottery  只用免费次数（绝不花积分），抽完返回剩余积分
- 两者:  --mode all      （默认，网页"签到+抽奖"按钮同款）
前置: 先在网页看板点"保存掘金登录"（或命令行 python login_save.py）
记录: data\checkin_log.json

免费次数来源: GET /growth_api/v1/lottery_config/get 的 data.free_count / data.point_cost
（data.lottery 只是奖品清单，不是次数配置；旧版误读它导致每天误判"次数已用完"）
抽奖执行方式: 点页面自己的「免费抽 1 次」按钮 #turntable-item-0。
掘金 draw 接口需要页面动态生成的 msToken/a_bogus 签名 + x-secsdk-csrf-token 头，
脚本直接 POST 会被静默拒绝（HTTP 200 空响应），走页面按钮才稳；点击前还会
校验按钮文字含"免费"，避免误点「单抽 200」/「十连抽 2000」扣矿石。
"""
import argparse
import datetime
import json
import os
import re
import sys

from playwright.sync_api import sync_playwright

from jjconfig import launch_playwright_browser
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

DEFAULT_POINT_COST = 200   # 接口没给 point_cost 时的兜底（掘金现价 200 矿石/次）
MAX_FREE_DRAWS = 5         # 单次运行免费抽奖硬上限，防接口异常导致无限循环
MAX_PAID_DRAWS_SAFE = 20   # 开了矿石补抽又没填次数时的绝对上限


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
    """在页面上下文里调掘金 API（自动带 cookie 和来源，页面脚本会补签名参数）
    非 JSON 响应也返回结构化错误，方便上层区分"接口异常"和"无免费次数"。"""
    return page.evaluate(
        """async ([method, path, bodyStr]) => {
            try {
                const r = await fetch('https://api.juejin.cn' + path, {
                    method, credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    ...(method === 'POST' ? {body: bodyStr || '{}'} : {}),
                });
                const t = await r.text();
                try { return JSON.parse(t); }
                catch (e) { return {error: '非JSON响应(HTTP ' + r.status + '): ' + t.slice(0, 120)}; }
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
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def get_points(page):
    """当前矿石（积分）数；失败返回 None"""
    r = api(page, "GET", "/growth_api/v1/get_cur_point")
    if r and r.get("err_no") == 0:
        return r["data"]
    return None


def get_lottery_config(page):
    """只读抽奖配置，返回 (剩余免费次数, 单次矿石消耗)；解析失败返回 (None, None)。

    掘金 data 结构: {"lottery": [奖品清单...], "free_count": 0或1, "point_cost": 200}
    规则: 每天完成签到后送 1 次免费机会，用尽后每次抽奖扣 point_cost 矿石。
    """
    r = api(page, "GET", "/growth_api/v1/lottery_config/get")
    if not r or r.get("err_no") != 0:
        return None, None
    d = r.get("data") or {}
    if not isinstance(d, dict) or d.get("free_count") is None:
        return None, None
    try:
        return int(d["free_count"]), int(d.get("point_cost") or DEFAULT_POINT_COST)
    except (TypeError, ValueError):
        return None, None


def is_signed_today(page):
    """今日是否已签到（用于区分"没签到所以没次数"和"次数已用完"）"""
    r = api(page, "GET", "/growth_api/v1/get_today_status")
    return bool(r and r.get("err_no") == 0 and r.get("data"))


def do_checkin(page, result):
    """签到部分"""
    print("正在打开掘金签到页...")
    page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)

    status = api(page, "GET", "/growth_api/v1/get_today_status")
    if not status or status.get("error"):
        msg = f"签到状态查询失败: {(status or {}).get('error') or '无响应'}"
        result["status"] = msg
        print(msg)
        notify_once("checkin_api_fail", "掘金签到异常", msg + "，请稍后重试")
        return False
    if status.get("err_no") != 0:
        result["status"] = f"登录状态异常: {status.get('err_msg')}，请重新登录"
        print(result["status"])
        notify_once("checkin_cookie", "掘金签到异常", "登录状态（cookie）已过期，请到网页看板重新登录")
        return False

    if status.get("data"):
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


def lottery_modal_text(page):
    """当前可见弹窗文本（中奖提示 / 风控提示）。
    用计算样式 + 尺寸判断可见性，offsetParent 对 position:fixed 弹窗会是 null。"""
    return page.evaluate("""() => {
        const els = Array.from(document.querySelectorAll('[class*=modal]')).filter(e => {
            const s = getComputedStyle(e), r = e.getBoundingClientRect();
            return s.display !== 'none' && s.visibility !== 'hidden' &&
                   r.width > 0 && r.height > 0 && (e.innerText || '').trim();
        });
        return els.length ? els[els.length - 1].innerText.trim().slice(0, 300) : '';
    }""")


def lottery_close_modal(page):
    """关掉中奖/风控弹窗，避免遮住下一次点击"""
    for label in ("收下奖励", "我知道了", "确定"):
        try:
            loc = page.get_by_text(label, exact=False)
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=2500)
                page.wait_for_timeout(400)
                return True
        except Exception:
            continue
    return False


def do_lottery(page, result, use_points=False, max_paid=0):
    """抽奖部分：严格按接口报的免费次数抽；use_points=True 时才允许矿石补抽。"""
    print("正在打开掘金抽奖页...")
    page.goto(LOTTERY_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)

    free_cnt, point_cost = get_lottery_config(page)
    points_before = get_points(page)
    print(f"剩余免费次数: {free_cnt if free_cnt is not None else '读取失败'}；单次补抽消耗: {point_cost} 矿石")
    if points_before is not None:
        print(f"当前矿石: {points_before}")

    result.update({
        "free_count_before": free_cnt,
        "point_cost": point_cost,
        "points_before": points_before,
        "drew_free": 0,
        "drew_paid": 0,
        "prizes": [],
    })

    # 读不到次数时绝不能"试着抽一次"——免费次数用尽后那一次会直接扣 200 矿石
    if free_cnt is None:
        msg = "抽奖配置读取失败，为安全起见未抽奖（不消耗矿石）"
        result["lottery"] = msg
        result["status"] = result.get("status") or msg
        result["points"] = points_before
        print(msg)
        notify_once("lottery_config_fail", "掘金抽奖异常", msg + "，请检查登录态或稍后重试")
        return

    prizes, drew_free, drew_paid = [], 0, 0

    def draw_once(allow_paid=False):
        """点页面自己的「单抽」按钮完成一次抽奖，返回 (奖品名, 错误信息)。

        不再手搓 API：掘金 draw 接口需要页面动态生成的 msToken/a_bogus 签名
        和 x-secsdk-csrf-token 请求头，脚本直接 POST 会被静默拒绝（HTTP 200 空响应）。
        页面按钮自己会带齐这些。点击前必须用「免费」字样二次确认，避免误点
        「单抽 200」「十连抽 2000」扣掉矿石。
        """
        btn = page.locator("#turntable-item-0")
        if not btn.count():
            return None, "页面找不到单抽按钮（掘金页面可能改版）"
        try:
            label = (btn.inner_text() or "").strip().replace("\n", " ")
        except Exception as e:
            return None, f"读取抽奖按钮失败: {e}"
        if not allow_paid and "免费" not in label:
            return None, f"单抽按钮显示「{label}」而非免费，已跳过（不点击，避免扣矿石）"
        try:
            btn.click(timeout=10000)
        except Exception as e:
            return None, f"点击抽奖按钮失败: {(str(e).splitlines() or [repr(e)])[0][:120]}"
        # 转盘有动画，轮询等结果弹窗出现（最多约 10 秒）
        modal = ""
        for _ in range(12):
            page.wait_for_timeout(800)
            modal = lottery_modal_text(page)
            if modal and ("恭喜抽中" in modal or "当前操作失败" in modal or "前往申诉" in modal):
                break
        if "当前操作失败" in modal or "前往申诉" in modal:
            lottery_close_modal(page)
            return None, f"掘金风控提示: {modal[:80]}"
        if not modal:
            lottery_close_modal(page)
            return None, "抽奖结果弹窗未出现（页面可能改版或请求被拦截）"
        m = re.search(r"恭喜抽中\s*([^\n]+)", modal)
        prize = m.group(1).strip() if m else ""
        if not prize and modal:
            prize = modal.splitlines()[0].strip()
        lottery_close_modal(page)
        return (prize or "抽奖完成（未读到奖品名）"), None

    # ---- 免费抽奖：总次数不超过刚查到的 free_cnt，且每抽之前再确认一次还有次数 ----
    planned = min(free_cnt, MAX_FREE_DRAWS)
    while drew_free < planned:
        cur_free, _ = get_lottery_config(page)
        if cur_free is None:
            print("免费次数复查失败，停止抽奖（不消耗矿石）")
            break
        if cur_free <= 0:
            print("免费次数已用完，停止抽奖")
            break
        prize, err = draw_once()
        if prize is None:
            print(f"本次未抽奖: {err}")
            result["lottery_error"] = err
            safe_skip = ("而非免费" in err) or ("避免扣矿石" in err)
            if drew_free == 0 and not safe_skip:
                notify_once("lottery_draw_fail", "掘金抽奖异常", f"免费抽奖失败: {err}")
            break
        drew_free += 1
        prizes.append(prize)
        print(f"免费抽中: {prize}")
        # 护栏：免费阶段不该扣矿石；一旦减少一个 point_cost 立即刹车并通知
        after_one = get_points(page)
        if (points_before is not None and after_one is not None
                and after_one <= points_before - point_cost):
            drop = points_before - after_one
            msg = f"检测到矿石异常减少 {drop}，已立即停止抽奖"
            print(msg)
            notify_once("lottery_unexpected_cost", "掘金抽奖异常", msg + "，请到掘金页面核对")
            break
        page.wait_for_timeout(1500)  # 抽奖冷却，防连点被风控

    # ---- 矿石补抽（仅当看板开关/命令行 --use-points 明确开启）----
    if use_points:
        paid_limit = max_paid if max_paid > 0 else MAX_PAID_DRAWS_SAFE
        while drew_paid < paid_limit:
            cur = get_points(page)
            if cur is not None and cur < point_cost:
                print(f"矿石不足({cur} < {point_cost})，停止补抽")
                break
            prize, err = draw_once(allow_paid=True)
            if prize is None:
                print(f"矿石补抽停止: {err}")
                break
            drew_paid += 1
            prizes.append(prize)
            print(f"矿石抽中: {prize}")
            page.wait_for_timeout(1500)

    points_after = get_points(page)
    if drew_free or drew_paid:
        parts = []
        if drew_free:
            parts.append(f"免费 {drew_free} 次")
        if drew_paid:
            parts.append(f"矿石补抽 {drew_paid} 次")
        result["lottery"] = ("抽奖完成[" + "、".join(parts) + "]: " +
                             ("、".join(prizes) if prizes else "无结果"))
        result["status"] = result.get("status") or "抽奖完成"
    else:
        err = result.get("lottery_error") or ""
        suffix = "（未抽奖，不消耗矿石）"
        if free_cnt == 0:
            hint = ("今日免费次数已用完" if is_signed_today(page)
                    else "今日尚未签到：完成每日签到后才有 1 次免费抽奖机会，请点「签到 + 抽奖」")
        elif "而非免费" in err or "避免扣矿石" in err:
            # 护栏主动拒绝，属于安全行为，不要写成"失败"吓人
            hint = "页面按钮不是免费抽奖，已安全跳过（不点击，避免扣矿石）"
            suffix = ""
        else:
            hint = f"页面抽奖未成功（{err or '详见运行日志'}）"
        result["lottery"] = hint + suffix
        result["status"] = result.get("status") or hint
        print(result["lottery"])

    result["points"] = points_after
    result["prizes"] = prizes
    result["drew_free"] = drew_free
    result["drew_paid"] = drew_paid
    if points_after is not None:
        print(f"剩余矿石: {points_after}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["all", "checkin", "lottery"], default="all",
                        help="all=签到+抽奖(默认), checkin=只签到, lottery=只抽奖")
    parser.add_argument("--use-points", dest="use_points", action="store_true",
                        help="免费次数用完后允许用矿石补抽（默认关闭, 绝不花积分）")
    parser.add_argument("--max-paid", dest="max_paid", type=int, default=0,
                        help="矿石补抽的最大次数, 0=使用安全上限")
    args = parser.parse_args()

    result = {
        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": args.mode,
        "status": "",  # 空串，让仅抽奖模式能正确写入抽奖结果说明
    }

    if not os.path.exists(AUTH_PATH):
        result["status"] = "未找到登录状态，请到网页看板点「保存掘金登录」"
        print(result["status"])
        notify_once("checkin_no_auth", "掘金签到异常", "未找到登录状态文件，请到网页看板重新登录")
        append_log(result)
        return

    with open(AUTH_PATH, "r", encoding="utf-8") as f:
        auth = json.load(f)

    try:
        with sync_playwright() as p:
            # 优先 Playwright 自带内核，缺失时自动用本机 Chrome / Edge
            browser = launch_playwright_browser(p, headless=True)
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
                        return

                if args.mode in ("all", "lottery"):
                    do_lottery(page, result, use_points=args.use_points, max_paid=args.max_paid)

                # 仅签到模式也补一份积分
                if args.mode == "checkin":
                    pts = get_points(page)
                    if pts is not None:
                        result["points"] = pts
            finally:
                browser.close()
    except Exception as e:
        result["status"] = f"脚本执行失败: {e}"
        print(result["status"])
        notify_once("checkin_crash", "掘金签到脚本报错", str(e)[:150])
    finally:
        append_log(result)
        print(f"\n记录已保存: {LOG_PATH}")


if __name__ == "__main__":
    main()
