"""端到端集成测试：用模拟 NapCat WS 客户端测试全部功能。

运行：
    python test_all.py

它会：
- 把数据目录指向临时目录（设 BOT_DATA_DIR），不污染真实 data/
- 启动 main.build_app() 的 WS Server（随机端口，不占用 8082）
- 以 WS 客户端身份连入，发送模拟群消息事件
- 收集并校验 bot 回发的 send_group_msg Action（复读 / 排行图 / 签到图 / 运势图）
- 校验 data/msg_stats.json 与 data/sign_in.json 写入正确

依赖：需已安装 requirements.txt 且执行过 `playwright install chromium`，
否则图片渲染类用例无法跑（脚本会给出明确提示）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from typing import Any, Dict, List

import aiohttp
from aiohttp import WSMsgType, web

# 1) 先把数据目录指向临时目录，必须在 import main 之前设置
_TMP_DATA = tempfile.mkdtemp(prefix="yuki_test_")
os.environ["BOT_DATA_DIR"] = _TMP_DATA
os.environ["BOT_ADMIN_QQ"] = "1001"  # 张三是测试管理员

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 2) 再 import main（它会在 import 时读取 BOT_DATA_DIR）
try:
    import main as yuki_main  # noqa: E402
except Exception as e:  # pragma: no cover
    print(f"[FAIL] 依赖缺失，无法导入 main：{e}")
    print("       请先：pip install -r requirements.txt && playwright install chromium")
    sys.exit(1)

SELF_ID = 10000       # 模拟机器人 QQ
GROUP_ID = 123456789  # 模拟群号
OTHER_GROUP = 987654321  # 场景8 用的另一个群（验证复读按群独立计数）

_passed = 0
_failed = 0
_msg_id = 0


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


def make_event(user_id: int, nickname: str, raw_message: str,
               group_id: int = GROUP_ID) -> Dict[str, Any]:
    global _msg_id
    _msg_id += 1
    return {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "self_id": SELF_ID,
        "user_id": user_id,
        "group_id": group_id,
        "raw_message": raw_message,
        "message": raw_message,
        "sender": {"nickname": nickname, "user_id": user_id},
        "message_id": _msg_id,
        "time": _msg_id,
    }


def load_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


async def collect(ws: aiohttp.ClientWebSocketResponse, idle: float = 1.5,
                   overall: float = 10.0) -> List[Dict[str, Any]]:
    """收集 bot 回发的 action，空闲 idle 秒无消息或达 overall 秒后停止。"""
    actions: List[Dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + overall
    while loop.time() < deadline:
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=idle)
        except asyncio.TimeoutError:
            break
        if msg.type == WSMsgType.TEXT:
            try:
                actions.append(msg.json())
            except Exception:
                pass
        elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
            break
        # 其它类型（PING/PONG/BINARY）忽略，继续接收
    return actions


def reply_texts(actions: List[Dict[str, Any]]) -> List[str]:
    return [
        a.get("params", {}).get("message", "")
        for a in actions
        if a.get("action") == "send_group_msg"
    ]


def has_image(msgs: List[str]) -> bool:
    return any("[CQ:image,file=base64://" in m and len(m) > 100 for m in msgs)


async def run_scenarios(ws: aiohttp.ClientWebSocketResponse) -> None:
    # ============ 场景1：三人复读 + 跟团不重复 ============
    print("== 场景1：三人复读（第3人触发，后续跟团不重复）==")
    text = "今晚月色真美"
    await ws.send_json(make_event(1001, "张三", text))
    step("第1人发言不复读", text not in reply_texts(await collect(ws, idle=1.0)))
    await ws.send_json(make_event(1002, "李四", text))
    step("第2人发言不复读", text not in reply_texts(await collect(ws, idle=1.0)))
    await ws.send_json(make_event(1003, "王五", text))
    step("第3人触发复读", text in reply_texts(await collect(ws, idle=1.5)))
    await ws.send_json(make_event(1004, "赵六", text))
    step("第4人跟团不再复读", text not in reply_texts(await collect(ws, idle=1.0)))

    # ============ 场景2：@bot 已下线（不再回复，交给 astrbot）============
    print("\n== 场景2：@bot 已下线（不再回复）==")
    # @bot 含压力词 → 不再回复
    at_msg = f"[CQ:at,qq={SELF_ID}] 啥子"
    await ws.send_json(make_event(1005, "钱七", at_msg))
    msgs = reply_texts(await collect(ws, idle=1.5))
    step("@bot 含'啥子' → 不回复", "压力一个bot？" not in msgs, str(msgs)[:120])
    # @bot 其它 → 不再回复
    at_msg2 = f"[CQ:at,qq={SELF_ID}] 在吗"
    await ws.send_json(make_event(1007, "孙九", at_msg2))
    msgs = reply_texts(await collect(ws, idle=1.5))
    step("@bot 其它 → 不回复", "何意味？" not in msgs, str(msgs)[:120])

    # ============ 场景3：发言被动统计（按日期隔离）============
    print("\n== 场景3：发言被动统计写入 msg_stats.json（按日期隔离）==")
    for uid, nick in [(1001, "张三"), (1001, "张三"), (1002, "李四"),
                      (1003, "王五"), (1006, "孙八")]:
        await ws.send_json(make_event(uid, nick, f"日常水群{uid}"))
        await collect(ws, idle=0.4)  # 这些不会触发回复，收掉即可
    stats = load_json(os.path.join(_TMP_DATA, "msg_stats.json"))
    grp = stats.get(str(GROUP_ID), {})
    today_str = date.today().isoformat()
    today_data = grp.get(today_str, {})
    step("数据按日期隔离（today key 存在）", today_str in grp, f"keys={list(grp.keys())}")
    step("今日统计记录 >=3 个成员", len(today_data) >= 3, f"成员数={len(today_data)}")
    step("张三今日计数 >=2", today_data.get("1001", {}).get("count", 0) >= 2,
         str(today_data.get("1001")))

    # ============ 场景4：/发言排行 和 /发言榜 图片 ============
    print("\n== 场景4：/发言排行 和 /发言榜 返回排行图片 ==")
    await ws.send_json(make_event(1001, "张三", "/发言排行"))
    msgs = reply_texts(await collect(ws, idle=20.0, overall=60.0))
    step("/发言排行 返回图片", has_image(msgs), str(msgs)[:120])
    await ws.send_json(make_event(1002, "李四", "/发言榜"))
    msgs = reply_texts(await collect(ws, idle=20.0, overall=60.0))
    step("/发言榜 别名返回图片", has_image(msgs), str(msgs)[:120])
    await ws.send_json(make_event(1003, "王五", "/今日发言榜"))
    msgs = reply_texts(await collect(ws, idle=20.0, overall=60.0))
    step("/今日发言榜 别名返回图片", has_image(msgs), str(msgs)[:120])

    # ============ 场景5：/签到 首次 图片 + 写入记录 ============
    print("\n== 场景5：/签到 首次返回图片并写入 sign_in.json ==")
    await ws.send_json(make_event(1001, "张三", "/签到"))
    msgs = reply_texts(await collect(ws, idle=20.0, overall=60.0))
    step("/签到 首次返回图片", has_image(msgs), str(msgs)[:120])
    sign = load_json(os.path.join(_TMP_DATA, "sign_in.json"))
    key = f"{GROUP_ID}_1001"
    step("签到记录已写入", key in sign, str(sign.get(key)))

    # ============ 场景6：/签到 重复（文本提示 + 重发运势图）============
    print("\n== 场景6：/签到 重复返回文本提示 + 重发运势图 ==")
    await ws.send_json(make_event(1001, "张三", "/签到"))
    msgs = reply_texts(await collect(ws, idle=20.0, overall=60.0))
    step("/签到 重复返回文本提示", any("已经签到过" in m for m in msgs), str(msgs)[:120])
    step("/签到 重复重发运势图", has_image(msgs), str(msgs)[:120])

    # ============ 场景7：/运势 图片 ============
    print("\n== 场景7：/运势 返回运势图片 ==")
    await ws.send_json(make_event(1002, "李四", "/运势"))
    msgs = reply_texts(await collect(ws, idle=20.0, overall=60.0))
    step("/运势 返回图片", has_image(msgs), str(msgs)[:120])

    # ============ 场景8：另一群复读按群独立计数 ============
    print("\n== 场景8：另一群复读独立计数 ==")
    text2 = "另一群专用复读"
    await ws.send_json(make_event(2001, "用户甲", text2, group_id=OTHER_GROUP))
    await collect(ws, idle=0.4)
    await ws.send_json(make_event(2002, "用户乙", text2, group_id=OTHER_GROUP))
    await collect(ws, idle=0.4)
    await ws.send_json(make_event(2003, "用户丙", text2, group_id=OTHER_GROUP))
    msgs = reply_texts(await collect(ws, idle=1.5))
    step("另一群 3 人触发复读", text2 in msgs, str(msgs)[:120])

    # ============ 场景9：/昨日发言 无数据时返回文本提示 ============
    print("\n== 场景9：/昨日发言 无昨日数据返回文本提示 ==")
    await ws.send_json(make_event(1001, "张三", "/昨日发言"))
    msgs = reply_texts(await collect(ws, idle=1.5))
    step("/昨日发言 无数据返回提示",
         any("昨日" in m and "没有" in m for m in msgs), str(msgs)[:120])

    # ============ 场景10：/昨日发言榜 注入昨日数据后返回图片 ============
    print("\n== 场景10：/昨日发言榜 注入数据后返回图片 ==")
    yesterday_str = (date.today() - timedelta(days=1)).isoformat()
    stats_path = os.path.join(_TMP_DATA, "msg_stats.json")
    stats = load_json(stats_path)
    grp = stats.setdefault(str(GROUP_ID), {})
    grp[yesterday_str] = {
        "1001": {"nickname": "张三", "count": 8},
        "1002": {"nickname": "李四", "count": 5},
        "1003": {"nickname": "王五", "count": 2},
    }
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    await ws.send_json(make_event(1001, "张三", "/昨日发言榜"))
    msgs = reply_texts(await collect(ws, idle=20.0, overall=60.0))
    step("/昨日发言榜 返回图片", has_image(msgs), str(msgs)[:120])

    # ============ 场景11：>7天旧数据惰性清理（保留 7 天窗口）============
    print("\n== 场景11：>7天旧数据在写入时被惰性清理，7 天内保留 ==")
    old_date = (date.today() - timedelta(days=8)).isoformat()
    keep_date = (date.today() - timedelta(days=3)).isoformat()
    stats = load_json(stats_path)
    grp = stats.setdefault(str(GROUP_ID), {})
    grp[old_date] = {"9999": {"nickname": "旧数据", "count": 100}}
    grp[keep_date] = {
        "1002": {"nickname": "李四", "count": 6},
        "1003": {"nickname": "王五", "count": 3},
    }
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    # 发一条消息触发惰性清理
    await ws.send_json(make_event(1001, "张三", "触发清理"))
    await collect(ws, idle=0.4)
    stats = load_json(stats_path)
    grp = stats.get(str(GROUP_ID), {})
    step("8天前数据被清理", old_date not in grp, f"keys={list(grp.keys())}")
    step("3天前数据保留（7天窗口内）", keep_date in grp, f"keys={list(grp.keys())}")
    step("今天+昨天数据仍在", today_str in grp and yesterday_str in grp,
         f"keys={list(grp.keys())}")

    # ============ 场景12：/查发言 按 QQ号 查询个人统计 ============
    print("\n== 场景12：/查发言 1001 按 QQ号 查询 → 图片 ==")
    await ws.send_json(make_event(1002, "李四", "/查发言 1001"))
    msgs = reply_texts(await collect(ws, idle=20.0, overall=60.0))
    step("/查发言 <QQ号> 返回图片", has_image(msgs), str(msgs)[:120])

    # ============ 场景13：/发言 不带参数查自己 / 带参数按昵称查询 ============
    print("\n== 场景13：/发言 无参查自己 + <昵称> 查询 → 图片 ==")
    await ws.send_json(make_event(1002, "李四", "/发言"))
    msgs = reply_texts(await collect(ws, idle=20.0, overall=60.0))
    step("/发言 无参查自己 → 图片", has_image(msgs), str(msgs)[:120])
    await ws.send_json(make_event(1003, "王五", "/查发言"))
    msgs = reply_texts(await collect(ws, idle=20.0, overall=60.0))
    step("/查发言 无参查自己 → 图片", has_image(msgs), str(msgs)[:120])
    await ws.send_json(make_event(1002, "李四", "/发言 张三"))
    msgs = reply_texts(await collect(ws, idle=20.0, overall=60.0))
    step("/发言 <昵称> 返回图片", has_image(msgs), str(msgs)[:120])

    # ============ 场景14：/查发言 查不存在的人 ============
    print("\n== 场景14：/查发言 不存在的人 → 文本提示 ==")
    await ws.send_json(make_event(1002, "李四", "/查发言 不存在的人"))
    msgs = reply_texts(await collect(ws, idle=1.5))
    step("/查发言 不存在的人返回提示",
         any("未找到" in m for m in msgs), str(msgs)[:120])

    # ============ 场景15：非管理员不能开启统计 ============
    print("\n== 场景15：非管理员 /统计发言 被拒绝 ==")
    await ws.send_json(make_event(1002, "李四", "/统计发言 不赖"))
    msgs = reply_texts(await collect(ws, idle=1.5))
    step("非管理员被拒绝", any("管理员" in m for m in msgs), str(msgs)[:120])

    # ============ 场景16：管理员开启统计 ============
    print("\n== 场景16：管理员 /统计发言 不赖 → 开启 ==")
    await ws.send_json(make_event(1001, "张三", "/统计发言 不赖"))
    msgs = reply_texts(await collect(ws, idle=1.5))
    step("管理员开启统计", any("已开启" in m and "不赖" in m for m in msgs), str(msgs)[:120])

    # ============ 场景17：发送含追踪短语的消息被统计 ============
    print("\n== 场景17：发送含「不赖」的消息被统计 ==")
    await ws.send_json(make_event(1002, "李四", "这首歌不赖啊"))
    await collect(ws, idle=0.4)
    await ws.send_json(make_event(1003, "王五", "不赖不赖"))
    await collect(ws, idle=0.4)
    await ws.send_json(make_event(1001, "张三", "确实不赖"))
    await collect(ws, idle=0.4)
    stats_path = os.path.join(_TMP_DATA, "phrase_stats.json")
    pstats = load_json(stats_path)
    grp = pstats.get(str(GROUP_ID), {})
    today_str = date.today().isoformat()
    ph_day = grp.get(today_str, {})
    bu_lai = ph_day.get("不赖", {})
    step("「不赖」统计到 >=3 个用户", len(bu_lai) >= 3, f"users={list(bu_lai.keys())}")
    step("李四「不赖」计数 >=1", bu_lai.get("1002", {}).get("count", 0) >= 1,
         str(bu_lai.get("1002")))

    # ============ 场景18：/<追踪短语> 生成统计图 ============
    print("\n== 场景18：/不赖 生成统计图片 ==")
    await ws.send_json(make_event(1002, "李四", "/不赖"))
    msgs = reply_texts(await collect(ws, idle=20.0, overall=60.0))
    step("/不赖 返回图片", has_image(msgs), str(msgs)[:120])

    # ============ 场景19：/昨日数据 查昨日统计 ============
    print("\n== 场景19：/昨日数据 不赖 → 无数据提示 ==")
    await ws.send_json(make_event(1002, "李四", "/昨日数据 不赖"))
    msgs = reply_texts(await collect(ws, idle=1.5))
    step("/昨日数据 无数据返回提示",
         any("没有" in m for m in msgs), str(msgs)[:120])

    # ============ 场景20：管理员删除统计 ============
    print("\n== 场景20：管理员 /删除统计 不赖 → 停止+清数据 ==")
    await ws.send_json(make_event(1001, "张三", "/删除统计 不赖"))
    msgs = reply_texts(await collect(ws, idle=1.5))
    step("管理员删除统计", any("已停止" in m and "不赖" in m for m in msgs), str(msgs)[:120])
    pstats = load_json(stats_path)
    grp = pstats.get(str(GROUP_ID), {})
    today_data = grp.get(today_str, {})
    step("「不赖」数据已清除", "不赖" not in today_data, f"keys={list(today_data.keys())}")

    # ============ 场景21：/yukihelp 和 //help 帮助菜单 ============
    print("\n== 场景21：/yukihelp 和 //help 返回帮助菜单图片 ==")
    await ws.send_json(make_event(1002, "李四", "/yukihelp"))
    msgs = reply_texts(await collect(ws, idle=20.0, overall=60.0))
    step("/yukihelp 返回图片", has_image(msgs), str(msgs)[:120])
    await ws.send_json(make_event(1003, "王五", "//help"))
    msgs = reply_texts(await collect(ws, idle=20.0, overall=60.0))
    step("//help 返回图片", has_image(msgs), str(msgs)[:120])

    # ============ 场景22：/发言趋势 折线图 ============
    print("\n== 场景22：/发言趋势 近7天折线图 ==")
    await ws.send_json(make_event(1001, "张三", "/发言趋势"))
    msgs = reply_texts(await collect(ws, idle=20.0, overall=60.0))
    step("/发言趋势 无参查自己 → 图片", has_image(msgs), str(msgs)[:120])
    await ws.send_json(make_event(1001, "张三", "/趋势"))
    msgs = reply_texts(await collect(ws, idle=20.0, overall=60.0))
    step("/趋势 别名无参查自己 → 图片", has_image(msgs), str(msgs)[:120])
    await ws.send_json(make_event(1001, "张三", "/发言趋势 1002"))
    msgs = reply_texts(await collect(ws, idle=20.0, overall=60.0))
    step("/发言趋势 <QQ号> → 图片", has_image(msgs), str(msgs)[:120])
    await ws.send_json(make_event(1001, "张三", "/趋势 1002"))
    msgs = reply_texts(await collect(ws, idle=20.0, overall=60.0))
    step("/趋势 <QQ号> → 图片", has_image(msgs), str(msgs)[:120])
    await ws.send_json(make_event(1001, "张三", "/发言趋势 李四"))
    msgs = reply_texts(await collect(ws, idle=20.0, overall=60.0))
    step("/发言趋势 <昵称> → 图片", has_image(msgs), str(msgs)[:120])
    await ws.send_json(make_event(1001, "张三", "/发言趋势 不存在的人"))
    msgs = reply_texts(await collect(ws, idle=1.5))
    step("/发言趋势 不存在的人 → 提示",
         any("未找到" in m for m in msgs), str(msgs)[:120])
    await ws.send_json(make_event(1001, "张三", "/发言趋势 8888"))
    msgs = reply_texts(await collect(ws, idle=1.5))
    step("/发言趋势 无记录用户 → 提示",
         any("没有发言记录" in m for m in msgs), str(msgs)[:120])

    # ============ 场景23：/发言速 /水群速 /水群 发言速度 ============
    print("\n== 场景23：/发言速 /水群速 /水群 发言速度 ==")
    # 造几条消息进滚动窗口
    for uid, nick in [(1002, "李四"), (1002, "李四"), (1003, "王五")]:
        await ws.send_json(make_event(uid, nick, "水群消息"))
        await collect(ws, idle=0.4)
    await ws.send_json(make_event(1001, "张三", "/发言速 30m"))
    msgs = reply_texts(await collect(ws, idle=20.0, overall=60.0))
    step("/发言速 30m → 图片", has_image(msgs), str(msgs)[:120])
    await ws.send_json(make_event(1001, "张三", "/水群速 1h"))
    msgs = reply_texts(await collect(ws, idle=20.0, overall=60.0))
    step("/水群速 1h → 图片", has_image(msgs), str(msgs)[:120])
    await ws.send_json(make_event(1002, "李四", "/水群"))
    msgs = reply_texts(await collect(ws, idle=20.0, overall=60.0))
    step("/水群 无参默认30分钟 → 图片", has_image(msgs), str(msgs)[:120])
    await ws.send_json(make_event(1001, "张三", "/发言速 三十分钟"))
    msgs = reply_texts(await collect(ws, idle=20.0, overall=60.0))
    step("/发言速 三十分钟（中文）→ 图片", has_image(msgs), str(msgs)[:120])
    await ws.send_json(make_event(1001, "张三", "/水群 abc"))
    msgs = reply_texts(await collect(ws, idle=1.5))
    step("/水群 abc → 格式提示", any("看不懂" in m for m in msgs), str(msgs)[:120])
    await ws.send_json(make_event(1001, "张三", "/发言速 1s"))
    msgs = reply_texts(await collect(ws, idle=1.5))
    step("/发言速 1s → 太短提示", any("太短" in m for m in msgs), str(msgs)[:120])
    await ws.send_json(make_event(1001, "张三", "/发言速 2d"))
    msgs = reply_texts(await collect(ws, idle=1.5))
    step("/发言速 2d → 太长提示", any("太长" in m for m in msgs), str(msgs)[:120])
    # 无发言的群 → 无数据提示
    await ws.send_json(make_event(1001, "张三", "/水群 5m", group_id=555888))
    msgs = reply_texts(await collect(ws, idle=1.5))
    step("/水群 无发言群 → 提示", any("还没有发言记录" in m for m in msgs), str(msgs)[:120])


async def amain() -> int:
    print(f"Python {sys.version.split()[0]}")
    print(f"测试数据目录（临时，不影响真实 data/）：{_TMP_DATA}\n")

    app = yuki_main.build_app()
    runner = web.AppRunner(app)
    try:
        await runner.setup()
    except Exception as e:
        print(f"[FAIL] WS Server 启动失败（多半是未安装 playwright/chromium）：{e}")
        print("       请先运行：pip install -r requirements.txt && playwright install chromium")
        await runner.cleanup()
        return 1

    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    print(f"测试 WS Server 已启动：ws://127.0.0.1:{port}（随机端口，不占用 8082）")

    session = aiohttp.ClientSession()
    try:
        ws = await session.ws_connect(f"http://127.0.0.1:{port}/")
    except Exception as e:
        print(f"[FAIL] 模拟客户端连接失败：{e}")
        await session.close()
        await runner.cleanup()
        return 1

    print("模拟 NapCat 客户端已连接\n")
    try:
        await run_scenarios(ws)
    finally:
        try:
            await ws.close()
        except Exception:
            pass
        await session.close()
        await runner.cleanup()  # 关闭浏览器 / HTTP 会话

    print("\n" + "=" * 52)
    if _failed == 0:
        print(f"全部通过  共 {_passed} 项  ✅")
        print(f"（临时数据目录保留以便排查：{_TMP_DATA}）")
        return 0
    print(f"有失败  通过 {_passed}，失败 {_failed}  ❌")
    print(f"（临时数据目录保留以便排查：{_TMP_DATA}）")
    return 1


def main() -> None:
    rc = asyncio.run(amain())
    sys.exit(rc)


if __name__ == "__main__":
    main()
