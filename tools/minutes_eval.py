#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""minutes_eval.py — 纪要质量评测基准（可重跑门禁，零依赖、零网络）。

为什么单独做一层：`validate_minutes.py` 判的是「结构是否合规」（有没有缺节、
待办表列名对不对、禁用标注在不在），它不回答「这份纪要质量如何」。
两者是不同的闸门——结构全对但满篇编造的稿子照样能过结构校验。

本层只做能机械判定的指标，每条指标都能从原文与纪要的文本对照中算出，
不依赖大模型打分（不可复现）也不依赖人工标注（跑不了 CI）：

  忠实度类
    fact_coverage       纪要里的数字有多少能在原文找到          越高越可信
    invented_numbers    原文里找不到的数字个数                  应为 0
    owner_groundedness  待办责任人能在原文找到的比例            应为 1.0

  覆盖度类
    decision_density    原文每千字对应几条决策                  过低=漏判定
    action_density      原文每千字对应几条待办                  过低=漏提取
    section_presence    必填节齐全度                            应为 1.0

  结构类
    owner_filled        待办表责任人非「待定」的比例             仅供参考，不设门槛
    due_filled          待办表完成时间非「待定」的比例           仅供参考，不设门槛

刻意不做的：语义忠实度（「这句话有没有曲解原意」）、语用错误分类
（过度自信转述 / 无依据量化 / 说话人张冠李戴，见 MeetingProbe 的失败 taxonomy）。
这三类必须读语义，机械判定会把「同义改写」误判成编造，把正确转述误判成幻觉。
留作人工抽检项，写在报告的 unjudged 字段里，不参与门禁。

跑法：
  python tools/minutes_eval.py <纪要.md|docx> --source <会议原文.md>
  python tools/minutes_eval.py <纪要> --source <原文> --json 报告.json
  python tools/minutes_eval.py --selftest
  python tools/minutes_eval.py <纪要> --source <原文> --gate   # 低于门槛则退出码 1
"""
import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent


def _load_validator():
    """复用 validate_minutes 的解析层，避免第二套解析器漂移。"""
    from minutes_quality import _load_validator as _lq
    return _lq()


# 门禁门槛。数字来自 2026-11-18 首轮对真实素材的实测基线，改动前先看是否有回归数据支撑。
GATES = {
    "invented_numbers_max": 0,       # 编造数字：零容忍，原文没有就是没有
    "owner_groundedness_min": 0.8,   # 责任人溯源：允许少量转写变体差异
    "fact_coverage_min": 0.5,        # 数字覆盖：纪要里的数字至少一半有出处
    "section_presence_min": 1.0,     # 必填节：必须齐全
}

REQUIRED_SECTIONS = ("会议结论", "关键决策", "待办事项", "会议内容")
NUM_RE = re.compile(r"\d+(?:[.,]\d+)*\s*[%％]?")

# 中文数字：转写稿里价格与数量常用「十三四块」「十五六块」这类写法，
# 纪要整理时会被写成阿拉伯数字「13 元至 16 元」。只比对阿拉伯数字会把这种
# 正常转写形态误判成编造（2026-11-18 实测：一份真实纪要因「16」被误报）。
CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
            "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_NUM_SEQ = re.compile(r"[零一二三四五六七八九十百千万亿两]{1,6}")


def _cn_to_values(seq: str) -> set:
    """把一段中文数字串展开成可能的阿拉伯数字集合（保守展开，宁宽不漏）。

    只处理会议记录里真正会出现的两类：
      - 确定值：「二十」→20、「十六」→16、「三百」→300
      - 模糊相邻：「十五六」→15/16、「十三四」→13/14、「三五十」→30/40/50/35/50
    宁宽不漏的理由：本指标要抓的是「原文根本没提过这个数」，「十五六块」写成
    「15 元至 16 元」是忠实整理而非编造，误判会让人不信这个门禁。
    """
    out = set()
    if not seq:
        return out
    # 十位以上：X十Y / X十 / 十Y / 十
    m = re.fullmatch(r"([一二三四五六七八九两])?十([一二三四五六七八九两])?", seq)
    if m:
        tens = CN_DIGIT[m.group(1)] if m.group(1) else 1
        if m.group(2):
            out.add(tens * 10 + CN_DIGIT[m.group(2)])
        else:
            out.add(tens * 10)
        return out
    # 带「百/千/万」的整值
    m = re.fullmatch(r"([一二三四五六七八九两])?([百千万亿])", seq)
    if m:
        base = CN_DIGIT[m.group(1)] if m.group(1) else 1
        out.add(base * {"百": 100, "千": 1000, "万": 10000, "亿": 100000000}[m.group(2)])
        return out
    # 单个汉字数字
    if len(seq) == 1 and seq in CN_DIGIT:
        out.add(CN_DIGIT[seq])
        return out
    # 模糊串：逐位取数字，并补「十位组合」——「十五六」→ 15,16；「三五十」→ 30,35,40,50
    vals = [CN_DIGIT[c] for c in seq if c in CN_DIGIT]
    if vals:
        out.update(vals)
        # 若串里出现「十」，把其前后数字按十位组合展开
        if "十" in seq:
            for i, c in enumerate(seq):
                if c != "十":
                    continue
                prev = CN_DIGIT.get(seq[i - 1]) if i > 0 else 1
                prev = 1 if prev is None else prev
                out.add(prev * 10)
                if i + 1 < len(seq):
                    nxt = CN_DIGIT.get(seq[i + 1])
                    if nxt is not None:
                        out.add(prev * 10 + nxt)
                        # 相邻位再补一个（「十五六」的 16）
                        if i + 2 < len(seq):
                            n2 = CN_DIGIT.get(seq[i + 2])
                            if n2 is not None:
                                out.add(prev * 10 + n2)
                else:
                    out.add(prev * 10)
            # 「三五十」这类（十之前连续两位数字）：十之前的每一位都可能是十位（30/40），
            # 十（读作 50）本身也是一个可能值。只取十之前的数字，不能把十之后的算进来——
            # 那样「十五六」会被展开成 50/55/56，「十五六块」根本不可能指 56 元。
            ten_at = seq.index("十")
            for h in [CN_DIGIT[c] for c in seq[:ten_at] if c in CN_DIGIT]:
                out.add(h * 10)
        # 「三十四」这类未带「十」的连写（罕见）按整体十位组合兜底
        if len(vals) == 2 and "十" not in seq:
            out.add(vals[0] * 10 + vals[1])
    return out


def _cn_numbers(text: str) -> set:
    """原文里出现的所有中文数字可能取值（字符串形式，与阿拉伯数字同口径归一）。"""
    out = set()
    for seq in CN_NUM_SEQ.findall(text or ""):
        for v in _cn_to_values(seq):
            out.add(str(v))
    return out


def _norm_num(s: str) -> str:
    """数字归一：去千分位与空格，统一百分号。用于跨文本比对。"""
    return re.sub(r"[,\s]", "", s).rstrip("%％")


def _strip_timestamps(text: str) -> str:
    """剥掉原文的 [00:12] 时间戳。

    不剥会让「12」进数字集合，纪要里编造的「12 台」正好命中而查不出来——
    这个坑在 validate_minutes.py 的溯源核对里已实测踩过一次。
    """
    return re.sub(r"\[\d{1,3}:\d{2}(?:-\d{1,3}:\d{2})?\]", " ", text)


def _numbers(text: str) -> set:
    return {_norm_num(x) for x in NUM_RE.findall(text)}


def _body_without_list_numbers(data: dict) -> str:
    """拼出用于数字统计的正文，剔掉结构性序号。

    待办表的「序号」列与决策列表的「1. 」前缀都是排版产物，不是事实数据。
    不剔的话，一份完全干净的三条待办纪要会被判出三个「编造数字」（实测踩过）——
    原文里当然没有「1」「2」「3」这种孤立序号。
    """
    sections = data.get("sections") or {}
    chunks = []
    for key, text in sections.items():
        if key == "待办事项":
            # 待办表整节跳过：它的数字全在表格的序号列里，任务本体已在 action_items 中
            # 单独参与责任人溯源；数字层面的核对交由待办正文里的数字各自判断
            continue
        # 去掉行首列表序号（"1. " / "1）"）与表格前的序号列
        cleaned = re.sub(r"(?m)^\s*\d+[.、)]\s+", "", text)
        chunks.append(cleaned)
    # 待办表的任务描述仍要参与数字核对（任务里可能写「3000 万」这类关键数字），
    # 但只取 task 与 note 字段，不取 id
    for it in data.get("action_items") or []:
        chunks.append(str(it.get("task") or ""))
        chunks.append(str(it.get("note") or ""))
    return "\n".join(chunks)


def evaluate(minutes_path: Path, source_text: str) -> dict:
    """算全部指标，返回 {metrics, verdict, unjudged}。"""
    mod = _load_validator()
    data = mod.export_structured(minutes_path)
    sections = data.get("sections") or {}
    items = data.get("action_items") or []
    decisions = data.get("decisions") or []

    src_clean = _strip_timestamps(source_text or "")
    src_nums = _numbers(src_clean)
    src_tokens = set(NUM_RE.findall(src_clean))
    src_cn = _cn_numbers(src_clean)      # 中文数字（「十五六」→ 15/16）
    src_all = src_nums | src_cn

    # 纪要做数字统计时剔掉结构性序号（待办表序号列、决策列表前缀），只看事实内容
    body = _body_without_list_numbers(data)
    out_nums = _numbers(body)
    invented = sorted(n for n in out_nums if n not in src_all and n not in src_tokens)

    fact_coverage = (len(out_nums) - len(invented)) / len(out_nums) if out_nums else 1.0

    owners = [it.get("owner", "") for it in items]
    real_owners = [o for o in owners
                   if o and o not in {"待定", "未指定", "全体", "大家", "—", "-"}]
    grounded = [o for o in real_owners if o in src_clean]
    owner_groundedness = len(grounded) / len(real_owners) if real_owners else 1.0

    src_len_k = max(len(src_clean) / 1000.0, 1e-6)
    present = [k for k in REQUIRED_SECTIONS if (sections.get(k) or "").strip()]
    section_presence = len(present) / len(REQUIRED_SECTIONS)

    metrics = {
        "invented_numbers": len(invented),
        "invented_number_list": invented[:10],
        "fact_coverage": round(fact_coverage, 3),
        "owner_groundedness": round(owner_groundedness, 3),
        "decision_density": round(len(decisions) / src_len_k, 2),
        "action_density": round(len(items) / src_len_k, 2),
        "section_presence": round(section_presence, 3),
        "owner_filled": round(len(real_owners) / len(owners), 3) if owners else 0.0,
        "due_filled": round(
            sum(1 for it in items if (it.get("due") or "") not in ("", "待定"))
            / len(items), 3) if items else 0.0,
        "n_action_items": len(items),
        "n_decisions": len(decisions),
        "src_chars": len(src_clean),
    }

    checks = {
        "invented_numbers": metrics["invented_numbers"] <= GATES["invented_numbers_max"],
        "owner_groundedness": metrics["owner_groundedness"] >= GATES["owner_groundedness_min"],
        "fact_coverage": metrics["fact_coverage"] >= GATES["fact_coverage_min"],
        "section_presence": metrics["section_presence"] >= GATES["section_presence_min"],
    }
    verdict = "pass" if all(checks.values()) else "fail"

    return {
        "schema_version": 1,
        "minutes": str(minutes_path),
        "metrics": metrics,
        "checks": checks,
        "gates": GATES,
        "verdict": verdict,
        # 机械判定不覆盖的维度，明确列出以免被误读成「已评估且通过」
        "unjudged": [
            "语义忠实度：是否曲解原意（需读语义，机械判定会误伤同义改写）",
            "无依据量化：把原文的模糊表述写成精确数字（需读语义）",
            "说话人张冠李戴：归属错人（纪要若为中性叙述则不适用）",
            "语用过信：把推测写成事实（需读语义）",
        ],
    }


def render_report(rep: dict) -> str:
    m = rep["metrics"]
    g = rep["gates"]
    rows = [
        "指标                        值        门槛        结论",
        "-" * 60,
        f"编造数字                    {m['invented_numbers']:<9}"
        f"≤{g['invented_numbers_max']:<10}"
        f"{'PASS' if rep['checks']['invented_numbers'] else 'FAIL'}",
        f"数字溯源率                  {m['fact_coverage']:<9}"
        f"≥{g['fact_coverage_min']:<10}"
        f"{'PASS' if rep['checks']['fact_coverage'] else 'FAIL'}",
        f"责任人溯源率                {m['owner_groundedness']:<9}"
        f"≥{g['owner_groundedness_min']:<10}"
        f"{'PASS' if rep['checks']['owner_groundedness'] else 'FAIL'}",
        f"必填节齐全度                {m['section_presence']:<9}"
        f"≥{g['section_presence_min']:<10}"
        f"{'PASS' if rep['checks']['section_presence'] else 'FAIL'}",
        "-" * 60,
        f"参考项（不设门槛）：决策 {m['n_decisions']} 条（密度 {m['decision_density']}/千字）、"
        f"待办 {m['n_action_items']} 条（密度 {m['action_density']}/千字）",
        f"                责任人填写率 {m['owner_filled']:.0%}、"
        f"完成时间填写率 {m['due_filled']:.0%}",
    ]
    if m["invented_number_list"]:
        rows.append(f"编造数字明细：{'、'.join(m['invented_number_list'])}")
    rows.append("")
    rows.append("未评估（需人工或语义判断）：")
    for u in rep["unjudged"]:
        rows.append(f"  - {u}")
    return "\n".join(rows)


def selftest() -> int:
    """自检：造一份含已知缺陷的纪要，验证每类指标都能被抓到。"""
    fails = []

    def ck(name, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'} {name}" + (f"  {detail}" if not cond else ""))
        if not cond:
            fails.append(name)

    src = ("[00:10] 张三：这次要 3000 万的销售额。\n"
           "[00:20] 李四：毛利率定在 18 个点。\n")
    good = """# 会议纪要：测试

## 会议基本信息

会议主题：测试

## 会议结论

本次确定 3000 万销售额目标。

## 关键决策

1. 确定销售额目标为 3000 万。

## 待办事项

| 序号 | 待办事项 | 责任人/责任部门 | 完成时间 | 优先级 | 备注 |
|---|---|---|---|---|---|
| 1 | 核对毛利率口径 | 李四 | 本周五 | 高 | 无 |

## 会议内容

### 议题

会上确认销售额 3000 万，毛利率 18 个点。
"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "good.md"
        p.write_text(good, encoding="utf-8")
        rep = evaluate(p, src)
        ck("干净稿判 pass", rep["verdict"] == "pass", str(rep["checks"]))
        ck("无编造数字", rep["metrics"]["invented_numbers"] == 0,
           str(rep["metrics"]["invented_number_list"]))
        ck("责任人已溯源", rep["metrics"]["owner_groundedness"] == 1.0)

        # 编造数字：9 亿在原文完全不存在
        bad = good.replace("毛利率 18 个点", "毛利率 18 个点，明年做到 9 亿")
        p2 = Path(td) / "bad.md"
        p2.write_text(bad, encoding="utf-8")
        rep2 = evaluate(p2, src)
        ck("编造数字被抓", rep2["metrics"]["invented_numbers"] >= 1,
           str(rep2["metrics"]["invented_number_list"]))
        ck("编造数字判 fail", rep2["verdict"] == "fail")
        ck("编造数字进明细", "9" in rep2["metrics"]["invented_number_list"],
           str(rep2["metrics"]["invented_number_list"]))

        # 责任人不在原文：王五从没出现
        bad_owner = good.replace("| 李四 |", "| 王五 |")
        p3 = Path(td) / "bad_owner.md"
        p3.write_text(bad_owner, encoding="utf-8")
        rep3 = evaluate(p3, src)
        ck("责任人未溯源被判 0", rep3["metrics"]["owner_groundedness"] == 0.0,
           str(rep3["metrics"]["owner_groundedness"]))
        ck("责任人未溯源判 fail", rep3["verdict"] == "fail")

        # 时间戳不得为编造数字背书：[00:20] 里的 20 不能被当成出处
        ts_src = "[00:20] 张三：讨论一下。\n"
        ts_md = good.replace("毛利率 18 个点", "发了 20 台货")
        p4 = Path(td) / "ts.md"
        p4.write_text(ts_md, encoding="utf-8")
        rep4 = evaluate(p4, ts_src)
        ck("时间戳不给数字背书", rep4["metrics"]["invented_numbers"] >= 1,
           str(rep4["metrics"]["invented_number_list"]))

        # 缺节：删掉关键决策
        missing = good.replace("## 关键决策", "## 其他内容")
        p5 = Path(td) / "missing.md"
        p5.write_text(missing, encoding="utf-8")
        rep5 = evaluate(p5, src)
        ck("缺节被扣分", rep5["metrics"]["section_presence"] < 1.0,
           str(rep5["metrics"]["section_presence"]))

    print("SELFTEST", "PASS" if not fails else f"FAIL（{len(fails)} 项）")
    return 0 if not fails else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="纪要质量评测基准")
    ap.add_argument("file", nargs="?", help="纪要 md/docx")
    ap.add_argument("--source", required=False, help="会议原文（用于数字与责任人溯源）")
    ap.add_argument("--json", dest="json_path", default=None, help="报告落盘路径")
    ap.add_argument("--gate", action="store_true", help="不达标时退出码 1（CI 用）")
    ap.add_argument("--selftest", action="store_true")
    ns = ap.parse_args(argv)

    if ns.selftest:
        return selftest()
    if not ns.file:
        ap.error("需要纪要文件，或 --selftest")
    path = Path(ns.file)
    if not path.is_file():
        print(f"文件不存在：{path}", file=sys.stderr)
        return 2
    if not ns.source:
        ap.error("质量评测需要 --source 会议原文（没有原文就无法判断数字与责任人是否编造）")
    source = Path(ns.source).read_text(encoding="utf-8", errors="replace")

    rep = evaluate(path, source)
    print(render_report(rep))
    if ns.json_path:
        Path(ns.json_path).write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
        print(f"\n报告已写入：{ns.json_path}")
    if ns.gate and rep["verdict"] != "pass":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
