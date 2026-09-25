"""头像压缩 / LRU 缓存 / 并发去重 测试脚本（小内存设备内存优化的回归）。

直接运行：
    python test_avatar.py

无需网络与浏览器：_do_fetch_avatar 打桩，压缩用本地生成的图片。
退出码：0 = 全部通过，1 = 有失败。
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_TMP_DATA = tempfile.mkdtemp(prefix="yuki_avatar_test_")
os.environ["BOT_DATA_DIR"] = _TMP_DATA
os.environ["BOT_CONFIG_PATH"] = os.path.join(_TMP_DATA, "config.json")
os.environ["BOT_ADMIN_QQ"] = "1001"

import handler as handler_mod  # noqa: E402
from handler import MessageHandler  # noqa: E402

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


class FakeOB:
    def __init__(self) -> None:
        class _WS:
            closed = False
            async def send_json(self, payload):
                pass
        self.ws = _WS()


def make_png(size: int = 300) -> bytes:
    # 随机噪声图模拟真实头像（纯色图 PNG 本身极小，测不出压缩比）
    import random
    from PIL import Image
    random.seed(42)
    img = Image.frombytes(
        "RGB", (size, size),
        bytes(random.randrange(256) for _ in range(size * size * 3)),
    )
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def make_handler() -> MessageHandler:
    return MessageHandler(FakeOB(), renderer=None, http_session=None, data_dir=_TMP_DATA)


async def amain() -> None:
    print("== 头像压缩 ==")
    if not handler_mod._HAS_PIL:
        print("  [SKIP] 当前环境无 Pillow，跳过压缩测试")
        return
    big = make_png(300)
    small = MessageHandler._compress_avatar(big)
    from PIL import Image
    out = Image.open(io.BytesIO(small))
    step("300px PNG → ≤64px JPEG", max(out.size) <= 64 and out.format == "JPEG",
         f"实际 {out.size} {out.format}")
    step("压缩后体积大幅下降（几十 KB → 几 KB）", len(small) < len(big) / 3,
         f"{len(big)}B → {len(small)}B")
    step("坏数据兜底返回原样", MessageHandler._compress_avatar(b"not-an-image") == b"not-an-image")

    print("\n== LRU 缓存 + 并发去重 ==")
    h = make_handler()
    calls: list = []

    async def fake_fetch(user_id) -> str:  # 打桩：不真的发 HTTP
        calls.append(str(user_id))
        await asyncio.sleep(0.05)  # 模拟网络延迟，制造并发窗口
        return f"data:image/jpeg;base64,FAKE_{user_id}"

    h._do_fetch_avatar = fake_fetch

    # 并发去重：同一 uid 并发拉 5 次 → 只发 1 次 HTTP
    results = await asyncio.gather(*[h._fetch_avatar_b64(111) for _ in range(5)])
    step("同 uid 并发 5 次 → 只拉取 1 次", calls == ["111"], f"calls={calls}")
    step("并发结果一致", all(r == "data:image/jpeg;base64,FAKE_111" for r in results),
         f"results={results}")

    # 缓存命中：第二次拉取走缓存
    await h._fetch_avatar_b64(111)
    step("缓存命中不再发请求", calls == ["111"], f"calls={calls}")

    # 失败不缓存：返回空串且下次重试
    async def fake_fail(user_id) -> str:
        calls.append(f"fail-{user_id}")
        return ""
    h._do_fetch_avatar = fake_fail
    r1 = await h._fetch_avatar_b64(222)
    r2 = await h._fetch_avatar_b64(222)
    step("拉取失败不缓存、可重试", r1 == "" and r2 == "" and calls.count("fail-222") == 2,
         f"calls={calls}")

    # LRU 上限：超过 AVATAR_CACHE_MAX 淘汰最旧
    h2 = make_handler()
    n = [0]
    async def fake_seq(user_id) -> str:
        n[0] += 1
        return f"img-{n[0]}"
    h2._do_fetch_avatar = fake_seq
    old_max = handler_mod.AVATAR_CACHE_MAX
    handler_mod.AVATAR_CACHE_MAX = 3
    try:
        for uid in (1, 2, 3, 4):
            await h2._fetch_avatar_b64(uid)
        step("缓存满后淘汰最旧（保留 3 条）", len(h2._avatar_cache) == 3
             and "1" not in h2._avatar_cache and "4" in h2._avatar_cache,
             f"keys={list(h2._avatar_cache)}")
        await h2._fetch_avatar_b64(1)  # 被淘汰的 1 需要重新拉取
        step("被淘汰条目重新拉取", n[0] == 5, f"拉取次数={n[0]}")
    finally:
        handler_mod.AVATAR_CACHE_MAX = old_max

    # 命中刷新 LRU 顺序：频繁访问的 1 不被淘汰
    h3 = make_handler()
    h3._do_fetch_avatar = fake_seq
    handler_mod.AVATAR_CACHE_MAX = 3
    try:
        for uid in (1, 2, 3):
            await h3._fetch_avatar_b64(uid)
        await h3._fetch_avatar_b64(1)  # 刷新 1 的位置
        await h3._fetch_avatar_b64(4)  # 挤掉最久未用的 2
        step("命中刷新 LRU 顺序（挤掉 2 而非 1）",
             set(h3._avatar_cache) == {"1", "3", "4"}, f"keys={list(h3._avatar_cache)}")
    finally:
        handler_mod.AVATAR_CACHE_MAX = old_max


def main() -> None:
    asyncio.run(amain())
    print("\n" + "=" * 44)
    if _failed == 0:
        print(f"全部通过  共 {_passed} 项")
        sys.exit(0)
    print(f"有失败  通过 {_passed}，失败 {_failed}")
    sys.exit(1)


if __name__ == "__main__":
    main()
