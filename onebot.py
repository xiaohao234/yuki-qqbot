"""OneBot v11 协议：维护与 NapCat 的 WebSocket 连接、解析事件、封装 Action 调用。

本程序作为 WebSocket Server，NapCat 作为 Client 反向连接进来。
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("onebot")


class OneBotConnection:
    """维护当前与 NapCat 的活动 WebSocket 连接。

    NapCat 断线重连时会建立新连接，此时旧的 ws 会被替换并关闭。
    """

    def __init__(self) -> None:
        self._ws: Optional[Any] = None

    def set_connection(self, ws: Any) -> None:
        if self._ws is not None and self._ws is not ws and not getattr(self._ws, "closed", True):
            logger.warning("检测到新的 WS 连接，关闭旧连接")
            asyncio.create_task(self._ws.close())
        self._ws = ws
        logger.info("当前活动 WS 连接已更新")

    def clear_connection(self, ws: Any) -> None:
        if self._ws is ws:
            self._ws = None
            logger.info("活动 WS 连接已断开，等待 NapCat 重连")

    @property
    def ws(self) -> Optional[Any]:
        return self._ws

    @property
    def connected(self) -> bool:
        return self._ws is not None and not getattr(self._ws, "closed", True)


async def send_action(
    ws: Any, action: str, params: Optional[Dict[str, Any]] = None, echo: Optional[str] = None
) -> None:
    """通过 WS 发送 OneBot v11 Action 请求。

    OneBot v11 约定：{"action": "...", "params": {...}, "echo": "..."}。
    echo 用于客户端关联响应（本程序为 fire-and-forget，不阻塞等待响应）。
    """
    if ws is None or getattr(ws, "closed", True):
        logger.warning("WS 未连接，丢弃 Action: %s", action)
        return
    payload: Dict[str, Any] = {"action": action, "params": params or {}}
    if echo is not None:
        payload["echo"] = echo
    await ws.send_json(payload)


async def send_group_msg(ws: Any, group_id: int, message: str) -> None:
    """发送群消息（message 可为纯文本或包含 CQ 码的字符串）。"""
    await send_action(ws, "send_group_msg", {"group_id": int(group_id), "message": message})


async def send_group_text(ws: Any, group_id: int, text: str) -> None:
    """发送纯文本群消息。"""
    await send_group_msg(ws, group_id, text)


async def send_group_image_b64(ws: Any, group_id: int, image_b64: str) -> None:
    """发送 Base64 图片（封装为 OneBot v11 CQ 码）。"""
    cq = f"[CQ:image,file=base64://{image_b64}]"
    await send_group_msg(ws, group_id, cq)


def parse_event(data: Dict[str, Any]) -> Dict[str, Any]:
    """从原始事件 JSON 中抽取常用字段，缺省值安全。"""
    sender = data.get("sender") or {}
    return {
        "post_type": data.get("post_type"),
        "message_type": data.get("message_type"),
        "sub_type": data.get("sub_type"),
        "raw_message": data.get("raw_message", "") or "",
        "group_id": data.get("group_id"),
        "user_id": data.get("user_id"),
        "sender": sender,
        "nickname": sender.get("nickname") or str(data.get("user_id", "")),
        "self_id": data.get("self_id"),
        "message_id": data.get("message_id"),
        "time": data.get("time"),
    }
