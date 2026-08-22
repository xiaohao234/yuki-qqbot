"""程序入口：aiohttp WebSocket Server。

- 监听 ws://127.0.0.1:8082，接受 NapCat 的反向 WebSocket 连接。
- 启动时初始化 aiohttp ClientSession 与 Playwright 浏览器（复用）。
- 每条收到的上报事件交给 handler 处理（并发处理，互不阻塞）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import aiohttp
from aiohttp import web, WSMsgType
from playwright.async_api import async_playwright

import onebot
from handler import MessageHandler
from renderer import Renderer

# 日志修复：守护进程（宝塔/1panel）把 stdout 归入"输出日志"、stderr 归入"错误日志"，
# 而 Python logging 默认写 stderr → 普通日志全进了错误日志。
# 改为：logging 显式写 stdout；并开启行缓冲（无 TTY 时 Python 默认全缓冲，日志会卡住不刷）。
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("yuki")

HOST = os.environ.get("BOT_HOST", "127.0.0.1")
PORT = int(os.environ.get("BOT_PORT", "8082"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 数据目录可经 BOT_DATA_DIR 覆盖（测试时指向临时目录，避免污染真实数据）
DATA_DIR = os.environ.get("BOT_DATA_DIR") or os.path.join(BASE_DIR, "data")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

# 全局状态
ob_conn = onebot.OneBotConnection()
handler: MessageHandler  # type: ignore
_renderer: Renderer
_http_session: aiohttp.ClientSession
_playwright = None
_browser = None
# 后台事件任务的强引用集合（防止 task 执行中被 GC 回收；完成后自动移除，不会无限增长）
_pending_tasks: set = set()


async def _on_startup(app: web.Application) -> None:
    global _http_session, _playwright, _browser, _renderer, handler
    os.makedirs(DATA_DIR, exist_ok=True)

    _http_session = aiohttp.ClientSession()
    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(args=["--no-sandbox"])

    _renderer = Renderer(_browser, TEMPLATES_DIR)
    handler = MessageHandler(ob_conn, _renderer, _http_session, DATA_DIR)

    logger.info("Yuki 已启动，监听 ws://%s:%s （等待 NapCat 反向连接）", HOST, PORT)


async def _on_cleanup(app: web.Application) -> None:
    global _http_session, _browser, _playwright
    logger.info("正在关闭资源...")
    if _browser is not None:
        await _browser.close()
    if _playwright is not None:
        await _playwright.stop()
    if _http_session is not None:
        await _http_session.close()
    logger.info("已关闭，再见~")


async def _handle_event_safe(data: dict) -> None:
    """事件处理包装：捕获所有异常，避免任务崩溃影响主循环。"""
    try:
        await handler.handle_event(data)
    except Exception as e:
        logger.exception("处理事件异常: %s", e)


def _spawn_task(data: dict) -> None:
    """创建事件处理任务并保存强引用，完成后从集合移除（防 GC / 防泄漏两不误）。"""
    task = asyncio.create_task(_handle_event_safe(data))
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    """NapCat 反向 WebSocket 入口。"""
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=0)
    await ws.prepare(request)
    peer = request.remote
    logger.info("NapCat 已连接：%s", peer)
    ob_conn.set_connection(ws)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = msg.json()
                except Exception:
                    logger.warning("收到非 JSON 文本: %s", msg.data[:200])
                    continue
                # 并发处理：每条事件独立 task，慢操作（渲染/拉头像）不阻塞后续消息
                _spawn_task(data)
            elif msg.type == WSMsgType.ERROR:
                logger.error("WS 错误: %s", ws.exception())
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                break
    finally:
        ob_conn.clear_connection(ws)
        logger.info("NapCat 已断开：%s（等待重连）", peer)
    return ws


def build_app() -> web.Application:
    app = web.Application()
    # 兼容 NapCat 配置根路径或 /ws
    app.router.add_get("/", ws_handler)
    app.router.add_get("/ws", ws_handler)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


def main() -> None:
    app = build_app()
    web.run_app(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
