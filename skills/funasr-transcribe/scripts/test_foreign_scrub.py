# -*- coding: utf-8 -*-
"""碎段外语兜底与扩边确认的离线单测（零网络）。

运行：python scripts/test_foreign_scrub.py
覆盖：is_foreign_script 语种字符识别（含泰文）/ scrub_foreign_filler 阈值与混排保护 /
      scrub_units 批量计数 / rescue_short_segments 只确认不改写（mock infer）。
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qwen_asr import (is_foreign_script, rescue_short_segments,
                      scrub_foreign_filler, scrub_units)

fails = []


def check(name, cond, detail=""):
    if cond:
        print(f"[PASS] {name}")
    else:
        print(f"[FAIL] {name}  {detail}")
        fails.append(name)


# ---- is_foreign_script：四种外来字符 + 中文/英文/标点不误判 ----
check("谚文识别", is_foreign_script("아"))
check("假名识别", is_foreign_script("うん"))
check("越南语声调识别", is_foreign_script("Ừm"))
check("泰文识别", is_foreign_script("อือ"))
check("混合串识别", is_foreign_script("Thế nào"))
check("中文不误判", not is_foreign_script("嗯。"))
check("英文不误判", not is_foreign_script("hello"))
check("空串不误判", not is_foreign_script(""))

# ---- scrub_foreign_filler：短杂字替换，正常内容不动 ----
cases_scrub = [
    ("아.", "嗯。", True),
    ("うん。", "嗯。", True),
    ("Ừm.", "嗯。", True),
    ("네.", "嗯。", True),
    ("Thế nào?", "嗯。", True),
    ("ええええ。", "嗯。", True),
    ("嗯。", "嗯。", False),            # 已正常，不重复处理
    ("对。", "对。", False),            # 无外来字符
    ("但是", "但是", False),
    ("这个是什么", "这个是什么", False),
    ("hello world", "hello world", False),
]
for src, want, want_hit in cases_scrub:
    got, hit = scrub_foreign_filler(src)
    check(f"scrub {src!r}", got == want and hit == want_hit, f"得到 {got!r}/{hit}")

# 超长含外来字符的句子不替换：真实混语可能性大
long_mixed = "这是一段很长的中文内容里面夹了一个そうですね的说法总共超过八个字"
got, hit = scrub_foreign_filler(long_mixed)
check("超长混语不替换", not hit and got == long_mixed, f"得到 {got!r}")

# 中外混排（有中文也有外来字符）即使是短段也不替换，避免误删真实内容
got, hit = scrub_foreign_filler("嗯아")
check("中外混排不替换", not hit, f"得到 {got!r}")

# 空串与纯空白原样返回
for blank in ("", "   ", "\n"):
    got, hit = scrub_foreign_filler(blank)
    check(f"空白 {blank!r} 不处理", not hit and got == blank)

# ---- extract_filler 与扩边提取已移除（实测仅 24% 准确率），
#      扩边只做语音有效性确认，文本统一走字符兜底 ----

# ---- scrub_units：批量处理并返回计数 ----
units = [{"text": "아."}, {"text": "嗯。"}, {"text": "うん。"}, {"text": "这是正常句子"}]
n = scrub_units(units, log=lambda *_: None)
check("scrub_units 计数", n == 2, f"期望 2，得到 {n}")
check("scrub_units 结果", [u["text"] for u in units] == ["嗯。", "嗯。", "嗯。", "这是正常句子"],
      str([u["text"] for u in units]))

# ---- rescue_short_segments：只确认，不改写文本 ----
def fake_infer(paths, resume=None):
    """模拟云端：返回有内容的结果，验证不改写文本"""
    return [{"transcription": "联系了啊，他联系了我。"} for _ in paths]


audio = np.zeros(16000 * 20, dtype="float32")      # 20 秒静音，足够覆盖测试段
units_rescue = [
    {"start_ms": 1000, "end_ms": 1300, "text": "아."},          # 应确认
    {"start_ms": 3000, "end_ms": 5000, "text": "正常内容"},      # 无外来字符，跳过
    {"start_ms": 6000, "end_ms": 9000, "text": "うん。"},        # 超 1500ms，跳过
    {"start_ms": 10000, "end_ms": 10300, "text": "네."},        # 应确认
]
orig_texts = [u["text"] for u in units_rescue]
confirmed, silent = rescue_short_segments(units_rescue, audio,
                                          {"pad_ms": 1000, "max_ms": 1500}, fake_infer)
check("rescue 确认数量", confirmed == 2, f"期望 2，得到 {confirmed}")
check("rescue 静音数量", silent == 0, f"期望 0，得到 {silent}")
check("rescue 不改写文本", [u["text"] for u in units_rescue] == orig_texts,
      str([u["text"] for u in units_rescue]))
check("rescue 打确认标记", units_rescue[0].get("speech_confirmed") is True
      and units_rescue[3].get("speech_confirmed") is True)
check("rescue 跳过非外语段", "speech_confirmed" not in units_rescue[1])
check("rescue 跳过长段", "speech_confirmed" not in units_rescue[2])

# 全部不是外语时不应调用 infer
def fail_infer(paths, resume=None):
    raise AssertionError("无目标时不该调用识别")


noop_units = [{"start_ms": 0, "end_ms": 500, "text": "正常"}]
c0, s0 = rescue_short_segments(noop_units, audio, {"pad_ms": 1000, "max_ms": 1500}, fail_infer)
check("无目标不调用识别", c0 == 0 and s0 == 0)

# 扩边后无文字：判为静音
def empty_infer(paths, resume=None):
    return [{"transcription": ""} for _ in paths]


units_silent = [{"start_ms": 1000, "end_ms": 1300, "text": "아."}]
c1, s1 = rescue_short_segments(units_silent, audio, {"pad_ms": 1000, "max_ms": 1500}, empty_infer)
check("扩边无字判静音", c1 == 0 and s1 == 1 and units_silent[0].get("silent") is True,
      f"{c1}/{s1}")

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    sys.exit(1)
print("全部通过")
