#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""超长段切分（静音点吸附）与声纹质心聚合的纯函数单测。

运行: python skills/funasr-transcribe/scripts/test_split_and_cluster.py
零重依赖（仅 numpy）: trim/subtitle 逻辑不碰 torch，CI 的 offline job 可直接跑。

重点守住两条曾经出错的边界：
  ① quiet_point 返回的是采样索引还是毫秒——混用会让切点飞出音频末尾；
  ② 静音点吸附后不得产生零长段或丢掉首尾端点。
"""
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import qwen_asr as q  # noqa: E402

fails = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        fails.append(name)


SR = 16000


def make_speech(total_s=120, quiet_s=(58, 59), amp=0.5):
    y = np.full(SR * total_s, amp, dtype=np.float32)
    a, b = quiet_s
    y[int(SR * a):int(SR * b)] = 0.0
    return y


def test_merge_spans():
    print("[1] _merge_spans 收敛重叠区间")
    check("重叠合并", q._merge_spans([(0, 10), (5, 15), (30, 40), (38, 50)]) == [(0, 15), (30, 50)])
    check("空输入", q._merge_spans([]) == [])
    check("完全包含", q._merge_spans([(0, 100), (10, 20)]) == [(0, 100)])
    check("相邻不合并（gap=0）", q._merge_spans([(0, 10), (10, 20)]) == [(0, 20)])


def test_split_without_audio():
    print("[2] 无音频时退化为等分（保持旧行为）")
    out = q.split_long_segments([(0, 120000)], 60000)
    check("两段等分", out == [(0, 60000), (60000, 120000)], out)
    out3 = q.split_long_segments([(0, 130000)], 60000)
    check("三段等分", len(out3) == 3, out3)
    check("max_ms<=0 原样返回", q.split_long_segments([(0, 120000)], 0) == [(0, 120000)])


def test_quiet_snap():
    print("[3] 有音频时切点吸附到静音窗")
    y = make_speech()
    out = q.split_long_segments([(0, 120000)], 60000, audio=y, sr=SR)
    check("切成两段", len(out) == 2, out)
    if len(out) == 2:
        # 静音在 58–59s，理想切点 60s，搜索窗 ±1.35s 应能覆盖到 58.5s 附近
        cut = out[0][1]
        check("切点毫秒量级（非采样索引）", cut <= 120000, f"cut={cut}")
        check("切点落入静音窗附近", 57000 <= cut <= 61000, f"cut={cut}")
    check("首端保真", out[0][0] == 0, out)
    check("尾端保真", out[-1][1] == 120000, out)


def test_no_zero_length():
    print("[4] 任意 max_ms 下不产生零长段、不越界")
    y = make_speech()
    for max_ms in (5000, 7000, 15000, 30000, 45000, 60000, 100000, 119000):
        out = q.split_long_segments([(0, 120000)], max_ms, audio=y, sr=SR)
        ok_len = all(b > a for a, b in out)
        ok_edge = out[0][0] == 0 and out[-1][1] == 120000
        # 吸附最多把切点推 ±45% step，单段理论上不超过 1.45 倍上限
        ok_cap = all((b - a) <= max_ms * 1.5 + 1000 for a, b in out)
        check(f"max_ms={max_ms}", ok_len and ok_edge and ok_cap,
              f"len={ok_len} edge={ok_edge} cap={ok_cap} out={out[:4]}")


def test_continuous_segments():
    print("[5] 多段输入各自独立切分")
    y = make_speech()
    out = q.split_long_segments([(0, 70000), (70000, 130000)], 60000, audio=y, sr=SR)
    check("覆盖完整区间", out[0][0] == 0 and out[-1][1] == 130000, out)
    check("无零长段", all(b > a for a, b in out), out)


def test_centroid():
    print("[6] centroid_by_label 聚合与归一化")
    embs = np.array([[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]], dtype=np.float32)
    embs = embs / np.linalg.norm(embs, axis=1, keepdims=True)
    c = q.centroid_by_label(embs, [0, 0, 1])
    check("标签齐全", set(c.keys()) == {0, 1}, c.keys())
    check("质心归一化", all(abs(float(np.linalg.norm(v)) - 1.0) < 1e-5 for v in c.values()))
    check("同簇质心偏向多数成员", float(c[0][0]) > 0.99, c[0])
    # 负标签（无向量的短段）应被忽略
    c2 = q.centroid_by_label(embs, [0, 0, -1])
    check("负标签被忽略", set(c2.keys()) == {0}, c2.keys())


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_merge_spans()
    test_split_without_audio()
    test_quiet_snap()
    test_no_zero_length()
    test_continuous_segments()
    test_centroid()
    print()
    if fails:
        print(f"FAILED ({len(fails)}): {', '.join(fails)}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
