"""指令处理逻辑：

- 被动监听所有群消息，按日期记录发言统计到 data/msg_stats.json。
- /发言排行 / /发言榜 / /今日发言榜 → 今日 Top10 排行图片（实时更新）。
- /昨日发言 / /昨日发言榜 → 昨日 Top10 排行图片（已定格）。
- /查发言 / /发言 <QQ号|@用户|昵称> → 个人今日+昨日发言数据图片（含趋势对比+占比饼图）。
- /查发言 / /发言（不带参数）→ 查自己的今日+昨日发言数据。
- /统计发言 <短语>（管理员）→ 开启特定发言统计（对所有群生效）。
- /删除统计 <短语>（管理员）→ 停止统计并删除该短语所有数据。
- /<特定发言> → 生成该短语今日统计图片（出现次数+用户排行+占群饼图）。
- /昨日数据 <特定发言> → 生成该短语昨日统计图片。
- /签到 / /运势 → 每日签到 + 运势图片。
- /yukihelp / //help → 帮助菜单图片（含全部指令介绍）。
- /发言趋势 / /趋势 [QQ号|@用户|昵称] → 近 7 天发言趋势折线图（无参数查自己）。
- 发言统计每天 0 点自动刷新（按日期隔离），数据保留 7 天（今天 + 前 6 天），
  超出 7 天的旧数据在每次写入时惰性清理。
- @bot 已下线：@ 消息交给 astrbot 处理，本程序不再回复 @。
- 三人复读：同群连续 3 个不同成员说同一句时复读一次，同句只复读一次。
所有 IO（文件/HTTP/渲染）均异步；用 asyncio.Lock 保护并发文件读写。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import re
import time
from collections import deque
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import aiofiles
import aiohttp

import onebot
from features import DEFAULT_FEATURES, config_path, load_features
from repeat import RepeatDetector

logger = logging.getLogger("handler")

# 匹配 CQ 码里的 @ 对象 qq 号：[CQ:at,qq=12345] 或 [CQ:at,qq=12345,name=...]
_AT_RE = re.compile(r"\[CQ:at,qq=(\d+)[,\]]")

# 运势等级 + 对应主题色
FORTUNE_LEVELS: List[Tuple[str, str]] = [
    ("大吉", "#E74C3C"),
    ("中吉", "#F39C12"),
    ("小吉", "#27AE60"),
    ("凶", "#7F8C8D"),
]

YI_POOL = ["看番", "追番", "打音游", "发弹幕", "刷推", "肝手游", "逛漫展", "买谷", "二创", "听V家", "抽卡", "补番"]
JI_POOL = ["断网", "停更", "抽卡沉船", "加班", "早起", "弃坑", "被剧透", "掉分", "修仙", "吃刀子", "催更", "断更"]

COMMAND_STATS = "/发言排行"
COMMAND_STATS_ALIAS = "/发言榜"
COMMAND_STATS_TODAY = "/今日发言榜"
COMMAND_YESTERDAY_STATS = "/昨日发言"
COMMAND_YESTERDAY_STATS_ALIAS = "/昨日发言榜"
COMMAND_USER_STATS = "/查发言"
COMMAND_USER_STATS_ALIAS = "/发言"
COMMAND_TRACK_PHRASE = "/统计发言"
COMMAND_UNTRACK_PHRASE = "/删除统计"
COMMAND_YESTERDAY_PHRASE = "/昨日数据"
COMMAND_SIGN = "/签到"
COMMAND_FORTUNE = "/运势"
COMMAND_HELP = "/yukihelp"
COMMAND_HELP_ALIAS = "//help"
COMMAND_TREND = "/发言趋势"
COMMAND_TREND_ALIAS = "/趋势"
COMMAND_FEATURE_OFF = "/stop"
COMMAND_FEATURE_ON = "/start"
COMMAND_SPEED = "/发言速"
COMMAND_SPEED_ALIAS = "/水群速"
COMMAND_SPEED_ALIAS2 = "/水群"
SPEED_COMMANDS = (COMMAND_SPEED, COMMAND_SPEED_ALIAS, COMMAND_SPEED_ALIAS2)

# 功能 key → 中文名（/stop /start 指令及帮助菜单展示用）
FEATURE_NAMES: Dict[str, str] = {
    "stats": "发言排行",
    "user_stats": "个人发言查询",
    "trend": "发言趋势",
    "phrase_stats": "特定发言统计",
    "sign": "签到运势",
    "repeat": "三人复读",
    "help": "帮助菜单",
}

# 功能别名（中文/英文均可）→ key；新功能在此登记别名
FEATURE_ALIASES: Dict[str, str] = {
    "stats": "stats", "排行": "stats", "发言排行": "stats",
    "user_stats": "user_stats", "查发言": "user_stats", "个人查询": "user_stats",
    "trend": "trend", "趋势": "trend", "发言趋势": "trend",
    "phrase_stats": "phrase_stats", "短语": "phrase_stats", "特定发言": "phrase_stats",
    "sign": "sign", "签到": "sign", "运势": "sign",
    "repeat": "repeat", "复读": "repeat",
    "help": "help", "帮助": "help", "菜单": "help",
    "speed": "speed", "速度": "speed", "发言速度": "speed", "水群": "speed",
}

# ---------- 发言速度：时长解析与窗口常量 ----------

# 滚动窗口上限（内存中最多保留这么久的历史消息时间戳；超出的惰性清理 → 内存有界）
MAX_SPEED_WINDOW_SEC = 2 * 3600
# 查询窗口允许的最短时长
MIN_SPEED_WINDOW_SEC = 60

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

# 单位 → 秒
_SPEED_UNITS: Dict[str, int] = {
    "s": 1, "sec": 1, "secs": 1, "秒": 1,
    "m": 60, "min": 60, "mins": 60, "分": 60, "分钟": 60,
    "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600, "时": 3600, "小时": 3600,
    "d": 86400, "day": 86400, "天": 86400,
}


def _parse_cn_int(s: str) -> Optional[int]:
    """解析简单中文数字（0-99，如 十五/二十/三十/两）。"""
    if not s:
        return None
    if "十" in s:
        left, _, right = s.partition("十")
        if left and left not in _CN_DIGITS:
            return None
        if right and right not in _CN_DIGITS:
            return None
        tens = _CN_DIGITS[left] if left else 1
        ones = _CN_DIGITS[right] if right else 0
        return tens * 10 + ones
    total = 0
    for ch in s:
        if ch not in _CN_DIGITS:
            return None
        total = total * 10 + _CN_DIGITS[ch]
    return total


def parse_duration(text: str) -> Optional[int]:
    """把时长文本解析为秒。支持：30m/30分/三十分钟/30分钟/1h/一小时/1.5h/45(默认分钟)。"""
    t = text.strip().lower()
    if not t:
        return None
    # 阿拉伯数字（含小数）
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(.*)$", t)
    if m:
        val = float(m.group(1))
        unit = m.group(2)
        if not unit:
            return round(val * 60)  # 纯数字默认按分钟
        unit = unit.lstrip("个")
        return round(val * _SPEED_UNITS[unit]) if unit in _SPEED_UNITS else None
    # 中文数字开头
    i = 0
    while i < len(t) and (t[i] in _CN_DIGITS or t[i] == "十"):
        i += 1
    n = _parse_cn_int(t[:i])
    if n is None:
        return None
    unit = t[i:].lstrip("个")
    return n * _SPEED_UNITS[unit] if unit in _SPEED_UNITS else None


def _fmt_window(sec: int) -> str:
    """把秒数格式化为人类可读时长（用于展示）。"""
    if sec % 3600 == 0:
        return f"{sec // 3600} 小时"
    if sec >= 3600:
        return f"{sec / 3600:g} 小时"
    return f"{sec // 60} 分钟" if sec % 60 == 0 else f"{sec / 60:g} 分钟"

# 数据保留天数（今天 + 前 6 天 = 7 天），/昨日发言 /昨日数据 等原有查询不受影响
STATS_RETENTION_DAYS = 7

# 管理员 QQ（最高权限，可使用 /统计发言 /删除统计）
# 默认 "0"（不启用管理员指令）；部署时通过环境变量 BOT_ADMIN_QQ 注入真实 QQ 号
ADMIN_QQ = os.environ.get("BOT_ADMIN_QQ", "0")


class MessageHandler:
    def __init__(
        self,
        ob_conn: onebot.OneBotConnection,
        renderer: Any,
        http_session: aiohttp.ClientSession,
        data_dir: str,
    ) -> None:
        self.ob = ob_conn
        self.renderer = renderer
        self.http = http_session
        self.data_dir = data_dir
        self.stats_path = os.path.join(data_dir, "msg_stats.json")
        self.sign_path = os.path.join(data_dir, "sign_in.json")
        self.tracked_path = os.path.join(data_dir, "tracked_phrases.json")
        self.phrase_stats_path = os.path.join(data_dir, "phrase_stats.json")
        self._stats_lock = asyncio.Lock()
        self._sign_lock = asyncio.Lock()
        self._phrase_stats_lock = asyncio.Lock()
        self._tracked_lock = asyncio.Lock()
        self._config_lock = asyncio.Lock()
        self.config_path = config_path()
        self._rng = random.Random()
        self._repeat = RepeatDetector()
        # 发言速度滚动窗口：group_id(str) -> deque[(timestamp, uid, nickname)]
        # 只保留 MAX_SPEED_WINDOW_SEC 内的消息（写入时惰性清理头部，内存有界）
        self._msg_times: Dict[str, deque] = {}
        # 功能开关（config.json，见 features.py；读取失败 → 全部启用）
        self.features: Dict[str, bool] = load_features()
        self._ensure_files()
        # 内存缓存：追踪短语列表（避免每条消息都读文件）
        self._tracked_phrases: List[str] = self._load_tracked_phrases_sync()

    # ---------- 文件初始化 ----------
    def _ensure_files(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        # msg_stats / sign_in / phrase_stats → 空对象
        for p in (self.stats_path, self.sign_path, self.phrase_stats_path):
            if not os.path.exists(p):
                with open(p, "w", encoding="utf-8") as f:
                    json.dump({}, f)
        # tracked_phrases → 空列表
        if not os.path.exists(self.tracked_path):
            with open(self.tracked_path, "w", encoding="utf-8") as f:
                json.dump([], f)

    # ---------- 追踪短语列表（同步加载，仅在 init 时调用）----------
    def _load_tracked_phrases_sync(self) -> List[str]:
        """启动时同步加载追踪短语列表到内存。"""
        if not os.path.exists(self.tracked_path):
            return []
        try:
            with open(self.tracked_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except (json.JSONDecodeError, IOError):
            return []

    async def _save_tracked_phrases(self) -> None:
        """保存追踪短语列表到文件（原子写）。"""
        tmp = self.tracked_path + ".tmp"
        async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
            await f.write(json.dumps(self._tracked_phrases, ensure_ascii=False, indent=2))
        os.replace(tmp, self.tracked_path)

    # ---------- 事件入口 ----------
    async def handle_event(self, data: Dict[str, Any]) -> None:
        ev = onebot.parse_event(data)
        if ev["post_type"] != "message" or ev["message_type"] != "group":
            return

        group_id = ev["group_id"]
        user_id = ev["user_id"]
        nickname = ev["nickname"]
        self_id = ev["self_id"]
        raw_message = ev["raw_message"]
        raw = raw_message.strip()

        # 跳过机器人自身消息（避免复读自己 / 自我触发 / 计入统计）
        if self_id is not None and user_id == self_id:
            return

        # 被动统计：任何群消息都计入（排行/趋势/个人查询都依赖，不设开关）
        await self._record_message(group_id, user_id, nickname)
        # 发言速度滚动窗口（同步内存操作，无 IO；跳过指令消息，统计真实水群）
        if not raw.startswith("/"):
            self._record_msg_time(group_id, user_id, nickname)
        # 特定发言追踪（跳过 / 开头的指令消息，避免查询指令被误统计）
        if not raw.startswith("/") and self.features.get("phrase_stats", True):
            await self._record_phrase_stats(group_id, user_id, nickname, raw_message)

        ws = self.ob.ws

        # 指令（@bot 已下线：@ 消息交给 astrbot 处理，本程序不再回复 @）
        # 各功能受 config.json 开关控制（见 features.py），停用的指令静默不响应
        if self.features.get("stats", True) and raw in (
            COMMAND_STATS, COMMAND_STATS_ALIAS, COMMAND_STATS_TODAY
        ):
            await self._cmd_stats(group_id, is_yesterday=False)
            return
        if self.features.get("stats", True) and raw in (
            COMMAND_YESTERDAY_STATS, COMMAND_YESTERDAY_STATS_ALIAS
        ):
            await self._cmd_stats(group_id, is_yesterday=True)
            return
        # /发言趋势 / /趋势 [QQ号|@用户|昵称] → 近 7 天发言趋势折线图（无参数查自己）
        if self.features.get("trend", True):
            if raw in (COMMAND_TREND, COMMAND_TREND_ALIAS):
                await self._cmd_trend(group_id, user_id, nickname, "")
                return
            if raw.startswith(COMMAND_TREND + " ") or raw.startswith(COMMAND_TREND_ALIAS + " "):
                prefix = COMMAND_TREND if raw.startswith(COMMAND_TREND + " ") else COMMAND_TREND_ALIAS
                arg = raw[len(prefix) + 1:].strip()
                await self._cmd_trend(group_id, user_id, nickname, arg)
                return
        # /查发言 或 /发言 不带参数 → 查自己的今日+昨日发言数据
        # （/发言排行 /发言榜 /发言趋势 均为更长指令，不会误触）
        if self.features.get("user_stats", True):
            if raw in (COMMAND_USER_STATS, COMMAND_USER_STATS_ALIAS):
                await self._cmd_user_stats(group_id, str(user_id))
                return
            # /查发言 或 /发言 <QQ号|@用户|昵称>（注意带空格才匹配，避免和 /发言榜 等冲突）
            if raw.startswith(COMMAND_USER_STATS + " ") or raw.startswith(COMMAND_USER_STATS_ALIAS + " "):
                prefix = COMMAND_USER_STATS if raw.startswith(COMMAND_USER_STATS + " ") else COMMAND_USER_STATS_ALIAS
                arg = raw[len(prefix) + 1:].strip()
                await self._cmd_user_stats(group_id, arg)
                return
        # 管理员指令：/统计发言 <短语> 和 /删除统计 <短语>（仅管理员可用）
        if self.features.get("phrase_stats", True):
            if raw.startswith(COMMAND_TRACK_PHRASE + " "):
                phrase = raw[len(COMMAND_TRACK_PHRASE) + 1:].strip()
                await self._cmd_track_phrase(group_id, user_id, phrase)
                return
            if raw.startswith(COMMAND_UNTRACK_PHRASE + " "):
                phrase = raw[len(COMMAND_UNTRACK_PHRASE) + 1:].strip()
                await self._cmd_untrack_phrase(group_id, user_id, phrase)
                return
            # /昨日数据 <短语> → 昨日特定发言统计
            if raw.startswith(COMMAND_YESTERDAY_PHRASE + " "):
                phrase = raw[len(COMMAND_YESTERDAY_PHRASE) + 1:].strip()
                await self._cmd_phrase_stats(group_id, phrase, is_yesterday=True)
                return
        # /发言速 /水群速 /水群 [时间] → 最近一段时间的发言速度排行
        if self.features.get("speed", True):
            if raw in SPEED_COMMANDS:  # 无参数 → 默认 30 分钟
                await self._cmd_speed(group_id, "")
                return
            matched = next((c for c in SPEED_COMMANDS if raw.startswith(c + " ")), None)
            if matched:
                await self._cmd_speed(group_id, raw[len(matched) + 1:].strip())
                return
        # 管理员热切换功能开关：/stop /start [功能]（不受任何开关控制，保证随时可恢复）
        if raw == COMMAND_FEATURE_OFF or raw.startswith(COMMAND_FEATURE_OFF + " "):
            arg = raw[len(COMMAND_FEATURE_OFF):].strip()
            await self._cmd_feature_switch(group_id, user_id, False, arg)
            return
        if raw == COMMAND_FEATURE_ON or raw.startswith(COMMAND_FEATURE_ON + " "):
            arg = raw[len(COMMAND_FEATURE_ON):].strip()
            await self._cmd_feature_switch(group_id, user_id, True, arg)
            return
        if self.features.get("help", True) and raw in (COMMAND_HELP, COMMAND_HELP_ALIAS):
            await self._cmd_help(group_id)
            return
        if self.features.get("sign", True) and raw in (COMMAND_SIGN, COMMAND_FORTUNE):
            await self._cmd_sign(group_id, user_id, nickname)
            return
        # /<追踪短语> → 今日特定发言统计（放在所有指令之后，避免和 /签到 等冲突）
        if self.features.get("phrase_stats", True) and raw.startswith("/") and len(raw) > 1:
            phrase = raw[1:]
            if phrase in self._tracked_phrases:
                await self._cmd_phrase_stats(group_id, phrase, is_yesterday=False)
                return

        # 3. 三人复读（仅纯文本参与；命中后本波次只发一次）
        if not self.features.get("repeat", True):
            return
        repeat_text = self._repeat.check_and_trigger(group_id, user_id, self_id, raw)
        if repeat_text is not None:
            try:
                await onebot.send_group_msg(ws, group_id, repeat_text)
            except Exception as e:
                logger.exception("复读发送失败: %s", e)

    # ---------- 通用 JSON 读写 ----------
    async def _load_json(self, path: str) -> Dict[str, Any]:
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            content = await f.read()
        try:
            return json.loads(content) if content.strip() else {}
        except json.JSONDecodeError:
            logger.warning("JSON 解析失败，重置为空 dict: %s", path)
            return {}

    async def _save_json(self, path: str, obj: Dict[str, Any]) -> None:
        tmp = path + ".tmp"
        async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
            await f.write(json.dumps(obj, ensure_ascii=False, indent=2))
        os.replace(tmp, path)  # 原子写，避免半截文件

    # ---------- 发言统计（按日期隔离，保留 2 天）----------

    @staticmethod
    def _is_old_format(group_data: Dict[str, Any]) -> bool:
        """检测 group_data 是否为旧格式（group → user → {nickname, count}）。

        新格式是 group → date → user → {nickname, count}。
        通过判断 key 是否为 ISO 日期（YYYY-MM-DD）来区分。
        """
        if not group_data:
            return False
        for key in group_data:
            try:
                datetime.strptime(key, "%Y-%m-%d")
            except (ValueError, TypeError):
                return True   # key 不是日期 → 旧格式
        return False

    @staticmethod
    def _migrate_group(group_data: Dict[str, Any]) -> Dict[str, Any]:
        """把旧格式（user → stats）迁移为新格式（date → user → stats），旧数据归入今天。"""
        today = date.today().isoformat()
        return {today: dict(group_data)}

    @staticmethod
    def _prune_old_dates(group_data: Dict[str, Any]) -> int:
        """删除超出保留窗口的日期数据（保留今天 + 前 N-1 天，共 7 天）。返回删除的日期数。

        ISO 日期字符串（YYYY-MM-DD）按字典序排序 = 按时间排序，可直接用 < 比较。
        """
        cutoff = (date.today() - timedelta(days=STATS_RETENTION_DAYS - 1)).isoformat()
        to_delete = [k for k in group_data if len(k) == 10 and k < cutoff]
        for k in to_delete:
            del group_data[k]
        return len(to_delete)

    async def _record_message(self, group_id: int, user_id: int, nickname: str) -> None:
        today = date.today().isoformat()
        gkey, ukey = str(group_id), str(user_id)
        async with self._stats_lock:
            stats = await self._load_json(self.stats_path)
            group = stats.setdefault(gkey, {})

            # 旧格式迁移：group → user → stats 变成 group → today → user → stats
            if self._is_old_format(group):
                group = self._migrate_group(group)
                stats[gkey] = group

            # 惰性清理：删除 2 天前的旧数据
            self._prune_old_dates(group)

            # 按今天日期记录
            day_data = group.setdefault(today, {})
            user = day_data.get(ukey, {"nickname": nickname, "count": 0})
            user["nickname"] = nickname
            user["count"] += 1
            day_data[ukey] = user

            await self._save_json(self.stats_path, stats)

    async def _cmd_stats(self, group_id: int, is_yesterday: bool = False) -> None:
        ws = self.ob.ws
        try:
            if is_yesterday:
                target_date = (date.today() - timedelta(days=1)).isoformat()
                date_label = "昨日"
            else:
                target_date = date.today().isoformat()
                date_label = "今日"

            async with self._stats_lock:
                stats = await self._load_json(self.stats_path)
            group = stats.get(str(group_id), {})

            # 旧格式迁移（读取时也兜底一次）
            if self._is_old_format(group):
                group = self._migrate_group(group)

            day_data = group.get(target_date, {})
            if not day_data:
                msg = "本群昨日还没有发言记录哦~" if is_yesterday else "本群今日还没有发言记录哦~ 快来水群吧！"
                await onebot.send_group_text(ws, group_id, msg)
                return

            total = sum(u["count"] for u in day_data.values())
            items = sorted(day_data.items(), key=lambda kv: kv[1]["count"], reverse=True)[:10]

            top_users: List[dict] = []
            for i, (uid, u) in enumerate(items, 1):
                avatar_b64 = await self._fetch_avatar_b64(uid)
                percent = round(u["count"] * 100 / total, 1) if total else 0.0
                top_users.append({
                    "rank": i,
                    "user_id": uid,
                    "nickname": u.get("nickname", uid),
                    "avatar_b64": avatar_b64,
                    "count": u["count"],
                    "percent": percent,
                })

            image_b64 = await self.renderer.render_stats(
                group_id, f"群 {group_id}", top_users, total, date_label=date_label
            )
            await onebot.send_group_image_b64(ws, group_id, image_b64)
        except Exception as e:
            logger.exception("发言排行生成失败: %s", e)
            await onebot.send_group_text(ws, group_id, f"生成发言排行失败了：{e}")

    # ---------- 个人发言查询 ----------
    async def _cmd_user_stats(self, group_id: int, arg: str) -> None:
        """查询指定用户的今日+昨日发言数据，渲染个人统计图片。

        arg 支持：QQ号、@用户（CQ 码）、群昵称。
        """
        ws = self.ob.ws
        try:
            today_str = date.today().isoformat()
            yesterday_str = (date.today() - timedelta(days=1)).isoformat()

            # 解析参数：@用户 → QQ号；纯数字 → QQ号；否则 → 按昵称搜索
            target_uid = None
            at_match = _AT_RE.search(arg)
            if at_match:
                target_uid = at_match.group(1)
            elif arg.isdigit():
                target_uid = arg
            else:
                # 按昵称搜索（先今天再昨天）
                async with self._stats_lock:
                    stats = await self._load_json(self.stats_path)
                group = stats.get(str(group_id), {})
                for dk in (today_str, yesterday_str):
                    day = group.get(dk, {})
                    for uid, u in day.items():
                        if u.get("nickname") == arg:
                            target_uid = uid
                            break
                    if target_uid:
                        break

            if not target_uid:
                await onebot.send_group_text(
                    ws, group_id, f"未找到用户「{arg}」，请用 QQ号、@用户 或 群昵称查询~"
                )
                return

            # 取今日/昨日数据
            async with self._stats_lock:
                stats = await self._load_json(self.stats_path)
            group = stats.get(str(group_id), {})
            today_data = group.get(today_str, {})
            yesterday_data = group.get(yesterday_str, {})

            today_user = today_data.get(str(target_uid), {})
            yesterday_user = yesterday_data.get(str(target_uid), {})
            today_count = today_user.get("count", 0)
            yesterday_count = yesterday_user.get("count", 0)

            nickname = (
                today_user.get("nickname")
                or yesterday_user.get("nickname")
                or str(target_uid)
            )

            if today_count == 0 and yesterday_count == 0:
                await onebot.send_group_text(
                    ws, group_id, f"用户「{nickname}」今日和昨日都没有发言记录~"
                )
                return

            today_total = sum(u["count"] for u in today_data.values())
            yesterday_total = sum(u["count"] for u in yesterday_data.values())
            today_pct = round(today_count * 100 / today_total, 1) if today_total else 0.0
            yesterday_pct = round(yesterday_count * 100 / yesterday_total, 1) if yesterday_total else 0.0

            # 趋势计算
            if yesterday_count > 0:
                diff = today_count - yesterday_count
                trend = "up" if diff > 0 else ("down" if diff < 0 else "same")
                change_pct = round(abs(diff) * 100 / yesterday_count, 1)
            else:
                diff = today_count
                trend = "new" if today_count > 0 else "same"
                change_pct = 100.0

            avatar_b64 = await self._fetch_avatar_b64(target_uid)
            image_b64 = await self.renderer.render_user_stats(
                nickname=nickname,
                avatar_b64=avatar_b64,
                target_uid=str(target_uid),
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
            await onebot.send_group_image_b64(ws, group_id, image_b64)
        except Exception as e:
            logger.exception("个人发言查询失败: %s", e)
            await onebot.send_group_text(ws, group_id, f"查询发言失败了：{e}")

    # ---------- 发言趋势（近 7 天折线图）----------
    async def _cmd_trend(self, group_id: int, user_id: int, nickname: str, arg: str) -> None:
        """生成近 7 天发言趋势图片（折线图：每日发言数 + 占群比例）。

        arg 为空 → 查自己；否则支持 QQ号 / @用户 / 群昵称。
        """
        ws = self.ob.ws
        try:
            today = date.today()
            dates = [today - timedelta(days=i) for i in range(STATS_RETENTION_DAYS - 1, -1, -1)]

            async with self._stats_lock:
                stats = await self._load_json(self.stats_path)
            group = stats.get(str(group_id), {})
            if self._is_old_format(group):
                group = self._migrate_group(group)

            # 解析目标用户
            if not arg:
                target_uid, target_nick = str(user_id), nickname
            else:
                target_uid = None
                at_match = _AT_RE.search(arg)
                if at_match:
                    target_uid = at_match.group(1)
                elif arg.isdigit():
                    target_uid = arg
                else:
                    # 按昵称搜索（从今天往回找，最近一天的记录优先）
                    for d in dates:
                        for uid, u in group.get(d.isoformat(), {}).items():
                            if u.get("nickname") == arg:
                                target_uid = uid
                                break
                        if target_uid:
                            break
                if not target_uid:
                    await onebot.send_group_text(
                        ws, group_id, f"未找到用户「{arg}」，请用 QQ号、@用户 或 群昵称查询~"
                    )
                    return
                target_nick = arg

            # 组装近 7 天序列（无记录的天补 0）
            days: List[dict] = []
            counts: List[int] = []
            found_nick: str = ""
            for d in dates:
                day = group.get(d.isoformat(), {})
                user = day.get(str(target_uid), {})
                cnt = user.get("count", 0)
                total = sum(u["count"] for u in day.values())
                if user.get("nickname"):
                    found_nick = user["nickname"]
                counts.append(cnt)
                days.append({
                    "label": d.strftime("%m-%d"),
                    "date_full": d.isoformat(),
                    "weekday": "一二三四五六日"[d.weekday()],
                    "count": cnt,
                    "total": total,
                    "pct": round(cnt * 100 / total, 1) if total else 0.0,
                })
            days[-1]["label"] = "今天"
            days[-2]["label"] = "昨天"
            nickname_disp = found_nick or target_nick or str(target_uid)

            if sum(counts) == 0:
                await onebot.send_group_text(
                    ws, group_id, f"用户「{nickname_disp}」近 {STATS_RETENTION_DAYS} 天都没有发言记录~"
                )
                return

            # 汇总
            total_count = sum(counts)
            avg_count = round(total_count / len(counts), 1)
            peak_count = max(counts)
            peak_idx = counts.index(peak_count)
            peak_label = days[peak_idx]["date_full"]
            pcts = [d["pct"] for d in days]
            avg_pct = round(sum(pcts) / len(pcts), 1)
            today_count = days[-1]["count"]
            today_pct = days[-1]["pct"]

            avatar_b64 = await self._fetch_avatar_b64(target_uid)
            image_b64 = await self.renderer.render_trend(
                nickname=nickname_disp,
                avatar_b64=avatar_b64,
                target_uid=str(target_uid),
                days=days,
                total_count=total_count,
                avg_count=avg_count,
                peak_count=peak_count,
                peak_label=peak_label,
                avg_pct=avg_pct,
                today_count=today_count,
                today_pct=today_pct,
            )
            await onebot.send_group_image_b64(ws, group_id, image_b64)
        except Exception as e:
            logger.exception("发言趋势生成失败: %s", e)
            await onebot.send_group_text(ws, group_id, f"发言趋势生成失败了：{e}")

    # ---------- 发言速度 ----------
    def _record_msg_time(self, group_id: int, user_id: int, nickname: str) -> None:
        """把一条消息记入滚动窗口（内存），并惰性清理超窗旧数据。"""
        gkey = str(group_id)
        dq = self._msg_times.get(gkey)
        if dq is None:
            dq = deque()
            self._msg_times[gkey] = dq
        now = time.time()
        dq.append((now, str(user_id), nickname))
        cutoff = now - MAX_SPEED_WINDOW_SEC
        while dq and dq[0][0] < cutoff:  # 时间戳单调递增，头部即最旧
            dq.popleft()

    async def _cmd_speed(self, group_id: int, arg: str) -> None:
        """查询最近一段时间内的发言速度（总速度 + 每人速度 Top10）。

        arg 支持 30m/30分/三十分钟/1h/一小时/1.5h 等；空 → 默认 30 分钟。
        """
        ws = self.ob.ws
        try:
            if arg:
                window_sec = parse_duration(arg)
                if window_sec is None:
                    await onebot.send_group_text(
                        ws, group_id,
                        "时间参数看不懂哦~ 支持：30m、30分、三十分钟、1h、一小时、1.5h（分钟/小时）",
                    )
                    return
            else:
                window_sec = 1800  # 默认 30 分钟
            if window_sec < MIN_SPEED_WINDOW_SEC:
                await onebot.send_group_text(
                    ws, group_id, f"时间窗口太短啦，最短支持 {_fmt_window(MIN_SPEED_WINDOW_SEC)}~"
                )
                return
            if window_sec > MAX_SPEED_WINDOW_SEC:
                await onebot.send_group_text(
                    ws, group_id, f"时间窗口太长啦，最长支持 {_fmt_window(MAX_SPEED_WINDOW_SEC)}~"
                )
                return

            cutoff = time.time() - window_sec
            per_user: Dict[str, List] = {}  # uid -> [nickname, count]
            total = 0
            dq = self._msg_times.get(str(group_id))
            if dq:
                # 从最新往回扫，遇到早于窗口起点即可停止
                for ts, uid, nick in reversed(dq):
                    if ts < cutoff:
                        break
                    total += 1
                    rec = per_user.setdefault(uid, [nick, 0])
                    rec[0] = nick  # 用最新昵称
                    rec[1] += 1

            window_label = _fmt_window(window_sec)
            if total == 0:
                await onebot.send_group_text(
                    ws, group_id, f"最近 {window_label} 本群还没有发言记录~"
                )
                return

            minutes = window_sec / 60
            speed = round(total / minutes, 1)
            items = sorted(per_user.items(), key=lambda kv: kv[1][1], reverse=True)[:10]
            top_users: List[dict] = []
            for i, (uid, (nick, cnt)) in enumerate(items, 1):
                avatar_b64 = await self._fetch_avatar_b64(uid)
                top_users.append({
                    "rank": i,
                    "user_id": uid,
                    "nickname": nick,
                    "avatar_b64": avatar_b64,
                    "count": cnt,
                    "rate": round(cnt / minutes, 1),  # 该用户条/分钟
                    "percent": round(cnt * 100 / total, 1),
                })

            image_b64 = await self.renderer.render_speed(
                window_label=window_label,
                top_users=top_users,
                total=total,
                speed=speed,
                active_users=len(per_user),
            )
            await onebot.send_group_image_b64(ws, group_id, image_b64)
        except Exception as e:
            logger.exception("发言速度查询失败: %s", e)
            await onebot.send_group_text(ws, group_id, f"发言速度查询失败了：{e}")

    # ---------- 特定发言追踪 ----------

    async def _record_phrase_stats(
        self, group_id: int, user_id: int, nickname: str, raw_message: str
    ) -> None:
        """检查消息是否包含被追踪的特定发言，如果包含则记录（按日期隔离）。"""
        if not self._tracked_phrases:
            return

        today = date.today().isoformat()
        gkey, ukey = str(group_id), str(user_id)

        # 找出消息中包含哪些追踪短语
        matched = [p for p in self._tracked_phrases if p in raw_message]
        if not matched:
            return

        async with self._phrase_stats_lock:
            stats = await self._load_json(self.phrase_stats_path)
            group = stats.setdefault(gkey, {})
            self._prune_old_dates(group)  # 惰性清理 >2 天数据
            day_data = group.setdefault(today, {})
            for phrase in matched:
                ph_data = day_data.setdefault(phrase, {})
                user = ph_data.get(ukey, {"nickname": nickname, "count": 0})
                user["nickname"] = nickname
                user["count"] += 1
                ph_data[ukey] = user
            await self._save_json(self.phrase_stats_path, stats)

    async def _cmd_track_phrase(self, group_id: int, user_id: int, phrase: str) -> None:
        """管理员：开启特定发言统计（对所有群生效）。"""
        ws = self.ob.ws
        if str(user_id) != ADMIN_QQ:
            await onebot.send_group_text(ws, group_id, "只有管理员才能使用此指令~")
            return
        if not phrase:
            await onebot.send_group_text(ws, group_id, "请输入要统计的发言，例如：/统计发言 不赖")
            return
        try:
            async with self._tracked_lock:
                if phrase not in self._tracked_phrases:
                    self._tracked_phrases.append(phrase)
                    await self._save_tracked_phrases()
                    msg = f"已开启统计「{phrase}」，所有群立即生效~"
                else:
                    msg = f"「{phrase}」已经在统计中了~"
            await onebot.send_group_text(ws, group_id, msg)
        except Exception as e:
            logger.exception("开启统计失败: %s", e)
            await onebot.send_group_text(ws, group_id, f"开启统计失败了：{e}")

    async def _cmd_untrack_phrase(self, group_id: int, user_id: int, phrase: str) -> None:
        """管理员：停止统计并删除该短语所有数据。"""
        ws = self.ob.ws
        if str(user_id) != ADMIN_QQ:
            await onebot.send_group_text(ws, group_id, "只有管理员才能使用此指令~")
            return
        if not phrase:
            await onebot.send_group_text(ws, group_id, "请输入要删除的统计，例如：/删除统计 不赖")
            return
        try:
            async with self._tracked_lock:
                if phrase in self._tracked_phrases:
                    self._tracked_phrases.remove(phrase)
                    await self._save_tracked_phrases()
            # 删除所有群所有日期的该短语数据
            async with self._phrase_stats_lock:
                stats = await self._load_json(self.phrase_stats_path)
                for gkey, group in stats.items():
                    for dk in list(group.keys()):
                        if phrase in group[dk]:
                            del group[dk][phrase]
                await self._save_json(self.phrase_stats_path, stats)
            await onebot.send_group_text(
                ws, group_id, f"已停止统计「{phrase}」并清除所有数据~"
            )
        except Exception as e:
            logger.exception("停止统计失败: %s", e)
            await onebot.send_group_text(ws, group_id, f"停止统计失败了：{e}")

    async def _cmd_phrase_stats(self, group_id: int, phrase: str, is_yesterday: bool = False) -> None:
        """生成特定发言的统计图片（今日或昨日）。"""
        ws = self.ob.ws
        try:
            if is_yesterday:
                target_date = (date.today() - timedelta(days=1)).isoformat()
                date_label = "昨日"
            else:
                target_date = date.today().isoformat()
                date_label = "今日"

            # 获取短语统计
            async with self._phrase_stats_lock:
                stats = await self._load_json(self.phrase_stats_path)
            group = stats.get(str(group_id), {})
            day_data = group.get(target_date, {})
            phrase_data = day_data.get(phrase, {})

            if not phrase_data:
                msg = f"「{phrase}」{date_label}还没有统计记录哦~"
                await onebot.send_group_text(ws, group_id, msg)
                return

            total_phrase = sum(u["count"] for u in phrase_data.values())

            # 获取群总消息数（用于饼图占比）
            async with self._stats_lock:
                msg_stats = await self._load_json(self.stats_path)
            msg_group = msg_stats.get(str(group_id), {})
            msg_day = msg_group.get(target_date, {})
            group_total = sum(u["count"] for u in msg_day.values())

            # 排序用户 Top10
            items = sorted(phrase_data.items(), key=lambda kv: kv[1]["count"], reverse=True)[:10]
            top_users: List[dict] = []
            for i, (uid, u) in enumerate(items, 1):
                avatar_b64 = await self._fetch_avatar_b64(uid)
                pct = round(u["count"] * 100 / total_phrase, 1) if total_phrase else 0.0
                top_users.append({
                    "rank": i,
                    "user_id": uid,
                    "nickname": u.get("nickname", uid),
                    "avatar_b64": avatar_b64,
                    "count": u["count"],
                    "percent": pct,
                })

            group_pct = round(total_phrase * 100 / group_total, 1) if group_total else 0.0

            image_b64 = await self.renderer.render_phrase_stats(
                phrase=phrase,
                top_users=top_users,
                total_phrase=total_phrase,
                group_total=group_total,
                group_pct=group_pct,
                date_label=date_label,
            )
            await onebot.send_group_image_b64(ws, group_id, image_b64)
        except Exception as e:
            logger.exception("特定发言统计生成失败: %s", e)
            await onebot.send_group_text(ws, group_id, f"统计生成失败了：{e}")

    # ---------- 签到 / 运势 ----------
    async def _cmd_sign(self, group_id: int, user_id: int, nickname: str) -> None:
        ws = self.ob.ws
        try:
            today = date.today().isoformat()
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            key = f"{group_id}_{user_id}"

            is_duplicate = False
            stored_fortune: Dict[str, Any] = {}

            async with self._sign_lock:
                sign_data = await self._load_json(self.sign_path)
                rec = sign_data.get(key)
                if rec and rec.get("date") == today:
                    # 已签到：标记重复，待锁外重发运势图
                    is_duplicate = True
                    stored_fortune = rec
                    streak = rec.get("streak", 1)
                else:
                    # 昨天签过 → streak +1，否则重置为 1
                    streak = (rec.get("streak", 0) + 1) if rec and rec.get("date") == yesterday else 1
                    fortune, color = self._rng.choice(FORTUNE_LEVELS)
                    yi = self._rng.sample(YI_POOL, 3)
                    ji = self._rng.sample(JI_POOL, 2)
                    lucky = self._rng.randint(1, 100)
                    sign_data[key] = {
                        "date": today,
                        "streak": streak,
                        "fortune": fortune,
                        "fortune_color": color,
                        "yi": yi,
                        "ji": ji,
                        "lucky_number": lucky,
                    }
                    await self._save_json(self.sign_path, sign_data)

            if is_duplicate:
                await onebot.send_group_text(
                    ws, group_id,
                    f"{nickname} 今天已经签到过啦，再给你看一次运势~（已连续签到 {streak} 天）",
                )
                # 有存储的运势数据 → 重新渲染图片（兼容旧数据：无 fortune 字段时跳过）
                if stored_fortune.get("fortune"):
                    avatar_b64 = await self._fetch_avatar_b64(user_id)
                    image_b64 = await self.renderer.render_fortune(
                        nickname=nickname,
                        avatar_b64=avatar_b64,
                        date_str=today,
                        fortune=stored_fortune["fortune"],
                        fortune_color=stored_fortune.get("fortune_color", "#8e7cc3"),
                        yi=stored_fortune.get("yi", []),
                        ji=stored_fortune.get("ji", []),
                        lucky_number=stored_fortune.get("lucky_number", 1),
                        streak=streak,
                    )
                    await onebot.send_group_image_b64(ws, group_id, image_b64)
                return

            # 首次签到：渲染并发送运势图
            avatar_b64 = await self._fetch_avatar_b64(user_id)
            image_b64 = await self.renderer.render_fortune(
                nickname=nickname,
                avatar_b64=avatar_b64,
                date_str=today,
                fortune=fortune,
                fortune_color=color,
                yi=yi,
                ji=ji,
                lucky_number=lucky,
                streak=streak,
            )
            await onebot.send_group_image_b64(ws, group_id, image_b64)
        except Exception as e:
            logger.exception("签到运势生成失败: %s", e)
            await onebot.send_group_text(ws, group_id, f"签到失败了：{e}")

    # ---------- 帮助菜单 ----------
    async def _cmd_help(self, group_id: int) -> None:
        ws = self.ob.ws
        try:
            image_b64 = await self.renderer.render_help(features=self.features)
            await onebot.send_group_image_b64(ws, group_id, image_b64)
        except Exception as e:
            logger.exception("帮助菜单生成失败: %s", e)
            await onebot.send_group_text(ws, group_id, f"帮助菜单生成失败了：{e}")

    # ---------- 功能开关热切换（管理员）----------
    async def _cmd_feature_switch(self, group_id: int, user_id: int, turn_on: bool, arg: str) -> None:
        """管理员热切换功能开关（立即生效并写回 config.json）。

        /stop /start 不带参数 → 列出全部功能当前状态；
        带参数 → 切换指定功能（支持中文别名或英文 key，如：/stop 签到、/start repeat）。
        """
        ws = self.ob.ws
        if str(user_id) != ADMIN_QQ:
            await onebot.send_group_text(ws, group_id, "只有管理员才能使用此指令~")
            return
        try:
            # 不带参数 → 列出全部功能状态
            if not arg:
                lines = []
                for k in DEFAULT_FEATURES:  # 固定顺序展示
                    on = self.features.get(k, True)
                    lines.append(f"{'✅' if on else '⛔'} {FEATURE_NAMES[k]}（{k}）")
                text = (
                    "当前功能开关：\n" + "\n".join(lines)
                    + "\n用法：/stop <功能> 停用，/start <功能> 启用（立即生效，无需重启）"
                )
                await onebot.send_group_text(ws, group_id, text)
                return

            key = FEATURE_ALIASES.get(arg) or FEATURE_ALIASES.get(arg.lower())
            if key is None:
                await onebot.send_group_text(
                    ws, group_id, f"未知功能「{arg}」，发送 /stop 查看可用功能列表~"
                )
                return

            self.features[key] = turn_on  # 原地修改（读多写少，dict 单键赋值线程安全）
            await self._save_config()     # 持久化，重启后保持

            verb = "启用" if turn_on else "停用"
            extra = ""
            if key == "help" and not turn_on:
                extra = "\n⚠️ 帮助菜单入口已停用，发送 /start help 可随时恢复~"
            await onebot.send_group_text(
                ws, group_id, f"已{verb}「{FEATURE_NAMES[key]}」{extra}"
            )
            logger.info("功能开关热切换：%s=%s（by %s）", key, turn_on, user_id)
        except Exception as e:
            logger.exception("功能开关切换失败: %s", e)
            await onebot.send_group_text(ws, group_id, f"切换功能开关失败了：{e}")

    async def _save_config(self) -> None:
        """把功能开关写回 config.json（保留其他字段，原子写）。"""
        async with self._config_lock:
            cfg: Dict[str, Any] = {}
            if os.path.exists(self.config_path):
                try:
                    async with aiofiles.open(self.config_path, "r", encoding="utf-8") as f:
                        content = await f.read()
                    loaded = json.loads(content) if content.strip() else {}
                    if isinstance(loaded, dict):
                        cfg = loaded
                except (json.JSONDecodeError, IOError):
                    cfg = {}
            cfg["features"] = dict(self.features)
            tmp = self.config_path + ".tmp"
            async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
                await f.write(json.dumps(cfg, ensure_ascii=False, indent=2))
            os.replace(tmp, self.config_path)  # 原子写，避免半截文件

    def log_features(self) -> None:
        """启动时输出各功能开关状态（由 main.py 在启动日志中调用）。"""
        parts = [f"{name}={'on' if flag else 'off'}" for name, flag in self.features.items()]
        logger.info("功能开关：%s", " ".join(parts))

    # ---------- 头像拉取 ----------
    async def _fetch_avatar_b64(self, user_id: Any) -> str:
        """通过 aiohttp 拉取 QQ 头像，返回 data URI（失败返回空串）。"""
        if not user_id:
            return ""
        url = f"http://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"
        try:
            async with self.http.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    if data:
                        mime = resp.content_type or "image/png"
                        b64 = base64.b64encode(data).decode("ascii")
                        return f"data:{mime};base64,{b64}"
                logger.warning("拉取头像失败 status=%s uid=%s", resp.status, user_id)
        except Exception as e:
            logger.warning("拉取头像异常 uid=%s: %s", user_id, e)
        return ""
