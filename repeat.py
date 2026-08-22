"""三人复读检测器。

同一群内连续有 3 个**不同成员**说了同一句纯文本时，触发一次复读；
触发后置 fudued=True，后续同句跟团不再重复发送，直到出现不同的纯文本才重置该群波次。

跳过项（不计入、也不触发）：
- 机器人自身消息（由调用方在更上层过滤，这里再保险判一次 self_id）
- 以 ``/`` 开头的命令
- 含 ``[CQ:`` 的消息（图片 / @ / 表情等）
- 空文本
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("repeat")


class RepeatDetector:
    TRIGGER_COUNT = 3  # 第 3 个不同成员说同一句时触发

    def __init__(self) -> None:
        # group_id -> {"text": str, "users": [user_id,...], "fudued": bool}
        self._state: Dict[Any, Dict[str, Any]] = {}

    @staticmethod
    def _is_plain_text(text: str) -> bool:
        if not text:
            return False
        if text.startswith("/"):  # 命令，不复读
            return False
        if "[CQ:" in text:  # 含图片 / @ / 表情等，不复读
            return False
        return True

    def check_and_trigger(
        self, group_id: Any, user_id: Any, self_id: Any, text: str
    ) -> Optional[str]:
        """检查是否应复读。

        返回需要复读的文本（触发时），或 None（不触发）。
        该方法维护每群的波次状态，是复读“只发一次”的核心。
        """
        if self_id is not None and user_id == self_id:
            return None  # 机器人自身消息，忽略

        if not self._is_plain_text(text):
            # 非“可复读文本”（命令/图片/@等）→ 视为不同消息，重置该群波次
            self._state.pop(group_id, None)
            return None

        st = self._state.get(group_id)
        if st and st["text"] == text:
            # 同一波次延续：仅累计未出现过的成员
            # （已触发复读后不再累计：users 只用于 len>=3 判断，防止同句大规模跟团时列表无限增长）
            if user_id not in st["users"] and not st["fudued"]:
                st["users"].append(user_id)
        else:
            # 不同的纯文本 → 新一波次
            st = {"text": text, "users": [user_id], "fudued": False}
            self._state[group_id] = st

        if len(st["users"]) >= self.TRIGGER_COUNT and not st["fudued"]:
            st["fudued"] = True  # 关键：本波次只复读这一次
            logger.info(
                "复读触发 group=%s text=%r users=%s", group_id, text, st["users"]
            )
            return text
        return None
