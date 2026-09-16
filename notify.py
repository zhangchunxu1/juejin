# -*- coding: utf-8 -*-
"""Windows 桌面通知助手：异常时弹系统通知（气球提示，Win10/11 自动转为 Toast）。
零第三方依赖，通过 PowerShell 调系统 API；通知失败静默，不影响主流程。
"""
import datetime
import os
import subprocess

FLAG_DIR = r"D:\11\juejin\data\notify_flags"


def notify(title, message):
    """弹一条 Windows 桌面通知"""
    def ps_quote(s):
        return "'" + str(s).replace("'", "''")[:200].replace("\n", " ") + "'"

    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -AssemblyName System.Drawing; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon = [System.Drawing.SystemIcons]::Warning; "
        "$n.Visible = $true; "
        f"$n.ShowBalloonTip(8000, {ps_quote(title)}, {ps_quote(message)}, 'Warning'); "
        "Start-Sleep -Seconds 8; $n.Dispose()"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


def notify_once(key, title, message):
    """同一 key 每天只通知一次（防止定时任务反复弹窗轰炸）"""
    try:
        os.makedirs(FLAG_DIR, exist_ok=True)
        flag = os.path.join(FLAG_DIR, f"{datetime.date.today():%Y%m%d}_{key}.flag")
        if os.path.exists(flag):
            return
        notify(title, message)
        with open(flag, "w") as f:
            f.write("1")
    except Exception:
        pass


if __name__ == "__main__":
    notify("掘金工具箱", "这是一条测试通知，看到说明通知功能正常")
