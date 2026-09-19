"""CQ 码消息归一化工具。

QQ 商城大表情（NapCat 的 face 消息带 raw 转义 JSON 字段，如
[CQ:face,id=297,raw={"faceIndex":297&#44;"faceText":"/拜谢"&#44;...}]）
会让原始消息变得巨长，直接拿原文做短语匹配/复读键会误匹配、也会撑爆统计图界面。

本模块把 CQ 码归一化为短 token（纯文本部分原样保留）：
- face（QQ 自带表情/商城大表情）→ [表情:faceText]（取不到 faceText 时用 id）
- 其他类型（图片/语音/@/卡片等）→ [图片] [语音] [@qq] 等短标记

归一化结果用于：特定发言统计的短语匹配、复读机的同句判定。
发送复读时仍使用原始消息（含原 CQ 码），保证表情原样复现。
"""

from __future__ import annotations

import re
from typing import Dict

# CQ 码：值中的逗号/中括号会被转义（&#44; &#91; &#93;），因此 [CQ: 到 ] 之间没有裸的中括号
_CQ_RE = re.compile(r"\[CQ:([a-zA-Z0-9_.-]+)([^\[\]]*)\]")
_CQ_UNESCAPE = (("&amp;", "&"), ("&#44;", ","), ("&#91;", "["), ("&#93;", "]"))

# 常见 CQ 类型 → 中文短标记
_TYPE_NAMES: Dict[str, str] = {
    "image": "图片",
    "record": "语音",
    "video": "视频",
    "json": "卡片",
    "xml": "卡片",
    "forward": "合并转发",
    "reply": "回复",
    "redbag": "红包",
    "rps": "猜拳",
    "dice": "骰子",
    "shake": "抖一抖",
    "poke": "戳一戳",
    "weather": "天气",
    "sign": "签到",
}


def _unescape_cq_value(v: str) -> str:
    for esc, raw in _CQ_UNESCAPE:
        v = v.replace(esc, raw)
    return v


def _face_token(params: str) -> str:
    """face CQ 码 → [表情:xxx]。优先取 raw 里的 faceText（去掉开头的 /），否则用 id。"""
    kv: Dict[str, str] = {}
    for part in params.split(","):
        k, _, v = part.partition("=")
        if k:
            kv[k.strip()] = _unescape_cq_value(v)
    label = ""
    raw = kv.get("raw")
    if raw:
        m = re.search(r'"faceText"\s*:\s*"([^"]*)"', raw)
        if m:
            label = m.group(1).strip().lstrip("/").strip()
    if not label:
        label = kv.get("id", "").strip()
    return f"[表情:{label}]" if label else "[表情]"


def has_non_face_cq(text: str) -> bool:
    """消息里是否含有 face 以外的 CQ 码（图片/@/卡片等，复读机据此排除）。"""
    return any(m.group(1) != "face" for m in _CQ_RE.finditer(text))


def normalize(text: str) -> str:
    """把含 CQ 码的消息归一化为短 token 文本；纯文本消息原样返回。"""
    if not text or "[CQ:" not in text:
        return text

    def repl(m: "re.Match[str]") -> str:
        ctype, params = m.group(1), m.group(2)
        if ctype == "face":
            return _face_token(params)
        if ctype == "at":
            q = re.search(r"qq=(-?\d+)", params)
            return f"[@{q.group(1)}]" if q else "[@]"
        return f"[{_TYPE_NAMES.get(ctype, ctype)}]"

    return _CQ_RE.sub(repl, text)
