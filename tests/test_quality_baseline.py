#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合成素材回归基线：用带标注的素材守住"转得准"而不只是"跑得通"。

与 test_pipeline_e2e.py 的分工：
  e2e           —— 验管道接通（产物齐不齐、退出码、幂等）
  本脚本        —— 验质量（DER 说人对不对、CER 说字对不对、overlaps 说抢话抓没抓到）

素材由 scripts/make_fixtures.py 生成（音频不入库，见 .gitignore）。缺素材时
自动调生成脚本；环境不支持（非 Windows / 无 ffmpeg）则整体跳过并返回 0，
不阻塞 CI 的 Linux runner。

用法：
  python tests/test_quality_baseline.py              # 跑全部基线
  python tests/test_quality_baseline.py --no-generate  # 不自动补素材，缺了就跳过
  python tests/test_quality_baseline.py --der-max 0.25 # 自定义 DER 门槛

判定门槛（本机实测值留了余量）：
  DER ≤ 0.25   双人清晰对话，实测 0.10-0.14；放到 0.25 抗模型小版本波动
  CER ≤ 0.30   TTS 素材本身无错字，但标点/数字写法差异会吃掉一部分，实测 0.05-0.15
"""
import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / ".tmp" / "fixtures"
MAKE_FIXTURES = ROOT / "scripts" / "make_fixtures.py"
BENCHMARK = ROOT / "benchmark_quality.py"

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'} {name} {detail if not cond else ''}")
    if not cond:
        fails.append(name)


def ensure_fixtures(auto=True):
    """素材缺失时调生成脚本。返回 True 表示可用。"""
    need = ["dialog.wav", "dialog.wav.ref.json", "overlap.wav", "single.wav"]
    if all((FIXTURES / n).exists() for n in need):
        return True
    if not auto:
        print(f"[跳过] 缺少素材（{FIXTURES}），且指定了 --no-generate")
        return False
    if not MAKE_FIXTURES.exists():
        print(f"[跳过] 找不到 {MAKE_FIXTURES}")
        return False
    print(f"[准备] 素材缺失，调用 {MAKE_FIXTURES.name} 生成…")
    r = subprocess.run([sys.executable, str(MAKE_FIXTURES), "--out", str(FIXTURES)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(f"[跳过] 素材生成失败（环境不支持则正常）：\n{(r.stdout or r.stderr or '')[-500:]}")
        return False
    return all((FIXTURES / n).exists() for n in need)


def norm_text(s):
    """CER 比对口径：去标点空白、全半角归一、英文小写。
    TTS 输出与实际转写的差异主要在标点和数字写法，不算识别错误。"""
    s = unicodedata.normalize("NFKC", s or "").lower()
    return re.sub(r"[\s\W_]+", "", s, flags=re.UNICODE)


def edit_distance(a, b):
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(ref, hyp):
    r, h = norm_text(ref), norm_text(hyp)
    if not r:
        return None
    return edit_distance(r, h) / len(r)


def der(ref_segs, hyp_segs):
    """说话人分离错误率。

    优先用 pyannote.metrics 的标准实现（0.25s collar，论文口径）：
    自写的网格近似在标签映射上容易出错（实测同一份完美标注被误判成 0.50）。
    缺 pyannote 环境时退回简化实现，只作趋势参考，门槛相应放宽。
    """
    try:
        from pyannote.core import Annotation, Segment
        from pyannote.metrics.diarization import DiarizationErrorRate
        ref = Annotation()
        for s in ref_segs:
            ref[Segment(s["start_ms"] / 1000, s["end_ms"] / 1000)] = s["spk"]
        hyp = Annotation()
        for s in hyp_segs:
            if s.get("spk") is None:
                continue
            hyp[Segment(s["start_ms"] / 1000, s["end_ms"] / 1000)] = str(s["spk"])
        # skip_overlap=False：重叠段也计入，与 README 里引用的 DER 口径一致
        metric = DiarizationErrorRate(collar=0.25, skip_overlap=False)
        return abs(metric(ref, hyp))
    except ImportError:
        return _der_simple(ref_segs, hyp_segs)


def _der_simple(ref_segs, hyp_segs):
    """无 pyannote 时的网格近似：100ms 切片比对，无 collar。
    只用于"变好还是变坏"的趋势判断，绝对值不可与论文 DER 直接比。"""
    if not ref_segs:
        return None
    tmax = max(max(s["end_ms"] for s in ref_segs), max((s["end_ms"] for s in hyp_segs), default=0))
    step = 100
    grid_n = max(1, tmax // step)

    def at(segs, t):
        for s in segs:
            if s["start_ms"] <= t < s["end_ms"]:
                return s["spk"]
        return None

    # 标签映射：按共现时长投票，不只认首次出现（首个回合可能正好在切点上）
    votes = {}
    for i in range(grid_n):
        t = i * step + step / 2
        r, h = at(ref_segs, t), at(hyp_segs, t)
        if r and h is not None:
            votes[(h, r)] = votes.get((h, r), 0) + 1
    mapping, taken = {}, set()
    for (h, r), n in sorted(votes.items(), key=lambda kv: -kv[1]):
        if h in mapping or r in taken:
            continue
        mapping[h] = r
        taken.add(r)

    err = 0
    for i in range(grid_n):
        t = i * step + step / 2
        r, h = at(ref_segs, t), at(hyp_segs, t)
        h = mapping.get(h, h) if h is not None else None
        if (r and not h) or (h and not r) or (r and h and r != h):
            err += 1
    return err / grid_n


def find_hyp_json(media: Path):
    """管线产物布局是 <out_dir>/<media_stem>/转写结果*.json（两层层级）。
    不用 --flat：回归要读结构化 json，平铺收尾会连它一起删掉。"""
    work = FIXTURES / media.stem / media.stem
    cands = [c for c in work.glob("转写结果*.json") if not c.name.endswith(".ref.json")]
    if not cands:
        # 兜底：布局万一调整过，退回递归找，别让回归因路径变化误报管线失败
        cands = [c for c in (FIXTURES / media.stem).rglob("转写结果*.json")
                 if not c.name.endswith(".ref.json")]
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def run_pipeline(media: Path, extra=()):
    """跑一次完整管线拿结构化结果。--force 保证用当前代码而非旧缓存。
    不默认加 --meeting：它与 --no-diarize 互斥，由调用方按素材类型决定。"""
    out = FIXTURES / media.stem
    cmd = [sys.executable, str(ROOT / "av_to_transcript_and_minutes.py"),
           str(media), "--force", "-o", str(out)] + list(extra)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(f"  [管线失败] {(r.stderr or r.stdout or '')[-600:]}")
        return None
    return find_hyp_json(media)


def test_dialog_der(der_max):
    print("[1] 双人对话：说话人分离质量（DER）")
    media = FIXTURES / "dialog.wav"
    ref_path = FIXTURES / "dialog.wav.ref.json"
    hyp_path = run_pipeline(media, ["--meeting", "--diarize-engine", "pyannote", "--num-speakers", "2"])
    if not hyp_path:
        check("管线产出结构化结果", False, "找不到转写结果 json")
        return
    check("管线产出结构化结果", True)

    payload = json.loads(hyp_path.read_text(encoding="utf-8"))
    check("结果带 schema_version", "schema_version" in payload, list(payload)[:5])
    lines = payload.get("lines") or []
    check("lines 非空", bool(lines), f"line 数={len(lines)}")

    hyp_segs = [{"start_ms": l.get("start_ms", 0), "end_ms": l.get("end_ms", 0),
                 "spk": l.get("spk")} for l in lines]
    ref_segs = json.loads(ref_path.read_text(encoding="utf-8"))["speakers"]
    got_spk = len({s["spk"] for s in hyp_segs if s["spk"] is not None})
    check("分出 2 个说话人", got_spk == 2, f"实得 {got_spk}")

    d = der(ref_segs, hyp_segs)
    check(f"DER ≤ {der_max}", d is not None and d <= der_max, f"DER={d}")

    # 文本正确性：TTS 原文里的关键数字必须出现（转写质量的最低底线）
    text = "".join(l.get("text", "") for l in lines)
    for kw in ("百分之十五", "百分之二十三", "库存周转率"):
        check(f"含关键词「{kw}」", kw in text or norm_text(kw) in norm_text(text), text[:60])


def test_overlap(der_max):
    print("[2] 抢话素材：重叠检测")
    media = FIXTURES / "overlap.wav"
    hyp_path = run_pipeline(media, ["--meeting", "--diarize-engine", "pyannote"])
    if not hyp_path:
        check("管线产出结构化结果", False, "找不到转写结果 json")
        return
    payload = json.loads(hyp_path.read_text(encoding="utf-8"))
    ov = payload.get("overlaps") or []
    # 重叠区是"检测到几处"的信号：合成素材两句交叠约 5s，至少要抓到一处
    check("检测到重叠区间", len(ov) >= 1, f"overlaps={len(ov)}")
    if ov:
        total = sum((o["end_ms"] - o["start_ms"]) for o in ov) / 1000
        check("重叠总时长 > 1s", total > 1.0, f"{total:.1f}s")


def test_single_cer(cer_max):
    print("[3] 单人素材：字错误率（CER）")
    media = FIXTURES / "single.wav"
    hyp_path = run_pipeline(media, ["--no-diarize"])
    if not hyp_path:
        check("管线产出结构化结果", False, "找不到转写结果 json")
        return
    payload = json.loads(hyp_path.read_text(encoding="utf-8"))
    # --no-diarize 走整段识别路径，结果在顶层 text；分离路径才有 lines。
    # 两条路都要能取到文本，否则回归会因为布局差异误判转写为空。
    lines = payload.get("lines") or []
    hyp = "".join(l.get("text", "") for l in lines) if lines else (payload.get("text") or "")
    ref = "今天讨论三件事，第一是季度目标，第二是人员安排，第三是预算调整。"
    c = cer(ref, hyp)
    check(f"CER ≤ {cer_max}", c is not None and c <= cer_max, f"CER={c}  hyp={hyp[:50]}")
    check("转写非空", bool(norm_text(hyp)), repr(hyp[:30]))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="合成素材质量回归基线")
    ap.add_argument("--no-generate", action="store_true", help="缺素材时不自动生成")
    ap.add_argument("--der-max", type=float, default=0.25, help="DER 上限（默认 0.25）")
    ap.add_argument("--cer-max", type=float, default=0.30, help="CER 上限（默认 0.30）")
    ap.add_argument("--only", choices=["dialog", "overlap", "single"], help="只跑某一组")
    args = ap.parse_args()

    if not ensure_fixtures(auto=not args.no_generate):
        print("SKIP：素材不可用（环境不支持或未生成），本次不判失败")
        return 0

    tasks = {"dialog": lambda: test_dialog_der(args.der_max),
             "overlap": lambda: test_overlap(args.der_max),
             "single": lambda: test_single_cer(args.cer_max)}
    for name, fn in tasks.items():
        if args.only and args.only != name:
            continue
        fn()

    print()
    if fails:
        print(f"FAILED ({len(fails)}): {', '.join(fails)}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
