"""管理员系统指令测试脚本（#yukireboot / #log）。

直接运行：
    python test_admin.py

无需启动 WS / NapCat / Playwright / journalctl：
- 用 FakeWS 捕获 bot 回发的 send_group_msg
- monkeypatch 掉 journalctl 子进程调用与真正的重启动作（os.execv）
退出码：0 = 全部通过，1 = 有失败。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import tempfile
import time

# 环境变量必须在 import handler 之前设置（ADMIN_QQ 在 import 时读取）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_TMP_DATA = tempfile.mkdtemp(prefix="yuki_admin_test_")
os.environ["BOT_DATA_DIR"] = _TMP_DATA
os.environ["BOT_CONFIG_PATH"] = os.path.join(_TMP_DATA, "config.json")
os.environ["BOT_ADMIN_QQ"] = "1001"  # 管理员 QQ = 1001

import handler as handler_mod  # noqa: E402
from handler import MessageHandler  # noqa: E402

ADMIN = 1001
OTHER = 2002
GROUP = 123
SELF = 99999

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
    """只实现帮助菜单渲染的假 renderer，记录 admin 参数便于断言分发。"""

    def __init__(self) -> None:
        self.calls: list = []

    async def render_help(self, features=None, admin=False) -> str:
        self.calls.append(admin)
        return f"fakeimg-admin-{admin}"


def make_handler() -> MessageHandler:
    return MessageHandler(FakeOB(), renderer=None, http_session=None, data_dir=_TMP_DATA)


def reply_texts(h: MessageHandler) -> list:
    return [
        a["params"]["message"]
        for a in h.ob.ws.sent
        if a.get("action") == "send_group_msg"
    ]


async def send(h: MessageHandler, uid: int, text: str) -> None:
    ev = {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "self_id": SELF,
        "user_id": uid,
        "group_id": GROUP,
        "raw_message": text,
        "message": text,
        "sender": {"nickname": f"u{uid}", "user_id": uid},
        "message_id": len(h.ob.ws.sent) + 1,
        "time": time.time(),
    }
    await h.handle_event(ev)


def set_config(cfg: dict) -> None:
    with open(os.environ["BOT_CONFIG_PATH"], "w", encoding="utf-8") as f:
        json.dump(cfg, f)


async def amain() -> int:
    global handler_mod
    # ---------- 权限 ----------
    print("== 权限校验 ==")
    h = make_handler()
    await send(h, OTHER, "#log astr")
    step("非管理员 #log 被拒绝", reply_texts(h) == ["只有管理员才能使用此指令~"],
         f"实际回复 {reply_texts(h)}")
    h.ob.ws.sent.clear()
    await send(h, OTHER, "#yukireboot")
    step("非管理员 #yukireboot 被拒绝", reply_texts(h) == ["只有管理员才能使用此指令~"],
         f"实际回复 {reply_texts(h)}")

    # ---------- #log 参数解析 ----------
    print("\n== #log 参数解析 ==")
    h = make_handler()
    calls: list = []

    async def fake_journ(unit: str, num: int) -> str:
        calls.append((unit, num))
        return f"line1 {unit}\nline2 with [CQ:image,file=evil] inside"

    h._run_journalctl = fake_journ  # monkeypatch：不真跑 journalctl

    await send(h, ADMIN, "#log")
    t = reply_texts(h)[-1]
    step("#log 无参数 → 用法提示", t.startswith("用法：#log"), f"实际回复 {t[:30]}")
    await send(h, ADMIN, "#log xyz")
    t = reply_texts(h)[-1]
    step("#log 非法目标 → 提示", "不认识的目标" in t, f"实际回复 {t[:30]}")
    await send(h, ADMIN, "#log astr abc")
    t = reply_texts(h)[-1]
    step("#log 行数非数字 → 提示", "纯数字" in t, f"实际回复 {t[:30]}")
    await send(h, ADMIN, "#log astr 0")
    t = reply_texts(h)[-1]
    step("#log 行数 0 → 提示", "1~200" in t, f"实际回复 {t[:30]}")
    await send(h, ADMIN, "#log astr 500")
    t = reply_texts(h)[-1]
    step("#log 行数 500 → 提示", "1~200" in t, f"实际回复 {t[:30]}")
    await send(h, ADMIN, "#log astr 15 30")
    t = reply_texts(h)[-1]
    step("#log 多余参数 → 用法提示", t.startswith("用法：#log"), f"实际回复 {t[:30]}")

    await send(h, ADMIN, "#log astr")
    t = reply_texts(h)[-1]
    step("#log astr → astrbot 默认 15 行", calls[-1] == ("astrbot", 15) and "line2" in t,
         f"calls={calls[-1]} 回复={t[:40]}")
    step("日志中的 CQ 码被转义", "&#91;CQ:image" in t and "[CQ:image" not in t,
         f"回复片段={t[t.find('line2'):][:60]}")
    await send(h, ADMIN, "#log NC 30")
    step("#log NC 30 → napcat 30 行（大小写不敏感）", calls[-1] == ("napcat", 30),
         f"calls={calls[-1]}")
    await send(h, ADMIN, "#log n 1")
    step("#log n 1 → napcat 1 行", calls[-1] == ("napcat", 1), f"calls={calls[-1]}")

    # ---------- log 日志源配置（file / auto / journal）----------
    print("\n== log 日志源配置 ==")
    h2 = make_handler()
    calls2: list = []

    async def fake_journ2(unit: str, num: int) -> str:
        calls2.append(("journal", unit, num))
        return "from-journal"

    h2._run_journalctl = fake_journ2

    # file 模式：用真实 tail 读临时日志文件（不 monkeypatch，验证子进程全链路）
    log_fd, log_path = tempfile.mkstemp(prefix="yuki_test_log_", suffix=".log")
    with os.fdopen(log_fd, "w", encoding="utf-8") as f:
        for i in range(1, 101):
            f.write(f"line-{i:03d}\n")
    set_config({"log": {"mode": "file",
                        "files": {"astrbot": log_path, "napcat": ""},
                        "units": {"astrbot": "astrbot", "napcat": "napcat"}}})
    await send(h2, ADMIN, "#log astr 5")
    t = reply_texts(h2)[-1]
    step("file 模式 tail 读最后 5 行",
         "line-096" in t and "line-100" in t and "line-095" not in t, f"回复片段={t[:80]}")
    step("file 模式回复头带目标名", "📋 astrbot 最近 5 行日志" in t, f"实际回复 {t[:30]}")
    await send(h2, ADMIN, "#log astr")
    t = reply_texts(h2)[-1]
    step("file 模式默认 15 行", "line-086" in t and "line-085" not in t, f"回复片段={t[:60]}")

    # auto 模式：文件不存在回落 journalctl（有提示）
    set_config({"log": {"mode": "auto", "files": {"astrbot": "/nonexistent/path.log"}}})
    await send(h2, ADMIN, "#log astr")
    t = reply_texts(h2)[-1]
    step("auto 模式文件缺失回落 journalctl 并提示",
         calls2[-1] == ("journal", "astrbot", 15) and "回落 journalctl" in t and "from-journal" in t,
         f"calls2={calls2[-1]} 回复={t[:50]}")

    # auto 模式：文件存在 → 优先用文件
    set_config({"log": {"mode": "auto", "files": {"astrbot": log_path}}})
    await send(h2, ADMIN, "#log astr 3")
    t = reply_texts(h2)[-1]
    step("auto 模式文件存在优先用文件",
         "line-098" in t and "回落" not in t and calls2[-1] == ("journal", "astrbot", 15),
         f"回复={t[:60]} calls2={calls2[-1]}")

    # journal 模式：走 journalctl，单元名可配置
    set_config({"log": {"mode": "journal", "units": {"napcat": "napcat.service"}}})
    await send(h2, ADMIN, "#log nc 20")
    step("journal 模式用配置的单元名", calls2[-1] == ("journal", "napcat.service", 20),
         f"calls2={calls2[-1]}")

    # file 模式：文件不存在 → 明确报错（不静默回落）
    set_config({"log": {"mode": "file", "files": {"astrbot": "/nonexistent/x.log"}}})
    await send(h2, ADMIN, "#log astr")
    t = reply_texts(h2)[-1]
    step("file 模式文件缺失 → 明确报错", "日志文件不存在" in t and "x.log" in t, f"实际回复 {t[:60]}")

    # 非法配置逐项回落默认
    set_config({"log": {"mode": "haha", "files": {"astrbot": "-evil"}, "units": {"astrbot": "-x"}}})
    await send(h2, ADMIN, "#log astr 2")
    t = reply_texts(h2)[-1]
    step("非法 mode/路径/单元名逐项回落默认",
         calls2[-1] == ("journal", "astrbot", 2) and "回落" not in t and "⚠️" not in t,
         f"calls2={calls2[-1]} 回复={t[:40]}")
    set_config({})
    os.remove(log_path)

    # ---------- 日志截断 ----------
    print("\n== 超长日志截断 ==")
    h3 = make_handler()

    async def fake_long(unit: str, num: int) -> str:
        return "\n".join(("L%d " % i) + "x" * 100 for i in range(200))  # 约 2 万字符，远超上限

    h3._run_journalctl = fake_long
    await send(h3, ADMIN, "#log astr")
    t = reply_texts(h3)[-1]
    step("超长日志被截断且含标记",
         "仅保留末尾部分" in t and "L199" in t and len(t) < handler_mod.LOG_MAX_CHARS + 100,
         f"长度={len(t)}")

    # ---------- journalctl 异常 ----------
    print("\n== journalctl 异常 ==")
    h4 = make_handler()

    async def fake_fail(unit: str, num: int) -> str:
        raise FileNotFoundError("journalctl 不存在")

    h4._run_journalctl = fake_fail
    await send(h4, ADMIN, "#log astr")
    t = reply_texts(h4)[-1]
    step("journalctl 失败 → 友好报错", t.startswith("获取日志失败了："), f"实际回复 {t[:40]}")

    # ---------- #yukireboot 二次确认 ----------
    print("\n== #yukireboot 二次确认 ==")
    h5 = make_handler()
    rebooted: list = []

    async def fake_reboot() -> None:
        rebooted.append(True)

    h5._do_reboot = fake_reboot  # monkeypatch：不真重启

    await send(h5, ADMIN, "#yukireboot")
    t = reply_texts(h5)[-1]
    step("首次 #yukireboot → 确认提示（不重启）",
         "确认要重启" in t and not rebooted, f"实际回复 {t[:30]}")
    await send(h5, OTHER, "#yukireboot")
    step("非管理员插队确认不影响状态", not rebooted, f"rebooted={rebooted}")
    await send(h5, ADMIN, "#yukireboot")
    t = reply_texts(h5)[-1]
    step("管理员二次发送 → 确认并重启", "正在重启" in t and rebooted == [True],
         f"实际回复 {t[:30]} rebooted={rebooted}")
    step("确认后状态已清除", h5._pending_reboot is None, f"pending={h5._pending_reboot}")
    step("确认重启后写入通知标记文件", os.path.exists(h5.reboot_notify_path))

    # 启动成功通知（模拟新进程 NapCat 重连）
    h5.ob.ws.sent.clear()
    await h5.maybe_send_startup_notice(h5.ob.ws)
    sent_actions = [a for a in h5.ob.ws.sent if a.get("action") == "send_group_msg"]
    step("NapCat 重连 → 向触发群发送启动成功通知",
         len(sent_actions) == 1
         and "启动成功" in sent_actions[0]["params"]["message"]
         and sent_actions[0]["params"]["group_id"] == GROUP,
         f"sent={sent_actions}")
    step("通知发送后标记文件已删除", not os.path.exists(h5.reboot_notify_path),
         f"exists={os.path.exists(h5.reboot_notify_path)}")
    n_before = len(h5.ob.ws.sent)
    await h5.maybe_send_startup_notice(h5.ob.ws)
    step("无标记时再次连入不重复发送", len(h5.ob.ws.sent) == n_before)

    # 重启执行失败：提示且标记文件被清理
    h5b = make_handler()

    async def bad_reboot() -> None:
        raise RuntimeError("execv fail")

    h5b._do_reboot = bad_reboot
    await send(h5b, ADMIN, "#yukireboot")
    await send(h5b, ADMIN, "#yukireboot")
    t = reply_texts(h5b)[-1]
    step("重启执行失败 → 提示且标记已清理",
         "重启失败" in t and not os.path.exists(h5b.reboot_notify_path),
         f"实际回复 {t[:30]} exists={os.path.exists(h5b.reboot_notify_path)}")

    h6 = make_handler()
    rebooted6: list = []

    async def fake_reboot6() -> None:
        rebooted6.append(True)

    h6._do_reboot = fake_reboot6
    await send(h6, ADMIN, "#yukireboot")
    await send(h6, ADMIN, "#log astr")  # 期间执行其他指令不影响确认槽
    await send(h6, ADMIN, "#yukireboot")
    step("确认窗口内夹杂其他指令仍可确认", rebooted6 == [True], f"rebooted6={rebooted6}")

    h7 = make_handler()
    rebooted7: list = []

    async def fake_reboot7() -> None:
        rebooted7.append(True)

    h7._do_reboot = fake_reboot7
    await send(h7, ADMIN, "#yukireboot")
    h7._pending_reboot = (h7._pending_reboot[0], time.time() - 1)  # 手动改成已过期
    await send(h7, ADMIN, "#yukireboot")
    t = reply_texts(h7)[-1]
    step("过期确认 → 重新提示（不重启）", "确认要重启" in t and not rebooted7,
         f"实际回复 {t[:30]}")

    # ---------- 统计排除 ----------
    print("\n== 管理指令不污染统计 ==")
    h8 = make_handler()
    await send(h8, ADMIN, "#log astr")
    step("#log 不计入水群速度窗口",
         not h8._msg_times.get(str(GROUP)), f"窗口={h8._msg_times.get(str(GROUP))}")
    await send(h8, ADMIN, "大家好呀")
    step("普通消息正常计入水群速度窗口",
         len(h8._msg_times.get(str(GROUP), [])) == 1, f"窗口={h8._msg_times.get(str(GROUP))}")

    # ---------- 帮助菜单：用户版 / 管理员版 ----------
    print("\n== 帮助菜单拆分 ==")
    hA = make_handler()
    fake_r = FakeRenderer()
    hA.renderer = fake_r
    await send(hA, ADMIN, "/yukihelp")
    step("/yukihelp → 用户版菜单",
         fake_r.calls == [False] and "fakeimg-admin-False" in reply_texts(hA)[-1],
         f"calls={fake_r.calls} 回复={reply_texts(hA)[-1][:40]}")
    await send(hA, ADMIN, "//help")
    step("//help → 用户版菜单", fake_r.calls == [False, False], f"calls={fake_r.calls}")
    await send(hA, ADMIN, "/yukihelp a")
    step("/yukihelp a → 管理员版菜单",
         fake_r.calls[-1] is True and "fakeimg-admin-True" in reply_texts(hA)[-1],
         f"calls={fake_r.calls}")
    await send(hA, ADMIN, "//help admin")
    step("//help admin → 管理员版菜单", fake_r.calls[-1] is True, f"calls={fake_r.calls}")
    await send(hA, ADMIN, "/yukihelp A")
    step("/yukihelp A（大小写不敏感）→ 管理员版菜单", fake_r.calls[-1] is True,
         f"calls={fake_r.calls}")
    n_calls = len(fake_r.calls)
    await send(hA, ADMIN, "/yukihelp xyz")
    t = reply_texts(hA)[-1]
    step("/yukihelp 非法参数 → 提示且不出图",
         "参数只认 a / admin" in t and len(fake_r.calls) == n_calls,
         f"实际回复 {t[:40]} calls={len(fake_r.calls)}")

    hB = make_handler()
    hB.features["help"] = False
    fake_r2 = FakeRenderer()
    hB.renderer = fake_r2
    await send(hB, ADMIN, "/yukihelp a")
    step("help 功能停用后菜单指令静默", fake_r2.calls == [] and not reply_texts(hB),
         f"calls={fake_r2.calls} 回复={reply_texts(hB)}")

    # ---------- 多管理员 ----------
    print("\n== 多管理员配置 ==")
    os.environ["BOT_ADMIN_QQ"] = "1001,2002 3003，非数字,0"
    handler_mod = importlib.reload(handler_mod)
    hM = handler_mod.MessageHandler(FakeOB(), renderer=None, http_session=None, data_dir=_TMP_DATA)
    step("多管理员解析（逗号/空格/中文逗号，过滤非数字与 0）",
         handler_mod.ADMIN_QQS == frozenset({"1001", "2002", "3003"}),
         f"实际 {sorted(handler_mod.ADMIN_QQS)}")
    await send(hM, 1001, "#yukireboot")
    t = reply_texts(hM)[-1]
    step("管理员1 可触发重启确认", "确认要重启" in t, f"实际回复 {t[:30]}")
    hM._pending_reboot = None
    await send(hM, 2002, "#yukireboot")
    t = reply_texts(hM)[-1]
    step("管理员2 可触发重启确认", "确认要重启" in t, f"实际回复 {t[:30]}")
    hM._pending_reboot = None
    await send(hM, 3003, "#log astr")
    t = reply_texts(hM)[-1]
    step("管理员3 可用 #log", "用法：#log" in t or "📋" in t or "获取日志" in t, f"实际回复 {t[:30]}")
    await send(hM, 3002, "#yukireboot")
    t = reply_texts(hM)[-1]
    step("非管理员被拒绝", t == "只有管理员才能使用此指令~", f"实际回复 {t}")

    # ---------- ADMIN_QQ 未配置 ----------
    print("\n== BOT_ADMIN_QQ 未配置（=0）时全部拒绝 ==")
    os.environ["BOT_ADMIN_QQ"] = "0"
    handler_mod = importlib.reload(handler_mod)
    h9 = handler_mod.MessageHandler(FakeOB(), renderer=None, http_session=None, data_dir=_TMP_DATA)

    async def send9(uid: int, text: str) -> None:
        ev = {
            "post_type": "message", "message_type": "group", "sub_type": "normal",
            "self_id": SELF, "user_id": uid, "group_id": GROUP,
            "raw_message": text, "message": text,
            "sender": {"nickname": f"u{uid}", "user_id": uid},
            "message_id": len(h9.ob.ws.sent) + 1, "time": time.time(),
        }
        await h9.handle_event(ev)

    await send9(ADMIN, "#yukireboot")
    t = reply_texts(h9)[-1]
    step("未配置管理员 → #yukireboot 拒绝", t == "只有管理员才能使用此指令~", f"实际回复 {t}")
    await send9(ADMIN, "#log astr")
    t = reply_texts(h9)[-1]
    step("未配置管理员 → #log 拒绝", t == "只有管理员才能使用此指令~", f"实际回复 {t}")
    os.environ["BOT_ADMIN_QQ"] = "1001"

    print("\n" + "=" * 44)
    if _failed == 0:
        print(f"全部通过  共 {_passed} 项")
        return 0
    print(f"有失败  通过 {_passed}，失败 {_failed}")
    return 1


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
