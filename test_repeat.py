"""复读逻辑测试脚本。

直接运行：
    python test_repeat.py

无需启动 WS / NapCat / Playwright，纯单元测试 repeat.RepeatDetector。
退出码：0 = 全部通过，1 = 有失败。
"""

from __future__ import annotations

import os
import sys

# 确保能 import 同目录下的 repeat.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from repeat import RepeatDetector  # noqa: E402

TRIGGER_TEXT = "2333"  # 用户们要复读的同一句话
SELF_ID = 99999        # 模拟机器人自己的 QQ 号

_passed = 0
_failed = 0


def step(label: str, got, expect_trigger: bool) -> None:
    """断言一次 check_and_trigger 的返回是否符合预期。"""
    global _passed, _failed
    triggered = got is not None
    ok = triggered == expect_trigger
    mark = "[OK]  " if ok else "[FAIL]"
    detail = ""
    if not ok:
        expect = "复读" if expect_trigger else "不复读"
        actual = "复读" if triggered else "不复读"
        detail = f"  -> 期望 {expect}，实际 {actual}"
    print(f"  {mark} {label}：{'复读' if triggered else '不复读'}{detail}")
    if ok:
        _passed += 1
    else:
        _failed += 1


def main() -> None:
    det = RepeatDetector()
    gid = 111  # 群号

    print("== 场景1：3 个不同用户说同一句，第 3 个触发复读 ==")
    step("用户 A 说 '2333'", det.check_and_trigger(gid, "A", SELF_ID, TRIGGER_TEXT), expect_trigger=False)
    step("用户 B 说 '2333'", det.check_and_trigger(gid, "B", SELF_ID, TRIGGER_TEXT), expect_trigger=False)
    step("用户 C 说 '2333'", det.check_and_trigger(gid, "C", SELF_ID, TRIGGER_TEXT), expect_trigger=True)

    print("\n== 场景2：第 4/5 个用户跟团，不应重复复读 ==")
    step("用户 D 说 '2333'", det.check_and_trigger(gid, "D", SELF_ID, TRIGGER_TEXT), expect_trigger=False)
    step("用户 E 说 '2333'", det.check_and_trigger(gid, "E", SELF_ID, TRIGGER_TEXT), expect_trigger=False)

    print("\n== 场景3：不同文本重置波次，新一轮 3 人触发 ==")
    step("用户 A 说 '哈哈哈'", det.check_and_trigger(gid, "A", SELF_ID, "哈哈哈"), expect_trigger=False)
    step("用户 B 说 '哈哈哈'", det.check_and_trigger(gid, "B", SELF_ID, "哈哈哈"), expect_trigger=False)
    step("用户 C 说 '哈哈哈'", det.check_and_trigger(gid, "C", SELF_ID, "哈哈哈"), expect_trigger=True)

    print("\n== 场景4：同一用户重复说同一句不累计（仍需 3 个不同人）==")
    det2 = RepeatDetector()
    gid2 = 222
    step("用户 X 说 '复读'", det2.check_and_trigger(gid2, "X", SELF_ID, "复读"), expect_trigger=False)
    step("用户 X 又说 '复读'", det2.check_and_trigger(gid2, "X", SELF_ID, "复读"), expect_trigger=False)
    step("用户 Y 说 '复读'", det2.check_and_trigger(gid2, "Y", SELF_ID, "复读"), expect_trigger=False)
    step("用户 Z 说 '复读'", det2.check_and_trigger(gid2, "Z", SELF_ID, "复读"), expect_trigger=True)

    print("\n== 场景5：命令 / 含 CQ / 空文本 / 机器人自身消息 不复读 ==")
    det3 = RepeatDetector()
    gid3 = 333
    step("用户 A 说 '/签到'", det3.check_and_trigger(gid3, "A", SELF_ID, "/签到"), expect_trigger=False)
    step("用户 A 说 '[CQ:image,...]'", det3.check_and_trigger(gid3, "A", SELF_ID, "[CQ:image,file=abc]"), expect_trigger=False)
    step("空文本", det3.check_and_trigger(gid3, "A", SELF_ID, ""), expect_trigger=False)
    step("机器人自己说 '2333'", det3.check_and_trigger(gid3, SELF_ID, SELF_ID, "2333"), expect_trigger=False)

    print("\n" + "=" * 44)
    if _failed == 0:
        print(f"全部通过  共 {_passed} 项")
        sys.exit(0)
    print(f"有失败  通过 {_passed}，失败 {_failed}")
    sys.exit(1)


if __name__ == "__main__":
    main()
