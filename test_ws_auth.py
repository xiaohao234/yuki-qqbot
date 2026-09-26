"""反向 WS 鉴权（BOT_ACCESS_TOKEN）测试脚本。

直接运行：
    python test_ws_auth.py

本地起真实 aiohttp 服务（随机端口），验证 OneBot v11 标准鉴权：
- token 已设置：无 token / 错 token（头或参数）→ 401 拒绝
- token 已设置：Bearer 头 / ?access_token= 正确 → 连入成功
- token 未设置：任何连接直接放行（兼容旧部署）
退出码：0 = 全部通过，1 = 有失败。
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_TMP_DATA = tempfile.mkdtemp(prefix="yuki_ws_auth_test_")
os.environ["BOT_DATA_DIR"] = _TMP_DATA
os.environ["BOT_CONFIG_PATH"] = os.path.join(_TMP_DATA, "config.json")

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

TOKEN = "sekret-123"
_passed = 0
_failed = 0


def step(label: str, ok: bool, detail: str = "") -> None:
    global _passed, _failed
    mark = "[OK]  " if ok else "[FAIL]"
    line = f"  {mark} {label}"
    if not ok and detail:
        line += f"  -> {detail}"
    print(line)
    if ok:
        _passed += 1
    else:
        _failed += 1


async def start_server() -> tuple:
    import main as yuki_main
    runner = web.AppRunner(yuki_main.build_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


async def try_connect(port: int, headers=None, query="") -> str:
    """尝试 WS 连入，返回 'ok' / '401' / 'other'。"""
    url = f"http://127.0.0.1:{port}/?{query}" if query else f"http://127.0.0.1:{port}/"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                url, headers=headers or {}, timeout=aiohttp.ClientWSTimeout(ws_close=3)
            ) as ws:
                await ws.close()
                return "ok"
    except aiohttp.WSServerHandshakeError as e:
        return "401" if e.status == 401 else f"other:{e.status}"
    except Exception as e:  # noqa: BLE001
        return f"other:{type(e).__name__}"


async def amain() -> None:
    # ---------- token 已设置 ----------
    os.environ["BOT_ACCESS_TOKEN"] = TOKEN
    import main as yuki_main
    runner, port = await start_server()
    print("== BOT_ACCESS_TOKEN 已设置 ==")
    step("无 token → 401 拒绝", await try_connect(port) == "401",
         f"实际 {await try_connect(port)}")
    step("错误 token（Bearer 头）→ 401 拒绝",
         await try_connect(port, {"Authorization": f"Bearer wrong-{TOKEN}"}) == "401")
    step("错误 token（query 参数）→ 401 拒绝",
         await try_connect(port, query=f"access_token=wrong") == "401")
    step("正确 Bearer 头 → 连入成功",
         await try_connect(port, {"Authorization": f"Bearer {TOKEN}"}) == "ok")
    step("正确 query 参数 → 连入成功",
         await try_connect(port, query=f"access_token={TOKEN}") == "ok")
    await runner.cleanup()

    # ---------- token 未设置（兼容旧行为）----------
    print("\n== BOT_ACCESS_TOKEN 未设置（留空）==")
    os.environ["BOT_ACCESS_TOKEN"] = ""
    yuki_main = importlib.reload(yuki_main)
    runner, port = await start_server()
    step("无 token → 直接放行", await try_connect(port) == "ok")
    step("随便一个 token → 也放行（不鉴权）",
         await try_connect(port, {"Authorization": "Bearer whatever"}) == "ok")
    await runner.cleanup()

    print("\n" + "=" * 44)
    if _failed == 0:
        print(f"全部通过  共 {_passed} 项")
        return
    print(f"有失败  通过 {_passed}，失败 {_failed}")
    sys.exit(1)


def main() -> None:
    sys.exit(asyncio.run(amain()) or 0)


if __name__ == "__main__":
    main()
