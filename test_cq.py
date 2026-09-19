"""CQ 码归一化 / QQ 表情复读 / 表情短语统计 测试脚本。

直接运行：
    python test_cq.py

无需启动 WS / NapCat / journalctl：
- cqtext.normalize 单元测试（真实商城大表情 CQ 格式）
- RepeatDetector 表情复读测试
- handler 端到端：/统计发言 + 表情 → 归一化追踪；表情消息计入统计；/表情 查询
退出码：0 = 全部通过，1 = 有失败。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_TMP_DATA = tempfile.mkdtemp(prefix="yuki_cq_test_")
os.environ["BOT_DATA_DIR"] = _TMP_DATA
os.environ["BOT_CONFIG_PATH"] = os.path.join(_TMP_DATA, "config.json")
os.environ["BOT_ADMIN_QQ"] = "1001"

import cqtext  # noqa: E402
import handler as handler_mod  # noqa: E402
from handler import MessageHandler  # noqa: E402
from repeat import RepeatDetector  # noqa: E402

SELF = 99999
GROUP = 777
ADMIN = 1001

_passed = 0
_failed = 0

# NapCat 商城大表情的真实 CQ 形态（raw 带转义 JSON，逗号转义为 &#44;）
FACE_BAIXIE = (
    '[CQ:face,id=297,raw={"faceIndex":297&#44;"faceText":"/拜谢"&#44;"faceType":2'
    '&#44;"packId":null&#44;"stickerId":null&#44;"sourceType":null&#44;"resultId":null'
    '&#44;"chainCount":null}]'
)
FACE_JINGYA = (
    '[CQ:face,id=187,raw={"faceIndex":187&#44;"faceText":"/惊讶"&#44;"faceType":1'
    '&#44;"packId":null&#44;"chainCount":null}]'
)
FACE_PLAIN = "[CQ:face,id=4]"  # 普通小黄脸（无 raw 字段）


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


class FakeWS:
    closed = False

    def __init__(self) -> None:
        self.sent: list = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


class FakeOB:
    def __init__(self) -> None:
        self.ws = FakeWS()


class FakeRenderer:
    def __init__(self) -> None:
        self.calls: list = []

    async def render_phrase_stats(self, **kw):
        self.calls.append(kw)
        return f"img-{kw.get('phrase')}"

    async def render_help(self, features=None, admin=False):
        return "img-help"


def make_handler() -> MessageHandler:
    return MessageHandler(FakeOB(), renderer=None, http_session=None, data_dir=_TMP_DATA)


def reply_texts(h: MessageHandler) -> list:
    return [a["params"]["message"] for a in h.ob.ws.sent if a.get("action") == "send_group_msg"]


async def send(h: MessageHandler, uid: int, text: str) -> None:
    ev = {
        "post_type": "message", "message_type": "group", "sub_type": "normal",
        "self_id": SELF, "user_id": uid, "group_id": GROUP,
        "raw_message": text, "message": text,
        "sender": {"nickname": f"u{uid}", "user_id": uid},
        "message_id": len(h.ob.ws.sent) + 1, "time": time.time(),
    }
    await h.handle_event(ev)


def main() -> None:
    global _passed, _failed
    print("== cqtext.normalize 单元测试 ==")
    step("商城大表情 → [表情:拜谢]", cqtext.normalize(FACE_BAIXIE) == "[表情:拜谢]",
         f"实际 {cqtext.normalize(FACE_BAIXIE)!r}")
    step("普通小黄脸（无 raw）→ [表情:4]", cqtext.normalize(FACE_PLAIN) == "[表情:4]",
         f"实际 {cqtext.normalize(FACE_PLAIN)!r}")
    step("纯文本原样保留", cqtext.normalize("不赖") == "不赖",
         f"实际 {cqtext.normalize('不赖')!r}")
    step("文本+表情混合", cqtext.normalize(f"哈哈{FACE_BAIXIE}") == "哈哈[表情:拜谢]",
         f"实际 {cqtext.normalize('哈哈' + FACE_BAIXIE)!r}")
    step("图片 → [图片]", cqtext.normalize("[CQ:image,file=abc.jpg,url=https://x]") == "[图片]",
         f"实际 {cqtext.normalize('[CQ:image,file=abc.jpg,url=https://x]')!r}")
    step("@ → [@123]", cqtext.normalize("[CQ:at,qq=123]") == "[@123]",
         f"实际 {cqtext.normalize('[CQ:at,qq=123]')!r}")
    step("无 CQ 快路径", cqtext.normalize("") == "" and cqtext.normalize("大家好") == "大家好")
    step("has_non_face_cq：表情=False 图片=True",
         cqtext.has_non_face_cq(FACE_BAIXIE) is False
         and cqtext.has_non_face_cq("[CQ:image,file=x]") is True)

    print("\n== 复读机：QQ 表情可触发 ==")
    det = RepeatDetector()
    r1 = det.check_and_trigger(GROUP, "A", SELF, FACE_BAIXIE)
    r2 = det.check_and_trigger(GROUP, "B", SELF, FACE_BAIXIE)
    r3 = det.check_and_trigger(GROUP, "C", SELF, FACE_BAIXIE)
    step("3 人发同一个拜谢表情 → 第 3 人触发复读", r1 is None and r2 is None and r3 == FACE_BAIXIE,
         f"{r1!r} {r2!r} {r3!r}")
    step("复读返回原始 CQ 码（表情原样复现）", r3 is not None and r3.startswith("[CQ:face,id=297"),
         f"{r3!r}")

    det2 = RepeatDetector()
    step("拜谢 / 惊讶 是不同文本（重置波次）",
         det2.check_and_trigger(GROUP, "A", SELF, FACE_BAIXIE) is None
         and det2.check_and_trigger(GROUP, "B", SELF, FACE_JINGYA) is None
         and det2.check_and_trigger(GROUP, "C", SELF, FACE_BAIXIE) is None
         and det2.check_and_trigger(GROUP, "D", SELF, FACE_JINGYA) is None)
    det3 = RepeatDetector()
    step("表情+文本混合参与复读（同句判定按归一化）",
         det3.check_and_trigger(GROUP, "A", SELF, f"哈哈哈{FACE_BAIXIE}") is None
         and det3.check_and_trigger(GROUP, "B", SELF, f"哈哈哈{FACE_BAIXIE}") is None
         and det3.check_and_trigger(GROUP, "C", SELF, f"哈哈哈{FACE_BAIXIE}") == f"哈哈哈{FACE_BAIXIE}")
    det4 = RepeatDetector()
    step("图片消息仍不参与复读",
         det4.check_and_trigger(GROUP, "A", SELF, "[CQ:image,file=x.jpg]") is None
         and det4.check_and_trigger(GROUP, "B", SELF, "[CQ:image,file=x.jpg]") is None
         and det4.check_and_trigger(GROUP, "C", SELF, "[CQ:image,file=x.jpg]") is None)
    det5 = RepeatDetector()
    step("命令仍不参与复读", det5.check_and_trigger(GROUP, "A", SELF, "/签到") is None)

    print("\n== 短语统计：表情追踪全链路 ==")

    async def amain() -> None:
        h = make_handler()
        # 1) 管理员 /统计发言 + 拜谢表情 → 存储归一化 token
        await send(h, ADMIN, f"/统计发言 {FACE_BAIXIE}")
        t = reply_texts(h)[-1]
        step("/统计发言 + 表情 → 回复含 [表情:拜谢]", "「[表情:拜谢]」" in t, f"实际回复 {t[:50]}")
        step("追踪列表已归一化", h._tracked_phrases == ["[表情:拜谢]"],
             f"实际 {h._tracked_phrases}")

        # 2) 群员发表情消息 → 计入该短语统计
        for uid in (11, 22):
            await send(h, uid, FACE_BAIXIE)
        async with h._phrase_stats_lock:
            stats = await h._load_json(h.phrase_stats_path)
        day = stats.get(str(GROUP), {}).get(time.strftime("%Y-%m-%d"), {})
        cnt = sum(u["count"] for u in day.get("[表情:拜谢]", {}).values())
        step("表情消息计入 [表情:拜谢] 统计", cnt == 2, f"实际 {cnt}")

        # 3) 管理员重复 /统计发言 + 同表情 → 提示已在统计中（而不是存第二份长 CQ）
        await send(h, ADMIN, f"/统计发言 {FACE_BAIXIE}")
        t = reply_texts(h)[-1]
        step("重复追踪提示「已经在统计中了」", "已经在统计中了" in t, f"实际回复 {t[:50]}")
        step("追踪列表没有重复项", h._tracked_phrases == ["[表情:拜谢]"], f"实际 {h._tracked_phrases}")

        # 4) /表情（斜杠 + 同一个表情）→ 命中追踪短语，渲染统计图
        h.renderer = FakeRenderer()
        await send(h, 11, f"/{FACE_BAIXIE}")
        fr = h.renderer
        ok = (len(fr.calls) == 1 and fr.calls[0].get("phrase") == "[表情:拜谢]"
              and "img-[表情:拜谢]" in reply_texts(h)[-1])
        step("/表情 → 命中并渲染统计图", ok, f"calls={fr.calls}")
        await send(h, 11, FACE_BAIXIE)  # 不带斜杠：只计数，不出图
        step("不带斜杠的纯表情消息只计数不出图", len(fr.calls) == 1, f"calls={len(fr.calls)}")

        # 5) /删除统计 + 表情 → 归一化匹配并删除
        await send(h, ADMIN, f"/删除统计 {FACE_BAIXIE}")
        t = reply_texts(h)[-1]
        step("/删除统计 + 表情 → 删除成功", "已停止统计" in t and h._tracked_phrases == [],
             f"实际回复 {t[:40]} 列表={h._tracked_phrases}")

        # 6) 历史数据自动迁移：tracked_phrases.json 里的整段 raw JSON → 启动时归一化
        with open(h.tracked_path, "w", encoding="utf-8") as f:
            json.dump([FACE_BAIXIE, "不赖", FACE_BAIXIE], f, ensure_ascii=False)
        h2 = make_handler()
        step("历史遗留长 CQ 短语启动时自动迁移+去重",
             h2._tracked_phrases == ["[表情:拜谢]", "不赖"], f"实际 {h2._tracked_phrases}")

        # 7) 普通文本短语统计不受影响
        await send(h2, ADMIN, "/统计发言 不赖")
        await send(h2, 11, f"今天心情不赖{FACE_BAIXIE}")
        async with h2._phrase_stats_lock:
            stats2 = await h2._load_json(h2.phrase_stats_path)
        day2 = stats2.get(str(GROUP), {}).get(time.strftime("%Y-%m-%d"), {})
        step("文本短语在含表情消息中正常匹配", day2.get("不赖", {}).get("11", {}).get("count") == 1,
             f"实际 {day2}")

    asyncio.run(amain())

    print("\n" + "=" * 44)
    if _failed == 0:
        print(f"全部通过  共 {_passed} 项")
        sys.exit(0)
    print(f"有失败  通过 {_passed}，失败 {_failed}")
    sys.exit(1)


if __name__ == "__main__":
    main()
