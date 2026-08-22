"""渲染签到/运势/排行样例图片到 ptest/ 目录。

复用项目里的 renderer.Renderer + templates/，用 Playwright 渲染为真实 PNG。
在线会拉取 QQ 头像；离线则用 SVG 占位头像，不影响出图。

运行：
    cd <项目根目录>
    python ptest/render_samples.py

产出：
    ptest/fortune_daji.png      （大吉 · 红色）
    ptest/fortune_zhongji.png   （中吉 · 橙色）
    ptest/fortune_xiong.png     （凶 · 灰色）
    ptest/stats_sample.png      （发言排行，金银铜高亮）
    ptest/user_stats_up.png     （个人统计 · 上升趋势）
    ptest/user_stats_down.png   （个人统计 · 下降趋势）
    ptest/user_stats_new.png    （个人统计 · 昨日无记录）
    ptest/phrase_stats.png      （特定发言统计 · 饼图+用户排行）
    ptest/help_sample.png       （帮助菜单 · 指令列表）
    ptest/trend_sample.png      （发言趋势 · 近7天双轴折线图）

依赖：pip install -r requirements.txt 且执行过 playwright install chromium
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
from datetime import date, timedelta
from typing import List

# 让脚本能 import 上级目录的 renderer
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
_TEMPLATES = os.path.join(_ROOT, "templates")
_OUT = os.path.dirname(os.path.abspath(__file__))  # ptest/

import aiohttp  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from renderer import Renderer  # noqa: E402


def placeholder_avatar(name: str) -> str:
    """SVG 占位头像（昵称首字 + 渐变底色），data URI 形式。"""
    ch = name[0] if name else "?"
    palette = ["#FFB7C5", "#C8A2C8", "#A8D8EA", "#FFDAC1", "#B5EAD7", "#FF9AA2"]
    color = palette[abs(hash(name)) % len(palette)]
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        f'<circle cx="50" cy="50" r="50" fill="{color}"/>'
        f'<text x="50" y="52" font-size="46" fill="#fff" text-anchor="middle" '
        f'dominant-baseline="central" font-family="sans-serif">{ch}</text></svg>'
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


async def fetch_avatar(session: aiohttp.ClientSession, user_id: int) -> str:
    """拉取 QQ 头像，返回 data URI；失败返回空串（调用方再回退占位）。"""
    url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                data = await resp.read()
                if data:
                    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
    except Exception:
        pass
    return ""


def save_png(b64_str: str, filename: str) -> None:
    path = os.path.join(_OUT, filename)
    with open(path, "wb") as f:
        f.write(base64.b64decode(b64_str))
    print(f"  [OK] 已生成 {filename}")


async def main() -> int:
    print(f"输出目录：{_OUT}")
    print("启动 Playwright / Chromium ……")
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(args=["--no-sandbox"])
    except Exception as e:
        print(f"[FAIL] 浏览器启动失败：{e}")
        print("       请先：pip install -r requirements.txt && playwright install chromium")
        return 1

    renderer = Renderer(browser, _TEMPLATES)
    session = aiohttp.ClientSession()
    today = date.today().isoformat()

    fortune_samples = [
        # (昵称, user_id, 运势, 颜色, 宜, 忌, 幸运数字, 连续天数, 文件名)
        ("星野绽放", 10001, "大吉", "#E74C3C",
         ["看番", "追番", "打音游"], ["断网", "催更"], 88, 7, "fortune_daji.png"),
        ("月下旅人", 10002, "中吉", "#F39C12",
         ["发弹幕", "刷推", "买谷"], ["抽卡沉船", "加班"], 42, 3, "fortune_zhongji.png"),
        ("初音未来", 10003, "凶", "#7F8C8D",
         ["补番", "听V家"], ["吃刀子", "被剧透", "弃坑"], 13, 1, "fortune_xiong.png"),
    ]

    try:
        # ---- 签到 / 运势 ----
        print("\n== 渲染签到/运势样例 ==")
        for nick, uid, fortune, color, yi, ji, lucky, streak, fname in fortune_samples:
            avatar = await fetch_avatar(session, uid)
            if not avatar:
                avatar = placeholder_avatar(nick)
            b64 = await renderer.render_fortune(
                nickname=nick, avatar_b64=avatar, date_str=today,
                fortune=fortune, fortune_color=color, yi=yi, ji=ji,
                lucky_number=lucky, streak=streak,
            )
            save_png(b64, fname)

        # ---- 发言排行 ----
        print("\n== 渲染发言排行李例 ==")
        rows = [
            (1, 10001, "星野绽放", 128, 21.3),
            (2, 10002, "月下旅人", 96, 16.0),
            (3, 10003, "初音未来", 80, 13.3),
            (4, 10004, "樱井桃华", 64, 10.7),
            (5, 10005, "雪之下", 50, 8.3),
            (6, 10006, "夜空中最亮的星", 42, 7.0),
            (7, 10007, "柠檬不酸", 30, 5.0),
            (8, 10008, "匿名水怪", 10, 1.7),
        ]
        top_users: List[dict] = []
        for rank, uid, nick, count, pct in rows:
            avatar = await fetch_avatar(session, uid)
            if not avatar:
                avatar = placeholder_avatar(nick)
            top_users.append({
                "rank": rank, "user_id": uid, "nickname": nick,
                "avatar_b64": avatar, "count": count, "percent": pct,
            })
        total_msgs = sum(r[3] for r in rows)
        b64 = await renderer.render_stats(123456789, "群 123456789", top_users, total_msgs)
        save_png(b64, "stats_sample.png")

        # ---- 个人发言统计（3 种趋势：上升 / 下降 / 昨日无记录）----
        print("\n== 渲染个人发言统计样例 ==")
        user_samples = [
            # (昵称, uid, 今日数, 昨日数, 今日群总, 昨日群总, trend, diff, change_pct, 文件名)
            ("星野绽放", 10001, 42, 28, 274, 210, "up", 14, 50.0, "user_stats_up.png"),
            ("月下旅人", 10002, 15, 38, 274, 210, "down", -23, 60.5, "user_stats_down.png"),
            ("初音未来", 10003, 30, 0, 274, 0, "new", 30, 100.0, "user_stats_new.png"),
        ]
        for nick, uid, today_cnt, yest_cnt, today_tot, yest_tot, trend, diff_val, chg_pct, fname in user_samples:
            avatar = await fetch_avatar(session, uid)
            if not avatar:
                avatar = placeholder_avatar(nick)
            today_p = round(today_cnt * 100 / today_tot, 1) if today_tot else 0.0
            yest_p = round(yest_cnt * 100 / yest_tot, 1) if yest_tot else 0.0
            b64 = await renderer.render_user_stats(
                nickname=nick,
                avatar_b64=avatar,
                target_uid=str(uid),
                today_count=today_cnt,
                yesterday_count=yest_cnt,
                today_total=today_tot,
                yesterday_total=yest_tot,
                today_pct=today_p,
                yesterday_pct=yest_p,
                trend=trend,
                diff=diff_val,
                change_pct=chg_pct,
            )
            save_png(b64, fname)

        # ---- 特定发言统计 ----
        print("\n== 渲染特定发言统计样例 ==")
        phrase_rows = [
            (1, 10001, "星野绽放", 12, 40.0),
            (2, 10002, "月下旅人", 8, 26.7),
            (3, 10003, "初音未来", 5, 16.7),
            (4, 10004, "樱井桃华", 3, 10.0),
            (5, 10005, "雪之下", 2, 6.7),
        ]
        phrase_users: List[dict] = []
        for rank, uid, nick, count, pct in phrase_rows:
            avatar = await fetch_avatar(session, uid)
            if not avatar:
                avatar = placeholder_avatar(nick)
            phrase_users.append({
                "rank": rank, "user_id": uid, "nickname": nick,
                "avatar_b64": avatar, "count": count, "percent": pct,
            })
        phrase_total = sum(r[3] for r in phrase_rows)
        group_total = 500
        group_pct = round(phrase_total * 100 / group_total, 1)
        b64 = await renderer.render_phrase_stats(
            phrase="不赖",
            top_users=phrase_users,
            total_phrase=phrase_total,
            group_total=group_total,
            group_pct=group_pct,
            date_label="今日",
        )
        save_png(b64, "phrase_stats.png")

        # ---- 帮助菜单 ----
        print("\n== 渲染帮助菜单样例 ==")
        b64 = await renderer.render_help()
        save_png(b64, "help_sample.png")

        # ---- 发言趋势（近 7 天折线图）----
        print("\n== 渲染发言趋势样例 ==")
        # (count, group_total)，按时间升序：6天前 → 今天；pct 由脚本计算
        trend_raw = [
            (52, 289), (68, 301), (35, 260), (90, 312), (74, 298), (58, 275), (46, 205),
        ]
        trend_days = []
        for i, (cnt, tot) in enumerate(trend_raw):
            d = date.today() - timedelta(days=6 - i)
            label = "今天" if i == 6 else ("昨天" if i == 5 else d.strftime("%m-%d"))
            trend_days.append({
                "label": label,
                "date_full": d.isoformat(),
                "weekday": "一二三四五六日"[d.weekday()],
                "count": cnt,
                "total": tot,
                "pct": round(cnt * 100 / tot, 1),
            })
        counts = [d["count"] for d in trend_days]
        pcts = [d["pct"] for d in trend_days]
        peak_i = counts.index(max(counts))
        avatar = await fetch_avatar(session, 10001)
        if not avatar:
            avatar = placeholder_avatar("星野绽放")
        b64 = await renderer.render_trend(
            nickname="星野绽放",
            avatar_b64=avatar,
            target_uid="10001",
            days=trend_days,
            total_count=sum(counts),
            avg_count=round(sum(counts) / len(counts), 1),
            peak_count=max(counts),
            peak_label=trend_days[peak_i]["date_full"],
            avg_pct=round(sum(pcts) / len(pcts), 1),
            today_count=counts[-1],
            today_pct=pcts[-1],
        )
        save_png(b64, "trend_sample.png")

        print("\n全部完成 ✅  打开 ptest/ 查看图片")
        return 0
    except Exception as e:
        print(f"\n[FAIL] 渲染出错：{e}")
        return 1
    finally:
        await session.close()
        await browser.close()
        await pw.stop()


def run() -> None:
    sys.exit(asyncio.run(main()))


if __name__ == "__main__":
    run()
