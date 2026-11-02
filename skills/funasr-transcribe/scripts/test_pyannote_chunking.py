#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pyannote 分块与重叠区间逻辑的纯函数单测（不加载模型，只验算法）。

运行: python skills/funasr-transcribe/scripts/test_pyannote_chunking.py
依赖 pyannote.core（随 pyannote.audio 装上）；缺失则整体跳过并返回 0，不阻塞 CI。

守住两条实测踩过的 API 事实：
  ① Annotation.get_overlap() 返回 Timeline，可直接迭代 Segment；
     它没有 itersegments()（凭记忆写会 AttributeError）。
  ② 分块时块间重叠必须小于块长，否则步进为 0/负数会死循环。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import qwen_asr as q  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'} {name} {detail if not cond else ''}")
    if not cond:
        fails.append(name)


def compute_spans(total_s, chunk_s, overlap_s):
    """复刻 run_pyannote 里的分块规划，用来验证步进不会退化为 0"""
    if chunk_s <= 0 or total_s <= chunk_s:
        return [(0.0, total_s)]
    spans, start = [], 0.0
    step = chunk_s - overlap_s
    while start < total_s:
        end = min(start + chunk_s, total_s)
        spans.append((start, end))
        if end >= total_s:
            break
        start += step
    return spans


def test_spans():
    print("[1] 分块规划")
    s = compute_spans(100, 900, 30)
    check("短于块长不分块", s == [(0.0, 100)], s)

    s = compute_spans(2000, 900, 30)
    check("长素材分成多块", len(s) == 3, len(s))
    check("首块从 0 开始", s[0][0] == 0.0, s[0])
    check("末块覆盖到结尾", s[-1][1] == 2000, s[-1])
    check("块间有重叠", s[1][0] < s[0][1], s[:2])
    check("步进为正", all(s[i + 1][0] > s[i][0] for i in range(len(s) - 1)), s)

    # 重叠等于块长会步进 0；run_pyannote 里有兜底把它压到一半
    fixed_overlap = 900 / 2
    s2 = compute_spans(2000, 900, fixed_overlap)
    check("重叠压缩后仍能推进", len(s2) > 1, len(s2))

    s3 = compute_spans(901, 900, 30)
    check("刚超块长也要覆盖完整", s3[0][0] == 0.0 and s3[-1][1] == 901, s3)


def test_overlap_api():
    print("[2] get_overlap 的真实 API（返回 Timeline，可迭代 Segment）")
    try:
        from pyannote.core import Annotation, Segment
    except Exception as e:
        print(f"  SKIP pyannote.core 不可用: {type(e).__name__}")
        return
    ann = Annotation()
    ann[Segment(0, 5), "A"] = "A"
    ann[Segment(3, 8), "B"] = "B"
    ov = ann.get_overlap()
    check("get_overlap 返回可迭代对象", hasattr(ov, "__iter__"), type(ov).__name__)
    check("没有 itersegments 方法", not hasattr(ov, "itersegments"),
          "若出现了说明上游 API 变了，需复核本脚本用的迭代方式")
    segs = [(s.start, s.end) for s in ov]
    check("重叠区正确（3-5）", segs == [(3.0, 5.0)], segs)

    # 无重叠时应为空，不能报错
    ann2 = Annotation()
    ann2[Segment(0, 5), "A"] = "A"
    ann2[Segment(6, 9), "B"] = "B"
    check("无重叠返回空", list(ann2.get_overlap()) == [])


def test_merge_spans():
    print("[3] 跨块重叠区收敛")
    check("相邻重叠合并", q._merge_spans([(0, 10), (5, 15)]) == [(0, 15)])
    check("不相邻保留", q._merge_spans([(0, 10), (20, 30)]) == [(0, 10), (20, 30)])
    check("空输入", q._merge_spans([]) == [])


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_spans()
    test_overlap_api()
    test_merge_spans()
    print()
    if fails:
        print(f"FAILED ({len(fails)}): {', '.join(fails)}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
