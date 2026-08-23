"""功能开关配置加载。

配置文件：config.json（与 main.py 同目录，可用 BOT_CONFIG_PATH 环境变量覆盖路径）。
文件不存在 / 损坏 / 缺字段 → 全部功能默认启用（保证不影响正常使用）。

示例 config.json：
{
  "features": {
    "stats": true,        // 发言排行（/发言排行 /发言榜 /今日发言榜 /昨日发言 /昨日发言榜）
    "user_stats": true,   // 个人查询（/查发言 /发言）
    "trend": true,        // 发言趋势（/发言趋势 /趋势）
    "phrase_stats": true, // 特定发言统计（/统计发言 /删除统计 /昨日数据 /<短语>）
    "sign": true,         // 签到运势（/签到 /运势）
    "repeat": true,       // 三人复读
    "help": true          // 帮助菜单（/yukihelp //help）
  }
}

注意：被动发言统计（_record_message）始终运行——排行/趋势/个人查询都依赖它，
停用后其他依赖功能会失真，因此不提供开关。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict

logger = logging.getLogger("config")

# 全部功能开关名 → 默认值（True=启用）。新增功能时在此登记
DEFAULT_FEATURES: Dict[str, bool] = {
    "stats": True,         # 发言排行
    "user_stats": True,    # 个人发言查询
    "trend": True,         # 发言趋势
    "phrase_stats": True,  # 特定发言统计
    "sign": True,          # 签到运势
    "repeat": True,        # 三人复读
    "help": True,          # 帮助菜单
}


def load_features() -> Dict[str, bool]:
    """加载功能开关。任何异常都回落到全默认（全部启用），并写警告日志。"""
    feats = dict(DEFAULT_FEATURES)
    path = os.environ.get("BOT_CONFIG_PATH") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.json"
    )
    try:
        if not os.path.exists(path):
            return feats
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        user_feats = cfg.get("features", {}) if isinstance(cfg, dict) else {}
        if not isinstance(user_feats, dict):
            logger.warning("config.json 的 features 不是对象，忽略开关配置")
            return feats
        unknown = []
        for k, v in user_feats.items():
            if k in feats:
                feats[k] = bool(v)
            else:
                unknown.append(k)
        if unknown:
            logger.warning("config.json 含未知功能开关（忽略）：%s", ", ".join(unknown))
    except (json.JSONDecodeError, IOError, OSError) as e:
        logger.warning("config.json 读取失败（%s），全部功能按默认启用", e)
    return feats
