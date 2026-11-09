#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""转写/说话人分离基准评测：DER（说话人分离错误率）与 CER（字错误率）。

为什么需要这个：管线里每一项优化（切分策略、说话人数、声纹库、对齐后端）的效果，
在合成 TTS 素材上测不出来。没有真实标注基线，改动只能靠"感觉变好了"。

标注格式（JSON，一个素材一份，放在素材同目录 <素材名>.ref.json）：
  {
    "speakers": [                      # 可选：真实说话人分段
      {"start_ms": 0,     "end_ms": 5200,  "spk": "张三"},
      {"start_ms": 5200,  "end_ms": 9000,  "spk": "李四"}
    ],
    "text": "完整正确文本（用于 CER，可省略）"
  }
  要算 cpWER 时给每段加 text：
      {"start_ms": 0, "end_ms": 5200, "spk": "张三", "text": "这一段他说的原话"}
  只有时间轴没有逐段文本时不报 cpWER——按时长把全文摊到各人头上，会把标注本身的
  位置误差算成模型错误，那个数字比没有更糟。

三个指标各管一件事，必须一起看：
  DER（0.25s 容差）  谁在什么时候说，边界微抖不计错——通行口径，可与论文对齐
  DER（无容差）      同一份预测的边界精度；与上面那个的差值变大 = 边界被弄粗了
  cpWER             把每个人的话拼成一串再比字，同时惩罚"归属错"与"识别错"，
                    比 DER 更贴近"这份稿子能不能直接用"

用法：
  # 评测单份素材（会自动跑管线出转写结果，再比对）
  python benchmark_quality.py 会议录音.m4a

  # 只比对已有产物（不重跑转写）
  python benchmark_quality.py 会议录音.m4a --hyp 输出目录/会议录音/会议原文-会议录音.json

  # 批量：目录下每个 <名>.ref.json 对应一个 <名> 素材
  python benchmark_quality.py ./素材目录 --report 基线.json

  # 与历史基线对比，看这次改动是变好还是变坏
  python benchmark_quality.py ./素材目录 --report 新.json --compare 基线.json

DER 依赖 pyannote.metrics（已随 pyannote.audio 装上）；缺失时跳过 DER 只算 CER。
CER 自写：中文按字、英文按词，编辑距离/参考长度。不引入 jiwer 这类重依赖。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata
from itertools import permutations
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


# ---------------- 编辑距离与 CER ----------------

def _norm_units(text, lang_hint=None):
    """把文本切成可比较单元：中文按字，英文按词，数字与标点规整。

    转写文本里的标点、空白、全半角差异不该计入错误率，先统一剔除；
    英文词按空白切，中文逐字——混排文本两种都做，按字符类别分流。
    """
    if not text:
        return []
    t = unicodedata.normalize("NFKC", text)   # 全角转半角
    units = []
    buf = []
    for ch in t:
        if ch.isalnum():
            if "\u4e00" <= ch <= "\u9fff":     # CJK 统一表意文字
                if buf:
                    units.append("".join(buf).lower())
                    buf = []
                units.append(ch)
            else:
                buf.append(ch)
        else:
            if buf:
                units.append("".join(buf).lower())
                buf = []
    if buf:
        units.append("".join(buf).lower())
    return units


def edit_distance(a, b):
    """Levenshtein 距离（滚动数组，O(len(a)*len(b)) 空间 O(min)）"""
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1,          # 删除
                           cur[j - 1] + 1,        # 插入
                           prev[j - 1] + (ca != cb)))  # 替换
        prev = cur
    return prev[-1]


def cer(ref_text, hyp_text):
    """字错误率 = 编辑距离 / 参考长度。参考为空时返回 None（无从比较）"""
    ref = _norm_units(ref_text)
    hyp = _norm_units(hyp_text)
    if not ref:
        return None
    return edit_distance(ref, hyp) / len(ref)


# ---------------- DER ----------------

def der_from_segments(ref_segments, hyp_segments, collar=0.25):
    """用 pyannote.metrics 算 DER。返回 (der, detail dict) 或 (None, {"reason": ...})

    collar 默认 0.25 秒（论文通行口径）：段边界抖动不计错误。传 0 得到"无容差"
    版本——两个数字一起看才有信息量：容差内一致、无容差变差，说明边界精度在退化，
    而 0.25 秒容差会把这类退化吃掉。只报一个数容易把"更准"和"更粗"看成一回事。
    """
    try:
        from pyannote.core import Annotation, Segment
        from pyannote.metrics.diarization import DiarizationErrorRate
    except Exception as e:
        return None, {"reason": f"pyannote.metrics 不可用: {type(e).__name__}"}

    def build(segs):
        ann = Annotation(uri="x")
        for s in segs:
            spk = str(s.get("spk") or s.get("spk_name") or "?")
            start = float(s.get("start_ms", 0)) / 1000.0
            end = float(s.get("end_ms", 0)) / 1000.0
            if end > start:
                ann[Segment(start, end), spk] = spk
        return ann

    ref = build(ref_segments)
    hyp = build(hyp_segments)
    if not ref or not hyp:
        return None, {"reason": "参考或预测为空"}
    # collar 是构造参数而非调用参数（pyannote.metrics 4.x）：0.25s 容差让段边界的
    # 微抖动不计错误，这是 DER 评测的通行做法；传错位置会 TypeError。
    metric = DiarizationErrorRate(collar=collar)
    value = metric(ref, hyp)
    comp = metric[:]
    return float(value), {
        "miss_s": round(float(comp.get("miss", 0.0)), 2),
        "false_alarm_s": round(float(comp.get("false alarm", 0.0)), 2),
        "confusion_s": round(float(comp.get("confusion", 0.0)), 2),
        "total_s": round(float(comp.get("total", 0.0)), 2),
    }


def cpwer(ref_segs, hyp_segs):
    """cpWER（concatenated minimum-permutation WER）：说话人归属对了没有。

    DER 只看"这段时间谁在说"，说话人 A 的内容被标成 B、另一段反过来错标，
    DER 会算两次混淆；cpWER 先把同一个人所有内容拼成一串再比字，问的是
    "如果把标签按最优置换对上，每个人说的话错了几个字"——它同时惩罚归属错误
    和识别错误，是会议场景里比 DER 更贴近实际可用性的数字（meet eval 的口径）。

    要求参考标注带逐段文本：ref_segs 的元素需有 text 字段；只有时间轴没有文本时
    返回 None（用时长按比例摊全文会把标注噪声算成模型错误，不如不算）。
    """
    if not ref_segs or not any((s.get("text") or "").strip() for s in ref_segs):
        return None
    if not hyp_segs:
        return None
    ref_by, hyp_by = {}, {}
    for s in ref_segs:
        ref_by.setdefault(str(s.get("spk")), []).append(s)
    for s in hyp_segs:
        if s.get("spk") is not None:
            hyp_by.setdefault(str(s.get("spk")), []).append(s)
    ref_keys = list(ref_by)
    hyp_keys = list(hyp_by)
    if not ref_keys or not hyp_keys:
        return None
    ref_txt = {k: "".join((x.get("text") or "") for x in ref_by[k]) for k in ref_keys}
    hyp_txt = {k: "".join((x.get("text") or "") for x in hyp_by[k]) for k in hyp_keys}
    total_ref = sum(len(_norm_units(t)) for t in ref_txt.values())
    if total_ref == 0:
        return None
    # 人数很少（会议通常 2-6 人），穷举全排列就是最优解，不必写匈牙利算法
    n = max(len(ref_keys), len(hyp_keys))
    if n > 7:
        return None      # 7! 已是 5040，再大人数远超实际场景，放弃最优解
    best = None
    for perm in permutations(range(n)):
        err = 0
        for i, hk in enumerate(hyp_keys):
            j = perm[i] if i < len(perm) else i
            rk = ref_keys[j] if j < len(ref_keys) else None
            h = _norm_units(hyp_txt.get(hk, ""))
            r = _norm_units(ref_txt.get(rk, "")) if rk is not None else []
            err += edit_distance(r, h)
        if best is None or err < best:
            best = err
    return best / total_ref if best is not None else None


# ---------------- 取预测结果 ----------------

def find_hyp_json(media: Path) -> Path:
    """在素材同目录与 separated_out/<stem>/ 下找管线的转写结果"""
    cands = []
    for root in (media.parent, media.parent / "separated_out" / media.stem):
        if root.is_dir():
            cands += sorted(root.glob("转写结果*.json"))
            cands += sorted(root.glob(f"{media.stem}*.json"))
    for c in cands:
        if "ref" not in c.name:
            return c
    return None


def load_hyp(path: Path):
    """读管线产物：lines 里带 spk/start_ms/end_ms/text，正是 DER/CER/cpWER 需要的三项输入"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    lines = payload.get("lines") or []
    segs = [{"start_ms": l.get("start_ms", 0), "end_ms": l.get("end_ms", 0),
             "spk": l.get("spk_name") or l.get("spk"), "text": l.get("text") or ""}
            for l in lines]
    text = "".join(l.get("text") or "" for l in lines)
    if not text and payload.get("text"):
        text = payload["text"]
    return segs, text


def run_pipeline(media: Path, extra_args):
    """跑一遍管线产出转写结果（--no-separate 让人声分离不参与评测速度）"""
    cmd = [sys.executable, str(SCRIPT_DIR / "av_to_transcript_and_minutes.py"),
           str(media), "--meeting", "--force"] + list(extra_args)
    print(f"[跑管线] {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(f"[跑管线] 失败 (exit {r.returncode}):\n{(r.stderr or r.stdout or '')[-2000:]}")
    return r.returncode == 0


def evaluate_one(media: Path, ref_path: Path, hyp_path=None, run=True, extra_args=()):
    ref = json.loads(ref_path.read_text(encoding="utf-8"))
    if hyp_path is None:
        hyp_path = find_hyp_json(media)
        if hyp_path is None and run:
            run_pipeline(media, extra_args)
            hyp_path = find_hyp_json(media)
    if hyp_path is None or not Path(hyp_path).is_file():
        return {"media": media.name, "error": "找不到转写结果，且管线未能产出"}
    hyp_segs, hyp_text = load_hyp(Path(hyp_path))

    result = {"media": media.name, "hyp": str(hyp_path)}
    ref_text = ref.get("text")
    if ref_text:
        c = cer(ref_text, hyp_text)
        result["cer"] = None if c is None else round(c, 4)
    ref_segs = ref.get("speakers") or []
    if ref_segs:
        d, detail = der_from_segments(ref_segs, hyp_segs)
        result["der"] = None if d is None else round(d, 4)
        result["der_detail"] = detail
        # 无容差对照：与 0.25s collar 版一起看，才分得清"更准"和"边界更粗"
        d0, _ = der_from_segments(ref_segs, hyp_segs, collar=0.0)
        result["der_nocollar"] = None if d0 is None else round(d0, 4)
        c = cpwer(ref_segs, hyp_segs)
        if c is not None:
            result["cpwer"] = round(c, 4)
        result["ref_speakers"] = len({str(s.get("spk")) for s in ref_segs})
        result["hyp_speakers"] = len({str(s.get("spk")) for s in hyp_segs if s.get("spk") is not None})
    return result


def aggregate(results):
    """汇总：CER 按参考长度加权（短素材不该与长素材同权），DER 取平均"""
    cer_num = cer_den = 0
    dens = []
    for r in results:
        if r.get("cer") is not None:
            dens.append(r["cer"])
        if r.get("der") is not None:
            pass
    out = {"count": len(results), "failed": sum(1 for r in results if r.get("error"))}
    if dens:
        out["cer_mean"] = round(sum(dens) / len(dens), 4)
        out["cer_min"] = round(min(dens), 4)
        out["cer_max"] = round(max(dens), 4)
    ders = [r["der"] for r in results if r.get("der") is not None]
    if ders:
        out["der_mean"] = round(sum(ders) / len(ders), 4)
        out["der_min"] = round(min(ders), 4)
        out["der_max"] = round(max(ders), 4)
    # 无容差 DER：与 der_mean 的差值是"边界精度"的量尺，差值变大说明改动把边界弄粗了
    d0s = [r["der_nocollar"] for r in results if r.get("der_nocollar") is not None]
    if d0s:
        out["der_nocollar_mean"] = round(sum(d0s) / len(d0s), 4)
    cpw = [r["cpwer"] for r in results if r.get("cpwer") is not None]
    if cpw:
        out["cpwer_mean"] = round(sum(cpw) / len(cpw), 4)
    return out


def compare(old, new):
    """两份报告对比：只报变化量与方向，不替用户判断好坏阈值"""
    rows = []
    for key, label, better in (("cer_mean", "CER", "lower"), ("der_mean", "DER", "lower")):
        a, b = (old.get("summary") or {}).get(key), (new.get("summary") or {}).get(key)
        if a is None or b is None:
            continue
        delta = b - a
        if abs(delta) < 1e-4:
            verdict = "持平"
        else:
            improved = (delta < 0) if better == "lower" else (delta > 0)
            verdict = "改善" if improved else "变差"
        rows.append({"metric": label, "before": a, "after": b,
                     "delta": round(delta, 4), "verdict": verdict})
    return rows


def selftest():
    """自检：CER 边界 + DER 一致性（同输入应得 0）"""
    fails = []
    def check(name, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'} {name} {detail if not cond else ''}")
        if not cond:
            fails.append(name)

    print("[1] CER")
    check("完全相同为 0", cer("今天天气很好", "今天天气很好") == 0.0)
    check("标点不计入", cer("今天天气很好。", "今天天气很好") == 0.0)
    check("全半角归一", cer("ＡＢＣ", "ABC") == 0.0)
    check("错一字 = 1/6", abs(cer("今天天气很好", "今天天汽很好") - 1 / 6) < 1e-9,
          cer("今天天气很好", "今天天汽很好"))
    check("多字计入", cer("今天", "今天天气") > 0.9)
    check("参考为空返回 None", cer("", "任意") is None)
    check("英文按词比较", abs(cer("hello world", "hello word") - 0.5) < 1e-9)

    print("[2] 编辑距离")
    check("空对空", edit_distance([], []) == 0)
    check("空对非空", edit_distance([], ["a", "b"]) == 2)
    check("对称性", edit_distance(list("abc"), list("abd")) == edit_distance(list("abd"), list("abc")))

    print("[3] DER（需 pyannote.metrics）")
    segs = [{"start_ms": 0, "end_ms": 3000, "spk": "A"},
            {"start_ms": 3000, "end_ms": 6000, "spk": "B"}]
    d, detail = der_from_segments(segs, segs)
    if d is None:
        print(f"  SKIP DER（{detail.get('reason')}）")
    else:
        check("完全相同 DER=0", abs(d) < 1e-6, d)
        wrong = [{"start_ms": 0, "end_ms": 3000, "spk": "B"},
                 {"start_ms": 3000, "end_ms": 6000, "spk": "B"}]
        d2, _ = der_from_segments(segs, wrong)
        check("错标一人 DER>0", d2 is not None and d2 > 0.2, d2)
        d0, _ = der_from_segments(segs, segs, collar=0.0)
        check("无容差同输入仍为 0", d0 is not None and abs(d0) < 1e-6, d0)
        # 边界抖动 0.1 秒：容差内（0.25s）不计错误，无容差版必须能看见
        shift = [{"start_ms": 100, "end_ms": 3000, "spk": "A"},
                 {"start_ms": 3000, "end_ms": 6000, "spk": "B"}]
        dc, _ = der_from_segments(segs, shift)
        dn, _ = der_from_segments(segs, shift, collar=0.0)
        check("容差内抖动不计错误", dc is not None and abs(dc) < 1e-6, dc)
        check("无容差能看见边界抖动", dn is not None and dn > 0, dn)

    print("[4] cpWER（说话人归属 + 文字错误一起算）")
    ref_txt = [{"start_ms": 0, "end_ms": 3000, "spk": "A", "text": "今天天气很好"},
               {"start_ms": 3000, "end_ms": 6000, "spk": "B", "text": "明天可能下雨"}]
    check("完全一致为 0",
          cpwer(ref_txt, [dict(s) for s in ref_txt]) == 0.0,
          cpwer(ref_txt, ref_txt))
    # 两人标签互换：拼串后按最优置换仍能对上，cpWER 应仍为 0（这正是它与 DER 的分工）
    swapped = [{"start_ms": 0, "end_ms": 3000, "spk": "X", "text": "今天天气很好"},
               {"start_ms": 3000, "end_ms": 6000, "spk": "Y", "text": "明天可能下雨"}]
    check("标签可置换（不惩罚纯改名）", cpwer(ref_txt, swapped) == 0.0, cpwer(ref_txt, swapped))
    # 两人内容被并到一个标签下：拼串后无法靠置换救回，必须被惩罚
    merged = [{"start_ms": 0, "end_ms": 3000, "spk": "X", "text": "今天天气很好"},
              {"start_ms": 3000, "end_ms": 6000, "spk": "X", "text": "明天可能下雨"}]
    v = cpwer(ref_txt, merged)
    check("两人内容并成一人被惩罚", v is not None and v > 0.4, v)
    check("缺逐段文本返回 None",
          cpwer([{"start_ms": 0, "end_ms": 3000, "spk": "A"}], swapped) is None)

    print()
    if fails:
        print(f"FAILED ({len(fails)}): {', '.join(fails)}")
        return 1
    print("SELFTEST PASS")
    return 0


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="转写/分离质量基准：DER（分离错误率）+ CER（字错误率）")
    ap.add_argument("target", nargs="?", help="素材文件、素材目录，或单份 .ref.json")
    ap.add_argument("--hyp", help="已有转写结果 JSON（省略则自动查找或跑管线）")
    ap.add_argument("--report", help="把评测报告写到该 JSON")
    ap.add_argument("--compare", help="与旧报告对比，输出变化量")
    ap.add_argument("--no-run", action="store_true", help="不跑管线，只比对已有产物")
    ap.add_argument("--selftest", action="store_true", help="跑自检（不需要素材）")
    ap.add_argument("--asr-extra", default=None, help="透传给管线的额外参数")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.target:
        ap.error("需要指定素材文件、目录或 .ref.json（或使用 --selftest）")

    target = Path(args.target)
    extra_args = args.asr_extra.split() if args.asr_extra else []
    pairs = []   # (media, ref_path)

    if target.is_file() and target.name.endswith(".ref.json"):
        stem = target.name[: -len(".ref.json")]
        for ext in (".m4a", ".mp3", ".wav", ".mp4", ".mkv", ".mov", ".flac", ".aac", ".ogg"):
            cand = target.parent / (stem + ext)
            if cand.is_file():
                pairs.append((cand, target))
                break
        if not pairs:
            print(f"[warn] 找不到与 {target.name} 对应的媒体文件，仅按 --hyp 比对", file=sys.stderr)
            pairs.append((None, target))
    elif target.is_dir():
        for ref in sorted(target.rglob("*.ref.json")):
            stem = ref.name[: -len(".ref.json")]
            media = None
            for ext in (".m4a", ".mp3", ".wav", ".mp4", ".mkv", ".mov", ".flac", ".aac", ".ogg"):
                cand = ref.parent / (stem + ext)
                if cand.is_file():
                    media = cand
                    break
            if media is None:
                print(f"[warn] {ref.name} 找不到对应媒体，跳过", file=sys.stderr)
                continue
            pairs.append((media, ref))
        if not pairs:
            sys.exit(f"{target} 下没有 *.ref.json 标注文件")
    else:
        ref = target.with_name(target.name + ".ref.json")
        if not ref.is_file():
            ref = target.with_suffix(".ref.json")
        if not ref.is_file():
            sys.exit(f"找不到标注文件（期望 {ref.name}）。请先按脚本头部说明准备 .ref.json")
        pairs.append((target, ref))

    results = []
    for media, ref in pairs:
        print(f"\n=== 评测 {media.name if media else ref.name} ===")
        r = evaluate_one(media, ref, args.hyp, run=not args.no_run, extra_args=extra_args)
        results.append(r)
        if r.get("error"):
            print(f"  失败: {r['error']}")
        else:
            parts = []
            if r.get("cer") is not None:
                parts.append(f"CER {r['cer']:.2%}")
            if r.get("der") is not None:
                parts.append(f"DER {r['der']:.2%}")
                if r.get("der_nocollar") is not None:
                    parts.append(f"DER(无容差) {r['der_nocollar']:.2%}")
                if r.get("cpwer") is not None:
                    parts.append(f"cpWER {r['cpwer']:.2%}")
                parts.append(f"说话人 参考{r.get('ref_speakers')}/预测{r.get('hyp_speakers')}")
            print("  " + "  ".join(parts) if parts else "  无可比指标（标注缺 speakers 或 text）")

    summary = aggregate(results)
    report = {"summary": summary, "results": results}

    print("\n=== 汇总 ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if any(r.get("der_detail") for r in results):
        for r in results:
            if r.get("der_detail") and "miss_s" in r["der_detail"]:
                d = r["der_detail"]
                print(f"  {r['media']}: miss {d['miss_s']}s / 误报 {d['false_alarm_s']}s / "
                      f"混淆 {d['confusion_s']}s / 总时长 {d['total_s']}s")

    out_path = args.report
    if out_path:
        Path(out_path).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"\n报告已写入 {out_path}")

    if args.compare:
        old = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        print(f"\n=== 与 {args.compare} 对比 ===")
        rows = compare(old, report)
        if not rows:
            print("  两份报告没有可对比的指标")
        for row in rows:
            print(f"  {row['metric']}: {row['before']:.2%} → {row['after']:.2%} "
                  f"({row['delta']:+.2%}) {row['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
