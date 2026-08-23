"""Playwright 渲染封装：将 HTML 模板渲染为 Base64 PNG。

- 浏览器实例在程序启动时启动一次，复用 new_page()。
- 模板使用 Jinja2 渲染（适合循环生成 Top 10 列表）。
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List

from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger("renderer")


class Renderer:
    def __init__(self, browser: Any, templates_dir: str) -> None:
        self.browser = browser
        self.env = Environment(
            loader=FileSystemLoader(templates_dir),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def _render(self, name: str, **context: Any) -> str:
        tpl = self.env.get_template(name)
        return tpl.render(**context)

    async def render_html_to_png_b64(self, html: str, width: int = 720) -> str:
        """把完整 HTML 字符串渲染为整页 PNG，返回 Base64 字符串。"""
        page = await self.browser.new_page(
            viewport={"width": width, "height": 800},
            device_scale_factor=2,  # 2x 提升清晰度
        )
        try:
            # 等待网络空闲（头像以 data URI 内嵌，通常无需外部请求）
            await page.set_content(html, wait_until="networkidle")
            img_bytes = await page.screenshot(full_page=True, type="png")
            return base64.b64encode(img_bytes).decode("ascii")
        finally:
            await page.close()

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

    async def render_help(self, features: Dict[str, bool] | None = None) -> str:
        """渲染帮助菜单图片（指令列表静态写在 help.html 中，新增指令时同步更新模板）。

        features 为功能开关（handler.features），停用的功能组在图中置灰并标注"已停用"。
        """
        features = features or {}
        html = self._render("help.html", features=features)
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
