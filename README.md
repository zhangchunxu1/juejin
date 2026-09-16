# 掘金工具集使用手册

目录：`D:\11\juejin\`

```
D:\11\juejin\
├── juejin_pins.py        # 沸点热门抓取（沸点+评论 → MySQL + Excel）
├── juejin_checkin.py     # 每日自动签到 + 免费抽奖
├── server\
│   ├── app.py            # Flask 后端 API（端口 5000）
│   └── index.html        # Vue3 + Element Plus 数据看板
└── data\
    ├── auth_juejin.json  # 掘金登录态（cookie，失效需重新登录）
    ├── checkin_log.json  # 签到历史记录
    └── 沸点热门_*.xlsx    # 历次抓取的 Excel 导出
```

## 环境准备（只需一次）

```cmd
pip install requests pandas openpyxl pymysql flask playwright
python -m playwright install chromium
```

## 首次登录（只需一次，cookie 失效后再做）

```cmd
python D:\11\scripts\login_save.py juejin
```

会弹出浏览器，手动登录掘金后回车，登录态保存到 `data\auth_juejin.json`。

## 日常使用

### 1. 抓取沸点（cmd 直接跑）

```cmd
set MYSQL_PASSWORD=zc123456 && python D:\11\juejin\juejin_pins.py
```

| 参数 | 说明 | 示例 |
|------|------|------|
| `--total N` | 抓取沸点条数，默认 50 | `--total 100` |
| `--comments N` | 每帖评论数，默认 20；`0`=不抓评论；`-1`=全部评论（自动含楼中楼回复） | `--comments -1` |

结果：自动写入 MySQL + 导出 Excel 到 `data\沸点热门_时间戳.xlsx`。

### 2. 数据看板（网页可视化）

```cmd
set MYSQL_PASSWORD=zc123456 && python D:\11\juejin\server\app.py
```

浏览器访问 **http://127.0.0.1:5000**

- 统计卡片、点赞 TOP10 / 活跃作者 TOP10 图表
- 沸点列表：搜索、排序、分页、点"评论数"查看评论
- **抓取控制面板**：网页上直接设置条数并启动抓取，实时看日志，完成自动刷新

注意：cmd 窗口关掉服务就停了。

### 3. 每日签到 + 抽奖

```cmd
python D:\11\juejin\juejin_checkin.py
```

自动完成签到、幸运大转盘免费抽奖，记录写入 `data\checkin_log.json`。

挂 Windows 计划任务每天自动跑（PowerShell 执行一次）：

```powershell
schtasks /create /tn "JuejinCheckin" /tr "python D:\11\juejin\juejin_checkin.py" /sc daily /st 09:00
```

## 数据库说明

- 库：`juejin`，本机 `127.0.0.1:3306`，账号 `root`
- **密码不落盘**，每次运行用 `set MYSQL_PASSWORD=zc123456 &&` 传入
- 表结构：

| 表 | 唯一键 | 内容 |
|----|--------|------|
| `pin` | `msg_id` | 沸点：作者/内容/点赞/评论数/发布时间/链接/抓取时间 |
| `pin_comment` | `comment_id` | 评论：所属 msg_id/作者/内容/点赞/回复数/时间 |

- **沸点表**重复抓取不会产生重复数据（唯一键 + upsert，只刷新动态字段）
- **评论表**为替换语义：每次抓到的沸点，其旧评论先整体删除再写入新数据，保证与线上当前状态一致（不会残留已删除的旧评论）
- 沸点卡片显示的"评论数" = 顶层评论 + 楼中楼回复；`--comments -1` 会两者都抓（数量可能略少于显示值，差额为已被删除的评论）
- 区分新旧数据用两个时间字段：

```sql
-- 某次新抓到的沸点
SELECT * FROM juejin.pin WHERE first_crawl_time >= '2026-09-15 16:00:00';
-- 老数据（被抓到过多次，点赞等有更新）
SELECT * FROM juejin.pin WHERE first_crawl_time < crawl_time;
```

## 常见问题

| 问题 | 解决 |
|------|------|
| 提示未找到登录状态 | 重新运行 `python D:\11\scripts\login_save.py juejin` |
| 接口报错/限流 | 把脚本里 `WORKERS`（默认 5）调小、`PAGE_DELAY` 调大 |
| 看板图表不显示 | 页面依赖 CDN（Vue/Element/ECharts），需联网 |
| 签到失败 | cookie 过期，重新登录 |
