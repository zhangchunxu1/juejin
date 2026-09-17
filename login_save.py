# -*- coding: utf-8 -*-
"""
掘金登录态保存工具（项目内自包含版）
打开浏览器 → 你手动登录掘金 → 脚本轮询检测登录成功 → 自动保存登录态并退出。
不用按回车，登录成功自动结束。

用法:
    python login_save.py            # 网页看板里点"保存登录"调用的也是它
    python login_save.py --wait 300 # 登录窗口最长等 300 秒（默认 180）
"""
import argparse
import datetime
import json
import os
import sys

from playwright.sync_api import sync_playwright

from jjconfig import DATA_DIR, AUTH_PATH

# Windows 控制台 GBK 兼容
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def save_auth():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait", type=int, default=180, help="等待登录的最长秒数")
    args = parser.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)

    print("即将打开浏览器，请在页面里登录掘金（扫码或账号密码均可）...")
    print(f"登录成功后本窗口会自动退出（最长等待 {args.wait} 秒）")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # 有头: 需要人手动登录
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
        )
        page = ctx.new_page()
        page.goto("https://juejin.cn", wait_until="domcontentloaded", timeout=60000)

        ok = False
        deadline = datetime.datetime.now() + datetime.timedelta(seconds=args.wait)
        while datetime.datetime.now() < deadline:
            page.wait_for_timeout(3000)
            try:
                state = page.evaluate(
                    """async () => {
                        try {
                            const r = await fetch('https://api.juejin.cn/user_api/v1/user/get', {
                                credentials: 'include'});
                            const j = await r.json();
                            return (j && j.err_no === 0 && j.data) ? j.data.user_name : null;
                        } catch (e) { return null; }
                    }"""
                )
            except Exception:
                state = None
            if state:
                auth = ctx.storage_state()
                payload = {
                    "saved_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "user_name": state,
                    "cookies": auth.get("origins", []) and auth.get("cookies", []),
                    **auth,
                }
                # storage_state() 返回 {cookies, origins}; 拍平存一份带说明的结构
                payload = {**auth, "saved_at": payload["saved_at"], "user_name": state}
                with open(AUTH_PATH, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                print(f"登录成功: {state}")
                print(f"登录态已保存: {AUTH_PATH}")
                ok = True
                break
            print("  还未登录，继续等待...（登录后自动检测）")

        browser.close()
        if not ok:
            print("超时未登录，未保存。重新运行本脚本再试。")
            sys.exit(1)


if __name__ == "__main__":
    save_auth()
