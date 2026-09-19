"""三人复读检测器。

同一群内连续有 3 个**不同成员**说了同一句"可复读文本"时，触发一次复读；
触发后置 fudued=True，后续同句跟团不再重复发送，直到出现不同的文本才重置该群波次。

可复读文本：纯文本，或只含 QQ 表情（含商城大表情）的文本+表情组合。
同句判定用 CQ 归一化文本（见 cqtext.py）：商城大表情会被归一化为 [表情:xxx] 短标记，
因此「3 个人发同一个拜谢表情」也能触发复读；触发时发送**原始消息**（含原 CQ 码），
保证表情原样复现。

跳过项（不计入、也不触发，且会重置该群波次）：
- 机器人自身消息（由调用方在更上层过滤，这里再保险判一次 self_id）
- 以 ``/`` 开头的命令
- 含 face 以外 CQ 码的消息（图片 / 语音 / @ / 卡片等）
- 归一化后为空文本
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import cqtext

logger = logging.getLogger("repeat")


class RepeatDetector:
    TRIGGER_COUNT = 3  # 第 3 个不同成员说同一句时触发

    def __init__(self) -> None:
        # group_id -> {"text": str(归一化), "users": [user_id,...], "fudued": bool}
        self._state: Dict[Any, Dict[str, Any]] = {}

    def check_and_trigger(
        self, group_id: Any, user_id: Any, self_id: Any, text: str
    ) -> Optional[str]:
        """检查是否应复读。

        返回需要复读的**原始文本**（触发时），或 None（不触发）。
        同句判定使用归一化文本；该方法维护每群的波次状态，是复读"只发一次"的核心。
        """
        if self_id is not None and user_id == self_id:
            return None  # 机器人自身消息，忽略

        if not text or text.startswith("/") or cqtext.has_non_face_cq(text):
            # 命令 / 图片 / 语音 / @ / 卡片等 → 不可复读，视为不同消息，重置该群波次
            self._state.pop(group_id, None)
            return None

        key = cqtext.normalize(text).strip()
        if not key:
            self._state.pop(group_id, None)
            return None

        st = self._state.get(group_id)
        if st and st["text"] == key:
            # 同一波次延续：仅累计未出现过的成员
            # （已触发复读后不再累计：users 只用于 len>=3 判断，防止同句大规模跟团时列表无限增长）
            if user_id not in st["users"] and not st["fudued"]:
                st["users"].append(user_id)
        else:
            # 不同的文本 → 新一波次
            st = {"text": key, "users": [user_id], "fudued": False}
            self._state[group_id] = st

        if len(st["users"]) >= self.TRIGGER_COUNT and not st["fudued"]:
            st["fudued"] = True  # 关键：本波次只复读这一次
            logger.info(
                "复读触发 group=%s text=%r users=%s", group_id, key, st["users"]
            )
            return text  # 发送原始消息（含原 CQ 码），表情原样复现
        return None
