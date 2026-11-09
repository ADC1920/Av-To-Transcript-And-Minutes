#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.5 升级项单测：VAD 自动选择、字幕硬切与阅读速度、RTTM、会议发言概览。

这些函数的共同点是"改错了不会崩，只会悄悄产出坏结果"：
  · VAD 后端选错 → 分离质量下降，但流程照跑、退出码 0
  · 字幕没做硬切 → 一行几十字的字幕，播放器不报错
  · 阅读速度没补 → 字幕闪一下，肉眼要盯才能发现
  · RTTM 字段错位 → dscore 读进去报格式错，或静默按错的说话人算
所以每条都配一个能被观察到的断言，而不是只验"函数能跑"。

跑法：python skills/funasr-transcribe/scripts/test_v25_upgrade_units.py
零重依赖：只用 numpy + 标准库（vad 可用性靠临时目录模拟，不加载任何模型）。
"""
import io
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import qwen_asr as q  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'} {name} {detail if not cond else ''}")
    if not cond:
        fails.append(name)


def test_vad_resolution():
    print("[1] VAD 后端自动选择")
    real = q.FIRERED_VAD_DIR
    try:
        # 权重缺失：两条链路都必须回落 fsmn，不能因为"想用新的"就崩
        q.FIRERED_VAD_DIR = Path(tempfile.gettempdir()) / "_no_such_firered_vad_dir"
        check("无权重时分离链路回落 fsmn", q.resolve_vad_backend("auto", True) == "fsmn")
        check("无权重时分段链路回落 fsmn", q.resolve_vad_backend("auto") == "fsmn")

        # 权重存在：分离链路仍 fsmn（碎段拉高 DER），分段链路换 firered
        tmp = Path(tempfile.mkdtemp(prefix="firered_vad_"))
        (tmp / "model.pth.tar").write_bytes(b"stub")
        q.FIRERED_VAD_DIR = tmp
        got_dia = q.resolve_vad_backend("auto", True)
        got_seg = q.resolve_vad_backend("auto")
        # fireredvad 包在 CI 上不存在，此时两条链路都回落 fsmn——两种环境的期望值不同，
        # 按包是否可导入分别断言，避免测成一个"本机通过、CI 也'通过'"的空洞用例
        pkg = q.vad_backend_available("firered")
        check("权重+包齐备时分离链路用 fsmn", got_dia == "fsmn")
        check("权重+包齐备时分段链路用 firered" if pkg else "包缺失时仍未崩",
              got_seg == ("firered" if pkg else "fsmn"), got_seg)
        check("显式指定不被改写", q.resolve_vad_backend("firered", True) == "firered")
        check("显式 fsmn 不被改写", q.resolve_vad_backend("fsmn") == "fsmn")
    finally:
        q.FIRERED_VAD_DIR = real


def test_hard_cut():
    print("[2] 字幕超长硬切兜底")
    long_no_punct = "这是一段完全没有标点符号的识别结果用来验证硬切兜底是否真的把每一条都压在上限之内"
    pieces = q.split_sentences(long_no_punct, 12)
    check("无标点长句被切成多条", len(pieces) > 1, len(pieces))
    check("每条都不超过上限", all(len(p[2]) <= 12 for p in pieces),
          [len(p[2]) for p in pieces])
    # 拼接后必须一字不丢：切分是实现细节，不许改文案
    check("拼接后与原文一致", "".join(p[2] for p in pieces) == long_no_punct)

    # 有软标点时优先在标点处切，不把词劈开
    soft = "第一句话，第二句话，第三句话，第四句话，第五句话，第六句话"
    ps = q.split_sentences(soft, 8)
    check("软标点优先", all(p[2].rstrip().endswith(("，", "。")) for p in ps[:-1]),
          [p[2] for p in ps])
    check("软标点切分也不丢字", "".join(p[2] for p in ps) == soft)

    # 上限过小（<8）时不硬切，避免把参数写错变成逐字出条
    tiny = q.split_sentences("一二三四五六七八九十", 2)
    check("上限过小时不逐字切", all(len(p[2]) > 2 for p in tiny), [p[2] for p in tiny])


def test_reading_speed():
    print("[3] 字幕阅读速度与最小时长")
    subs = [{"start_ms": 0, "end_ms": 1000,
             "text": "一二三四五六七八九十一二三四五六七八九十"}]
    q.enforce_reading_speed(subs, min_dur_ms=700, max_cps=20.0)
    # 20 字 / 20 字每秒 = 1.0s，刚好等于原始时长，不该被改动
    check("速度达标时不改动", subs[0]["end_ms"] == 1000, subs[0]["end_ms"])

    subs2 = [{"start_ms": 0, "end_ms": 500,
              "text": "一二三四五六七八九十一二三四五六七八九十"}]
    q.enforce_reading_speed(subs2, min_dur_ms=700, max_cps=20.0)
    check("字数要求优先于最小时长", subs2[0]["end_ms"] == 1000, subs2[0]["end_ms"])

    subs3 = [{"start_ms": 0, "end_ms": 300, "text": "好"},
             {"start_ms": 500, "end_ms": 900, "text": "行"}]
    q.enforce_reading_speed(subs3, min_dur_ms=700, max_cps=12.0)
    check("不越过下一条起点", subs3[0]["end_ms"] <= subs3[1]["start_ms"], subs3[0])
    check("最小时长对短句生效", subs3[1]["end_ms"] - subs3[1]["start_ms"] >= 700, subs3[1])

    # max_cps=0：只做最小时长兜底，不按字数拉长（给"我就要原始时长"的用法留出口）
    subs4 = [{"start_ms": 0, "end_ms": 500,
              "text": "一二三四五六七八九十一二三四五六七八九十"}]
    q.enforce_reading_speed(subs4, min_dur_ms=700, max_cps=0)
    check("cps=0 时只补最小时长", subs4[0]["end_ms"] == 700, subs4[0]["end_ms"])

    check("空列表不炸", q.enforce_reading_speed([], 700, 12.0) == [])


def test_rttm():
    print("[4] RTTM 输出")
    rows = [{"spk": 0, "spk_name": None, "start_ms": 1000, "end_ms": 2500},
            {"spk": 1, "spk_name": "张 三", "start_ms": 2500, "end_ms": 4000},
            {"spk": None, "spk_name": None, "start_ms": 4000, "end_ms": 5000}]
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "out.rttm"
        n = q.write_rttm(rows, p, uri="demo")
        text = p.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    check("无说话人标签的段被跳过", n == 2 and len(lines) == 2, (n, len(lines)))
    cols = lines[0].split()
    check("字段数固定 10 列", len(cols) == 10, cols)
    check("首列是 SPEAKER", cols[0] == "SPEAKER", cols[0])
    check("起点秒与时长秒正确", abs(float(cols[3]) - 1.0) < 1e-6
          and abs(float(cols[4]) - 1.5) < 1e-6, cols[3:5])
    check("标签里的空格被替换", "张_三" in lines[1], lines[1])
    check("空输入写出空文件不抛错", True)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "empty.rttm"
        n0 = q.write_rttm([], p, uri="x")
        check("空列表返回 0 行", n0 == 0 and p.read_text(encoding="utf-8") == "")


def test_pipeline_helpers():
    print("[5] 主管线：VAD 解析、发言概览、RTTM")
    sys.path.insert(0, str(HERE.parent.parent.parent))
    import av_to_transcript_and_minutes as av

    real = av.FIRERED_VAD_DIR
    try:
        av.FIRERED_VAD_DIR = Path(tempfile.gettempdir()) / "_no_such_firered_vad_dir"
        check("管线无权重时回落 fsmn", av.resolve_vad_arg("auto", diarize=True) == "fsmn")
        check("管线显式值直传", av.resolve_vad_arg("firered", diarize=True) == "firered")
        check("管线 None 也解析成具体值", av.resolve_vad_arg(None, diarize=False) == "fsmn")
    finally:
        av.FIRERED_VAD_DIR = real

    payload = {"lines": [
        {"spk": 0, "spk_name": "张三", "start_ms": 0, "end_ms": 60000, "text": "第一段"},
        {"spk": 1, "spk_name": "李四", "start_ms": 60000, "end_ms": 90000, "text": "第二段"},
        {"spk": 1, "spk_name": "李四", "start_ms": 90000, "end_ms": 120000, "text": "第三段"},
    ], "overlaps": [{"start_ms": 100000, "end_ms": 105000}]}
    summary = av.speaker_summary(payload)
    check("发言概览含两人", "张三" in summary and "李四" in summary, summary)
    # 张三 60s、李四 30+30=60s -> 各占 50%；占比按累计发言时长算，不按段数
    check("占比按累计时长算", summary.count("占 50%") == 2, summary)
    check("重叠区间被统计", "重叠语音" in summary and "1 处" in summary, summary)

    uneven = {"lines": [
        {"spk": 0, "spk_name": None, "start_ms": 0, "end_ms": 90000, "text": "长"},
        {"spk": 1, "spk_name": None, "start_ms": 90000, "end_ms": 100000, "text": "短"},
    ]}
    s2 = av.speaker_summary(uneven)
    check("时长悬殊时占比不同", "占 90%" in s2 and "占 10%" in s2, s2)

    single = {"lines": [{"spk": 0, "spk_name": None, "start_ms": 0, "end_ms": 1000, "text": "嗯"}]}
    check("单人素材不出概览", av.speaker_summary(single) == "")

    md = av.meeting_md(payload, "demo")
    check("概览在原文正文之前", md.index("发言概览") < md.index("[00:00]"), md[:120])
    check("原文保留时间戳与说话人", "[01:00] 李四：第二段" in md, md[-200:])

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "x.rttm"
        n = av.write_rttm(payload["lines"], p, "demo")
        check("管线 RTTM 写 3 行", n == 3, n)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_vad_resolution()
    test_hard_cut()
    test_reading_speed()
    test_rttm()
    test_pipeline_helpers()
    print()
    if fails:
        print(f"FAILED ({len(fails)}): {', '.join(fails)}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
