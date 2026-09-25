"""Playwright 渲染封装：将 HTML 模板渲染为 Base64 PNG。

生命周期（针对小内存设备的懒启动 + 闲置回收，消除常驻 Chromium 的空载内存）：
- 懒启动：首次渲染时才启动 Playwright 驱动 + Chromium（冷启动约 20s，首图变慢可接受）；
- 闲置回收：连续 BOT_BROWSER_IDLE_SEC 秒（默认 900）无渲染任务则关闭 Chromium
  并停止驱动，空闲时段（尤其深夜）释放 450~550 MB；
- 每次渲染独立开页、用完即关（页面渲染进程内存随关闭归还系统）；
- 渲染并发闸门 _RENDER_CONCURRENCY=2：防止刷屏指令同时开页打爆内存；
- 每次渲染记录耗时日志，便于线上验证内存/性能优化效果。

传入外部 browser（测试脚本/特殊部署）时进入外部模式：不做懒启动与回收。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from typing import Any, Dict, List, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger("renderer")

# Chromium 启动参数：小内存设备加固（软渲染、限 V8 堆、不用 /dev/shm）
CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--js-flags=--max-old-space-size=256",
]
# 渲染并发闸门：同时最多开这么多页面
_RENDER_CONCURRENCY = 2
# 闲置回收秒数（环境变量 BOT_BROWSER_IDLE_SEC 可覆盖；0 = 不回收）
IDLE_SEC = int(os.environ.get("BOT_BROWSER_IDLE_SEC", "900"))


class Renderer:
    def __init__(self, templates_dir: str, browser: Any = None) -> None:
        self.env = Environment(
            loader=FileSystemLoader(templates_dir),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._external_browser = browser  # 外部传入的浏览器：生命周期由调用方管理
        self._playwright: Optional[Any] = None
        self._browser: Optional[Any] = None
        self._start_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(_RENDER_CONCURRENCY)
        self._last_render_ts = 0.0
        self._idle_task: Optional["asyncio.Task"] = None

    # ---------- 浏览器生命周期 ----------
    async def _ensure_browser(self) -> Any:
        """懒启动：确保 Chromium 可用（带锁防并发渲染时重复启动）。"""
        if self._external_browser is not None:
            return self._external_browser
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        async with self._start_lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            if self._playwright is None:
                from playwright.async_api import async_playwright

                logger.info("首次渲染：正在启动 Playwright 驱动…")
                self._playwright = await async_playwright().start()
            logger.info("正在启动 Chromium（懒启动，冷启动约 20s）…")
            self._browser = await self._playwright.chromium.launch(args=CHROMIUM_ARGS)
            logger.info("Chromium 已就绪")
            return self._browser

    def _schedule_idle_recycle(self) -> None:
        """渲染结束后重置闲置计时：IDLE_SEC 秒内无新渲染则回收整套浏览器。"""
        if self._external_browser is not None or IDLE_SEC <= 0:
            return
        self._last_render_ts = time.time()
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = asyncio.create_task(self._idle_recycler())

    async def _idle_recycler(self) -> None:
        try:
            while True:
                remaining = IDLE_SEC - (time.time() - self._last_render_ts)
                if remaining <= 0:
                    break
                await asyncio.sleep(remaining)
            logger.info("Chromium 已闲置 %d 秒，自动回收（下次渲染时冷启动）", IDLE_SEC)
            await self._shutdown()
        except asyncio.CancelledError:
            pass  # 闲置期间来了新渲染任务，重新计时

    async def _shutdown(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    async def close(self) -> None:
        """进程退出时调用：取消回收任务并释放浏览器/驱动（幂等）。"""
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        await self._shutdown()

    # ---------- 渲染 ----------
    def _render(self, name: str, **context: Any) -> str:
        tpl = self.env.get_template(name)
        return tpl.render(**context)

    async def render_html_to_png_b64(self, html: str, width: int = 760) -> str:
        """把完整 HTML 字符串渲染为整页 PNG，返回 Base64 字符串。

        width 默认 760：模板 card 宽 680 + body 左右 padding 28*2 = 736，
        视口必须 ≥736 才能让 margin:0 auto 生效（卡片水平居中），
        760 时左右各留 40px 对称留白。720 会导致 680px 卡片溢出、内容右偏 8px。
        """
        t0 = time.perf_counter()
        async with self._sem:  # 并发闸门：防止刷屏指令同时开页打爆内存
            try:
                browser = await self._ensure_browser()
                page = await browser.new_page(
                    viewport={"width": width, "height": 800},
                    device_scale_factor=2,  # 2x 提升清晰度
                )
                try:
                    # 等待网络空闲（头像以 data URI 内嵌，通常无需外部请求）
                    await page.set_content(html, wait_until="networkidle")
                    img_bytes = await page.screenshot(full_page=True, type="png")
                finally:
                    await page.close()
            finally:
                self._schedule_idle_recycle()  # 成功失败都重置闲置计时
        logger.info(
            "渲染完成：%.2f s，HTML %.1f KB", time.perf_counter() - t0, len(html) / 1024
        )
        return base64.b64encode(img_bytes).decode("ascii")

    async def render_stats(
        self, group_id: int, group_name: str, top_users: List[dict], total: int = 0,
        date_label: str = "今日",
    ) -> str:
        """渲染发言排行榜图片。

        top_users 元素结构：
            {rank, user_id, nickname, avatar_b64(data URI 或空串), count, percent}
        total 为本群总发言数，用于在顶部展示。
        date_label 为标题前缀，"今日" 或 "昨日"。
        """
        html = self._render(
            "stats.html", group_name=group_name, users=top_users, total=total,
            date_label=date_label,
        )
        return await self.render_html_to_png_b64(html)

    async def render_user_stats(
        self,
        nickname: str,
        avatar_b64: str,
        target_uid: str,
        today_count: int,
        yesterday_count: int,
        today_total: int,
        yesterday_total: int,
        today_pct: float,
        yesterday_pct: float,
        trend: str,
        diff: int,
        change_pct: float,
    ) -> str:
        """渲染个人发言统计图片（今日 vs 昨日 + 趋势 + 占群饼图）。

        trend: "up" / "down" / "same" / "new"
        diff: 今日 - 昨日 的条数差
        change_pct: 变化百分比（绝对值）
        """
        html = self._render(
            "user_stats.html",
            nickname=nickname,
            avatar_b64=avatar_b64,
            target_uid=target_uid,
            today_count=today_count,
            yesterday_count=yesterday_count,
            today_total=today_total,
            yesterday_total=yesterday_total,
            today_pct=today_pct,
            yesterday_pct=yesterday_pct,
            trend=trend,
            diff=diff,
            change_pct=change_pct,
        )
        return await self.render_html_to_png_b64(html)

    async def render_phrase_stats(
        self,
        phrase: str,
        top_users: List[dict],
        total_phrase: int,
        group_total: int,
        group_pct: float,
        date_label: str = "今日",
    ) -> str:
        """渲染特定发言统计图片（出现次数+用户排行+占群饼图）。

        top_users 元素结构：{rank, user_id, nickname, avatar_b64, count, percent}
        total_phrase: 该短语今日/昨日总出现次数
        group_total: 群今日/昨日总消息数
        group_pct: 该短语占群总消息的百分比
        """
        html = self._render(
            "phrase_stats.html",
            phrase=phrase,
            users=top_users,
            total_phrase=total_phrase,
            group_total=group_total,
            group_pct=group_pct,
            date_label=date_label,
        )
        return await self.render_html_to_png_b64(html)

    async def render_speed(
        self,
        window_label: str,
        top_users: List[dict],
        total: int,
        speed: float,
        active_users: int,
    ) -> str:
        """渲染发言速度图片（时间窗口内的速度概览 + 水群 Top10）。

        top_users 元素：{rank, user_id, nickname, avatar_b64, count, rate, percent}
        speed 为群总速度（条/分钟）；active_users 为窗口内发过言的人数。
        """
        html = self._render(
            "speed.html",
            window_label=window_label,
            users=top_users,
            total=total,
            speed=speed,
            active_users=active_users,
        )
        return await self.render_html_to_png_b64(html)

    async def render_fortune(
        self,
        nickname: str,
        avatar_b64: str,
        date_str: str,
        fortune: str,
        fortune_color: str,
        yi: List[str],
        ji: List[str],
        lucky_number: int,
        streak: int = 1,
    ) -> str:
        """渲染签到运势图片。"""
        html = self._render(
            "fortune.html",
            nickname=nickname,
            avatar_b64=avatar_b64,
            date_str=date_str,
            fortune=fortune,
            fortune_color=fortune_color,
            yi=yi,
            ji=ji,
            lucky_number=lucky_number,
            streak=streak,
        )
        return await self.render_html_to_png_b64(html)

    async def render_help(self, features: Dict[str, bool] | None = None, admin: bool = False) -> str:
        """渲染帮助菜单图片（指令列表静态写在 help.html 中，新增指令时同步更新模板）。

        features 为功能开关（handler.features），停用的功能组在图中置灰并标注"已停用"。
        admin=False 渲染用户版（隐藏管理员组）；admin=True 渲染管理员版（仅管理员指令）。
        """
        features = features or {}
        html = self._render("help.html", features=features, admin=admin)
        return await self.render_html_to_png_b64(html)

    @staticmethod
    def _build_trend_geometry(days: List[dict]) -> dict:
        """把每日数据换算成 SVG 折线图坐标（供 trend.html 直接渲染）。

        days 元素：{label, count, pct, ...}，按时间升序。
        返回：{points, area_path, pct_points, pts[{x,y,y_pct,...}], grid[], max_count, max_pct}
        """
        W, H = 620, 250
        PL, PR, PT, PB = 46, 610, 30, 196   # 绘图区左右上下的像素边界
        n = len(days)
        xs = [PL + i * (PR - PL) / (n - 1) for i in range(n)]

        max_count = max((d["count"] for d in days), default=0) or 1
        max_pct = max((d["pct"] for d in days), default=0) or 1

        def y_of(v: float, vmax: float) -> float:
            return PB - (v / vmax) * (PB - PT)

        ys = [y_of(d["count"], max_count) for d in days]
        ys_pct = [y_of(d["pct"], max_pct) for d in days]

        pts = []
        for i, d in enumerate(days):
            pts.append({
                **d, "x": round(xs[i], 1), "y": round(ys[i], 1),
                "y_pct": round(ys_pct[i], 1), "label_y": PB + 20,
                "value_y": round(ys[i] - 10, 1), "pct_y": round(ys_pct[i] - 9, 1),
            })

        points = " ".join(f"{round(x,1)},{round(y,1)}" for x, y in zip(xs, ys))
        pct_points = " ".join(f"{round(x,1)},{round(y,1)}" for x, y in zip(xs, ys_pct))
        seg = " ".join(f"L{round(x,1)},{round(y,1)}" for x, y in zip(xs, ys))
        area_path = f"M{round(xs[0],1)},{PB} {seg} L{round(xs[-1],1)},{PB} Z"

        grid = [
            {"y": round(y_of(v, max_count), 1), "label": (round(v, 1) if isinstance(v, float) and v % 1 else int(v))}
            for v in (0, max_count / 2, max_count)
        ]
        return {
            "w": W, "h": H, "pl": PL, "pr": PR, "pt": PT, "pb": PB,
            "points": points, "area_path": area_path, "pct_points": pct_points,
            "pts": pts, "grid": grid, "max_count": max_count, "max_pct": max_pct,
        }

    async def render_trend(
        self,
        nickname: str,
        avatar_b64: str,
        target_uid: str,
        days: List[dict],
        total_count: int,
        avg_count: float,
        peak_count: int,
        peak_label: str,
        avg_pct: float,
        today_count: int,
        today_pct: float,
    ) -> str:
        """渲染近 7 天发言趋势折线图（每日发言数主线 + 占群比例副线）。

        days 元素：{label, date_full, weekday, count, total, pct}，按时间升序。
        """
        geo = self._build_trend_geometry(days)
        html = self._render(
            "trend.html",
            nickname=nickname,
            avatar_b64=avatar_b64,
            target_uid=target_uid,
            days=days,
            geo=geo,
            total_count=total_count,
            avg_count=avg_count,
            peak_count=peak_count,
            peak_label=peak_label,
            avg_pct=avg_pct,
            today_count=today_count,
            today_pct=today_pct,
        )
        return await self.render_html_to_png_b64(html)
