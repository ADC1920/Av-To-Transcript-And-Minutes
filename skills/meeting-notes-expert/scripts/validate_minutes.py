#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""validate_minutes.py — 结构化总结结构校验与源文核对（meeting-notes-expert 配套）

按会议版章节名校验，仅适用会议场景的会议纪要；讲座、访谈、播客等通用总结不强制。
做四件事：
  1) 结构：必需节齐备（会议结论/关键决策/待办事项/会议内容），按 minutes-schema.json 校验；
  2) 版式：待办表六列、优先级枚举合法、基本信息区不得是表格；
  3) 文风：正文不得出现「待确认/存疑/转写亦作」等禁用标注，决策不得写成「讨论了…」；
  4) 溯源（给了 --source 才做）：成稿中的数字必须出现在源材料；待办责任人必须出现在源材料。

用法：
    python validate_minutes.py 纪要.md [--source 会议原文.txt] [--strict] [--json 报告.json]
    python validate_minutes.py 纪要.docx [--source ...]
    python validate_minutes.py --selftest

退出码：0 通过；1 有 FAIL（--strict 时 WARN 也计 FAIL）；2 用法/读取错误。
"""
import argparse
import json
import re
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = TOOL_DIR.parent / "references" / "minutes-schema.json"

REQUIRED = ["会议结论", "关键决策", "待办事项", "会议内容"]
OPTIONAL = ["待定与开放问题", "上期行动项回顾"]
ALL_SECTIONS = REQUIRED + OPTIONAL
# 通用总结（访谈/播客/讲座等非会议材料）的章节名，见 SKILL.md「多类型模板分支」。
# 两类共用同一套校验逻辑，只是必填节与字段名不同——非会议材料本来就不该有
# 「关键决策」「待办事项」，用会议口径硬判会把合格的总结判成失败（实测）。
GENERIC_REQUIRED = ["核心结论", "关键要点", "内容详情"]
GENERIC_OPTIONAL = ["受访者原话", "解读与推断", "章节时间戳", "金句", "提及资源",
                    "术语定义", "案例与例证", "引用与参考",
                    "行动事项", "待定与开放问题", "上期事项回顾"]
GENERIC_SECTIONS = GENERIC_REQUIRED + GENERIC_OPTIONAL
GENERIC_BASIC_OK = {"内容主题", "来源", "时间", "主讲人或参与人", "记录人",
                    "内容类型", "时长"}
# 会议三个子分支的专属章节（见 SKILL.md「多类型模板分支」）。
# 正式会议在常规七节外多一个法定要素节；复盘与站会是各自独立的骨架，
# 不用「关键决策/待办事项」——用常规口径去判它们，合格的稿子会被判缺节。
FORMAL_EXTRA = ["法定要素"]
RETRO_REQUIRED = ["本次目标与结果", "做得好的", "待改进", "下轮改进项"]
STANDUP_REQUIRED = ["进度", "计划", "阻塞"]
MEETING_SUB_SECTIONS = FORMAL_EXTRA + RETRO_REQUIRED + STANDUP_REQUIRED
# 完整分层版（SKILL.md「输出变体·完整分层版」）：把七节按议题展开成两级，
# 章节名与默认七节不同但语义同源。判据取三个独有节名，命中即按这一层校验。
LAYERED_MARKERS = ("会议决策摘要", "关键议题与讨论", "会议结论与后续安排")
LAYERED_REQUIRED = ["会议决策摘要", "关键议题与讨论", "行动项", "会议结论与后续安排"]
# 分层版的可选节：材料没给就没有，但它们的名字必须被认成节——
# 不认的话这些节里的表格会落进「基本信息区」，被误判成「基本信息用了表格」（实测）。
LAYERED_OPTIONAL = ["项目背景", "项目背景与建设内容", "风险评估", "待决问题与风险",
                    "意见与分歧"]
FORBIDDEN = ["待确认", "存疑", "转写亦作", "不确定标注"]
ACTION_COLUMNS = ["序号", "待办事项", "责任人/责任部门", "完成时间", "优先级", "备注"]
PRIORITIES = {"高", "中", "低", "待定"}
BASIC_FIELDS_OK = {"会议主题", "会议时间", "会议地点", "参会人员", "记录人",
                   "主持人", "会议方式", "会议编号", "会议形式",
                   # 完整分层版常用字段（对照听脑AI 模板）：参会方按单位列、
                   # 会议性质标评审/初审/例会等，比「参会人员」更贴合多方会议。
                   "参会方", "会议性质", "参会单位", "缺席人员", "会议主持"}
HEADING_RE = re.compile(r"^##(?!#)\s+(.+)$")
HEADING_STYLE_RE = re.compile(r"^(?:Heading|标题)\s*(\d+)$")
LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.、)])\s+")
NUM_RE = re.compile(r"\d+(?:[.,]\d+)*\s*[%％]?")
OWNER_PLACEHOLDERS = {"待定", "未指定", "全体", "大家", "—", "-"}


# ---------- 解析 ----------
def parse_md(text):
    blocks, table = [], None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.lstrip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells and all(set(c) <= set("-: ") for c in cells):
                continue
            if table is None:
                table = []
                blocks.append(("t", table))
            table.append(cells)
            continue
        table = None
        blocks.append(("p", line))
    return blocks


def parse_docx(path):
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(str(path))
    blocks = []
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            para = Paragraph(child, doc)
            text = para.text
            # article-format 出稿后标题是样式不是字面 `##`，这里按样式还原成
            # Markdown 标题，让 split_sections 的同一套规则同时吃 md 与 docx。
            style = (para.style.name or "").strip() if para.style is not None else ""
            m = HEADING_STYLE_RE.match(style)
            if m:
                text = "#" * (int(m.group(1)) + 1) + " " + text
            elif style == "Title":
                text = "# " + text
            blocks.append(("p", text))
        elif child.tag == qn("w:tbl"):
            rows = [[c.text.strip() for c in r.cells] for r in Table(child, doc).rows]
            blocks.append(("t", rows))
    return blocks


def split_sections(blocks):
    basic, sections, basic_table = {}, {}, False
    cur = None
    known = (set(ALL_SECTIONS) | set(GENERIC_SECTIONS) | set(MEETING_SUB_SECTIONS)
             | set(LAYERED_REQUIRED) | set(LAYERED_OPTIONAL))
    for kind, payload in blocks:
        if kind == "p":
            text = payload.strip()
            m = HEADING_RE.match(text)
            if m:
                name = m.group(1).strip()
                # 通用总结的章节名也要认成节，否则「## 核心结论」会被当成基本信息区的
                # 后续文本吞掉，类型判定永远命不中（实测：访谈总结被判成缺四个会议节）
                cur = name if name in known else None
                if cur:
                    sections.setdefault(cur, [])
                continue
        if cur is None:
            if kind == "t":
                basic_table = True
            elif not payload.lstrip().startswith("#"):
                m = re.match(r"^([^：:]{2,12})[：:]\s*(.+)$", payload.strip())
                if m:
                    basic[m.group(1).strip()] = m.group(2).strip()
        else:
            sections[cur].append((kind, payload))
    return basic, sections, basic_table


def section_text(entries):
    out = []
    for kind, payload in entries:
        if kind == "p":
            out.append(payload)
        else:
            out.extend(" | ".join(r) for r in payload)
    return "\n".join(out).strip()


def collect_action_items(entries):
    """取待办节的第一张表。列数不对也返回，让列名断言报出来，而不是静默当作没有表。"""
    keys = ["id", "task", "owner", "due", "priority", "note"]
    for kind, payload in entries:
        if kind != "t" or not payload:
            continue
        header = [c.strip() for c in payload[0]]
        items = []
        for row in payload[1:]:
            cells = [c.strip() for c in row]
            items.append({k: (cells[i] if i < len(cells) else "") for i, k in enumerate(keys)})
        return items, header
    return [], []


def collect_decisions(entries):
    lines = []
    for kind, payload in entries:
        if kind != "p":
            continue
        text = payload.strip()
        if text:
            lines.append(LIST_RE.sub("", text).strip())
    return [x for x in lines if x]


# ---------- 校验 ----------
def validate(path, source_text=None, strict=False):
    results = []

    def ok(name, detail=""):
        results.append(("PASS", name, detail))

    def bad(name, detail=""):
        results.append(("FAIL", name, detail))

    def warn(name, detail=""):
        results.append(("WARN", name, detail))

    try:
        text = path.read_text(encoding="utf-8")
        blocks = parse_md(text)
    except (UnicodeDecodeError, OSError):
        try:
            blocks = parse_docx(path)
        except Exception as e:
            print(f"读取失败：{e}")
            # 回一条 FAIL 而不是空列表：空列表会让 summarize 判成 complete，
            # 于是读取失败被打印成「状态：complete」（实测）。退出码仍是 2。
            return 2, [("FAIL", "读取文件", f"无法解析 md/docx：{type(e).__name__}")]

    basic, sections, basic_table = split_sections(blocks)
    # 类型判定：先看是不是复盘/站会（它们有独有的必填节），再看是不是通用总结，
    # 都不命中才按常规会议（正式会议只是常规七节 + 法定要素节，必填节与常规相同）。
    is_retro = all(k in sections for k in RETRO_REQUIRED[:3])
    is_standup = ("进度" in sections and "计划" in sections and "阻塞" in sections
                  and not is_retro)
    is_layered = sum(1 for k in LAYERED_MARKERS if k in sections) >= 2
    is_generic = (not is_retro and not is_standup and not is_layered and
                  any(k in sections for k in
                      ("核心结论", "关键要点", "内容详情", "受访者原话",
                       "章节时间戳", "术语定义")))
    is_formal = (not (is_retro or is_standup or is_generic or is_layered)
                 and any(k in sections for k in FORMAL_EXTRA))
    if basic_table:
        bad("基本信息表形态", "基本信息区使用了表格，规范要求逐行「字段名：内容」")
    if not basic:
        warn("基本信息", "未解析到字段行（材料可能未提供基本信息）")
    else:
        ok_fields = GENERIC_BASIC_OK if is_generic else BASIC_FIELDS_OK
        unknown = [k for k in basic if k not in ok_fields]
        (warn if unknown else ok)("基本信息字段", "非常规字段：" + "、".join(unknown) if unknown else "")

    if is_retro:
        need, kind = RETRO_REQUIRED, "复盘会"
    elif is_standup:
        need, kind = STANDUP_REQUIRED, "站会"
    elif is_layered:
        need, kind = LAYERED_REQUIRED, "会议纪要·完整分层版"
    elif is_generic:
        need, kind = GENERIC_REQUIRED, "通用总结"
    else:
        need, kind = REQUIRED, ("正式会议" if is_formal else "会议纪要")
    missing = [s for s in need if not section_text(sections.get(s, []))]
    if missing:
        bad("必需节齐备", f"[{kind}] 缺：" + "、".join(missing))
    else:
        ok("必需节齐备", f"[{kind}] " + "/".join(need) + " 均在")

    # 禁用标注必须扫全篇：只扫四个必填节正文时，把「会议时间：待确认」写进基本信息区、
    # 或另起一个 ## 附录 放不确定表述，都能绕过（实测）
    body = "\n".join(section_text(v) for v in sections.values())
    full_text = body + "\n" + "\n".join(f"{k}：{v}" for k, v in basic.items())
    hits = [w for w in FORBIDDEN if w in full_text]
    if hits:
        bad("禁用标注", "全文出现：" + "、".join(hits))
    else:
        ok("禁用标注", "无待确认/存疑类标注")

    decisions = collect_decisions(sections.get("关键决策", []))
    if is_layered:
        # 完整分层版把决策写进「会议决策摘要」的 1.2 节，不在独立章节里。
        # 只验这一节非空且不出现讨论句式——分节层级由模板负责，校验器不解析 1.1/1.2 编号。
        summary_entries = collect_decisions(sections.get("会议决策摘要", []))
        bad_lines = [d for d in summary_entries if re.match(r"^(讨论|研究|考虑)了", d)]
        if not summary_entries:
            bad("决策摘要", "「会议决策摘要」为空，分层版必须写明本次核心结论")
        elif bad_lines:
            bad("决策摘要句式", "以下条目是讨论句式：" + "；".join(bad_lines[:3]))
        else:
            ok("决策摘要", f"{len(summary_entries)} 条记录，无讨论句式")
    elif is_retro or is_standup:
        # 复盘与站会骨架里没有「关键决策」节：复盘通常不表决，站会本来就不做决策。
        # 这里不判缺节，但仍扫一遍待改进/进度条目里的讨论句式，防止把过程写成结论。
        scan_key = "待改进" if is_retro else "进度"
        entries = collect_decisions(sections.get(scan_key, []))
        bad_lines = [d for d in entries if re.match(r"^(讨论|研究|考虑)了", d)]
        if bad_lines:
            bad("句式", f"[{scan_key}] 以下条目是讨论句式：" + "；".join(bad_lines[:3]))
        else:
            ok("句式", f"[{scan_key}] {len(entries)} 条无讨论句式")
    elif is_generic:
        # 通用总结没有「关键决策」节，决策句式无从检查；但「以讨论句式冒充结论」
        # 的毛病在「关键要点」里同样会出现，所以仍扫一遍要点条目。
        points = collect_decisions(sections.get("关键要点", []))
        bad_lines = [d for d in points if re.match(r"^(讨论|研究|考虑)了", d)]
        if bad_lines:
            bad("要点句式", "以下条目是讨论句式而非结论：" + "；".join(bad_lines[:3]))
        else:
            ok("要点句式", f"{len(points)} 条要点无讨论句式")
    elif not decisions:
        bad("关键决策", "节内无条目（未形成决策时应写「本次会议未形成决策」）")
    elif re.search(r"(未形成|没有形成|无)(任何)?(关键)?决策", "".join(decisions)):
        ok("关键决策", "声明未形成决策")
    else:
        bad_lines = [d for d in decisions if re.match(r"^(讨论|研究|考虑)了", d)]
        if bad_lines:
            bad("决策句式", "以下条目是讨论句式而非结论：" + "；".join(bad_lines[:3]))
        else:
            ok("决策句式", f"{len(decisions)} 条均为肯定表述")

    if is_formal:
        # 正式会议多一个法定要素节：只要求它存在且不是空壳，不逐项验票数格式
        # （各组织章程字段写法差异大，硬套格式会误判）。
        body_f = section_text(sections.get("法定要素", []))
        if "材料未记录" in body_f or len(body_f.strip()) >= 20:
            ok("法定要素", "本节已填写")
        else:
            bad("法定要素", "本节内容过短，法定要素需含法定人数/表决结果等实际记录")

    if is_layered:
        # 分层版的行动项表列名与默认待办表同源，但不要求必须有行——方案评审类常常
        # 只有「各方补材料」这一两件事，空表加说明也合法。
        items, header = collect_action_items(sections.get("行动项", []))
        if header and header != ACTION_COLUMNS:
            bad("行动项列名", "应为 " + "/".join(ACTION_COLUMNS) + "，实为 " + "/".join(header))
        elif header:
            ok("行动项列名", f"六列且顺序正确（{len(items)} 行）")
        else:
            warn("行动项", "本节未解析到表格（分层版建议用六列表呈现行动项）")
    elif is_retro:
        # 复盘的核心是「待改进」能对上下轮改进项；两者其一为空说明骨架没走完。
        items, header = collect_action_items(sections.get("下轮改进项", []))
        if not items:
            warn("下轮改进项", "本节无表格行（无改进项时应写明「本次未形成改进项」）")
        elif header != ACTION_COLUMNS:
            bad("改进表列名", "应为 " + "/".join(ACTION_COLUMNS) + "，实为 " + "/".join(header))
        else:
            ok("改进表列名", f"六列且顺序正确（{len(items)} 行）")
    elif is_standup:
        # 站会不要求表格，但阻塞节必须存在（「没有人被卡住」本身是有效信息）
        items, header = [], None
        ok("站会阻塞", "阻塞节已写")
    elif is_generic:
        # 通用总结的行动事项用同一张六列表；没有行动时允许写明「未形成行动事项」。
        # 不在这里硬判必须有行动——访谈与讲座常常没有行动项。
        items, header = collect_action_items(sections.get("行动事项", []))
        if header and header != ACTION_COLUMNS:
            bad("行动表列名", "应为 " + "/".join(ACTION_COLUMNS) + "，实为 " + "/".join(header))
        elif header:
            ok("行动表列名", "六列且顺序正确")
        else:
            ok("行动表", "本节无表格（通用总结允许无行动事项）")
    else:
        items, header = collect_action_items(sections.get("待办事项", []))
        if header:
            if header == ACTION_COLUMNS:
                ok("待办表列名", "六列且顺序正确")
            else:
                bad("待办表列名", "应为 " + "/".join(ACTION_COLUMNS) + "，实为 " + "/".join(header))
        elif "未形成行动事项" in section_text(sections.get("待办事项", [])):
            ok("待办表", "声明未形成行动事项")
        else:
            bad("待办表", "待办节既无六列表格，也无「未形成行动事项」声明")

    if items:
        for i, it in enumerate(items, 1):
            if not it["task"]:
                bad(f"待办第{i}行", "事项为空")
            if not it["owner"]:
                bad(f"待办第{i}行", "责任人列缺失（无责任人应填「待定」）")
            if not it["due"]:
                bad(f"待办第{i}行", "完成时间列缺失（未定时应填「待定」）")
            if it["priority"] not in PRIORITIES:
                bad(f"待办第{i}行", f"优先级「{it['priority']}」不在 高/中/低/待定 内")
        if not any(r[0] == "FAIL" and r[1].startswith("待办第") for r in results):
            ok("待办字段", f"{len(items)} 行责任人与时间均非空")

    if not missing and not is_generic and not (is_retro or is_standup or is_layered):
        # schema 只描述常规会议结构：通用总结、复盘、站会的章节名不同，套会议 schema
        # 必然缺节，所以按类型跳过（不是放松校验——它们仍走上面的必填节与禁用标注检查）。
        # 正式会议也走 schema：它就是常规七节 + 法定要素节，七节齐了 schema 自然通过。
        try:
            import jsonschema
            schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
            payload = {
                "basic_info": basic,
                "sections": {k: section_text(v) for k, v in sections.items()},
                "action_items": items if (items and header == ACTION_COLUMNS) else [],
                "decisions": decisions,
            }
            jsonschema.validate(payload, schema)
            ok("结构 schema", "符合 minutes-schema.json")
        except ImportError:
            warn("结构 schema", "jsonschema 未安装，跳过")
        except FileNotFoundError:
            warn("结构 schema", "minutes-schema.json 缺失，跳过")
        except Exception as e:
            warn("结构 schema", str(e).splitlines()[0][:120])

    # ---- 溯源核对（可选）----
    if source_text:
        src = source_text
        # 源文里的 [00:12] 时间戳要先剥掉：不剥的话「12」会进源数字集合，
        # 纪要里编造的「12 台」正好命中、查不出来（实测）
        src_clean = re.sub(r"\[\d{1,3}:\d{2}(?:-\d{1,3}:\d{2})?\]", " ", src)
        src_nums = {re.sub(r"[,\s]", "", x).rstrip("%％") for x in NUM_RE.findall(src_clean)}
        # 受检稿同样剥时间戳：会议纪要本不该有，但通用总结（播客章节、讲座术语）
        # 按规范要保留时刻，不剥就会把 00/01/20 这类时刻数字当成编造（实测告警）。
        body_for_nums = re.sub(r"[(（]?\d{1,3}:\d{2}(?:-\d{1,3}:\d{2})?[)）]?", " ", body)
        # 行首的列表序号也不是事实数据：决策列表的「1.」「2.」会被当成编造数字（实测）。
        # 只剥行首带标点的序号，不剥行内数字——「共 3 项」这种是正文事实，要保留。
        body_for_nums = re.sub(r"(?m)^\s*\d+[.、)]\s+", "", body_for_nums)
        # 表格展平后序号列会挤进数字流（「序号 | 待办事项 | ... | 1 | ...」），
        # 而序号是排版产物不是事实。剔掉每行表格第一个单元格——它与表头首列
        # （序号）对齐，仅在表头首列确为「序号」时执行，避免误伤别的表。
        if "序号" in body_for_nums:
            lines = []
            in_table = False
            for ln in body_for_nums.splitlines():
                cells = [c.strip() for c in ln.split("|")]
                if len(cells) >= 3 and ln.count("|") >= 2:
                    in_table = True
                    if cells and re.fullmatch(r"\d+", cells[0]):
                        cells = [""] + cells[1:]
                    lines.append(" ".join(c for c in cells if c))
                else:
                    if in_table:
                        in_table = False
                    lines.append(ln)
            body_for_nums = "\n".join(lines)
        out_nums = {re.sub(r"[,\s]", "", x).rstrip("%％") for x in NUM_RE.findall(body_for_nums)}
        # 一位数不再整体豁免：源文数字很少时它恰恰是编造的重灾区。改为要求该数字
        # 在源文里以独立 token 出现，避免「1」被「12」包含而误判为已出现。
        src_tokens = set(NUM_RE.findall(src_clean))
        invented = sorted(n for n in out_nums
                          if n not in src_nums and n not in src_tokens)
        if invented:
            warn("数字溯源", "源材料未出现：" + "、".join(invented[:8]))
        else:
            ok("数字溯源", f"{len(out_nums)} 个数字均可在源材料中找到")

        owners = [it["owner"] for it in items
                  if it["owner"] and it["owner"] not in OWNER_PLACEHOLDERS]
        unknown_owners = [o for o in owners if o not in src]
        if unknown_owners:
            warn("责任人溯源", "源材料未出现：" + "、".join(sorted(set(unknown_owners))))
        elif owners:
            ok("责任人溯源", f"{len(set(owners))} 位责任人均在源材料中出现")
        else:
            ok("责任人溯源", "无可核对的责任人（均为待定/未指定）")

    fails = sum(1 for r in results if r[0] == "FAIL")
    warns = sum(1 for r in results if r[0] == "WARN")
    if strict:
        fails += warns
    return (1 if fails else 0), results


def summarize(results):
    """三态判定：complete 全过 / degraded 无 FAIL 但有 WARN / failed 有 FAIL。

    借 silverstein/minutes 的 status 枚举思路——把「结构可用但存在非致命问题」
    从二元的 PASS/FAIL 里分出来，避免有 WARN 的稿子被当成完全健康或彻底报废。
    results 为空表示根本没读到内容，返回 error 而不是 complete——否则读取失败
    （rc=2）会打印「状态：complete」，把「没校验」说成「校验通过」（实测）。
    """
    if not results:
        return "error", []
    if len(results) == 1 and results[0][1] == "读取文件":
        return "error", []          # 解析失败，任何一项校验都没跑过
    fails = [r for r in results if r[0] == "FAIL"]
    warns = [r for r in results if r[0] == "WARN"]
    if fails:
        status = "failed"
    elif warns:
        status = "degraded"
    else:
        status = "complete"
    warnings = [{"step": name, "reason": detail or "未给原因", "message": detail}
                for _, name, detail in warns]
    return status, warnings


SELFTEST_OK = """# 会议纪要：测试会议

## 会议基本信息

会议主题：测试
会议时间：2026-10-19
参会人员：张三、李四

## 会议结论

本次会议决定采用方案 A。

## 关键决策

决定采用方案 A 上线。

## 待办事项

| 序号 | 待办事项 | 责任人/责任部门 | 完成时间 | 优先级 | 备注 |
|---|---|---|---|---|---|
| 1 | 完成上线检查 | 张三 | 10 月 25 日 | 高 | 优先级未明示 |

## 待定与开放问题

预算口径待定，由李四跟进。

## 会议内容

### 上线方案

讨论后确定采用方案 A。
"""

SELFTEST_BAD = """# 会议纪要：坏例

## 会议结论

待确认。

## 关键决策

讨论了方案 A。

## 待办事项

| 序号 | 待办事项 | 责任人 | 完成时间 |
|---|---|---|---|
| 1 | 处理一下 |  |  |

## 会议内容

无。
"""


def selftest():
    import tempfile
    ok = True
    with tempfile.TemporaryDirectory() as td:
        good = Path(td) / "good.md"
        good.write_text(SELFTEST_OK, encoding="utf-8")
        rc, res = validate(good)
        print(f"[{'PASS' if rc == 0 else 'FAIL'}] 正例应通过（exit={rc}）")
        ok &= rc == 0
        for level, name, detail in res:
            if level != "PASS":
                print(f"  {level} {name} {detail}")
        badp = Path(td) / "bad.md"
        badp.write_text(SELFTEST_BAD, encoding="utf-8")
        rc2, res2 = validate(badp)
        caught = [r for r in res2 if r[0] == "FAIL"]
        print(f"[{'PASS' if rc2 == 1 and len(caught) >= 4 else 'FAIL'}] 负例应被抓（exit={rc2}，{len(caught)} 项 FAIL）")
        ok &= rc2 == 1 and len(caught) >= 4
        # 数字溯源负例：成稿出现源材料没有的数字
        src = "本次会议由张三汇报，产量 120 吨。"
        rc3, res3 = validate(good, source_text=src)
        warn_hit = any(r[0] == "WARN" and r[1] == "数字溯源" for r in res3)
        print(f"[{'PASS' if warn_hit else 'FAIL'}] 编造数字应告警")
        ok &= warn_hit
        # 三态判定：干净稿=complete，有 WARN=degraded，有 FAIL=failed
        st_clean, _ = summarize(validate(good)[1])
        st_warn, wl = summarize(res3)
        st_fail, _ = summarize(validate(badp)[1])
        tri_ok = st_clean == "complete" and st_warn == "degraded" and st_fail == "failed" and len(wl) > 0
        print(f"[{'PASS' if tri_ok else 'FAIL'}] 三态判定（{st_clean}/{st_warn}/{st_fail}，告警 {len(wl)} 条）")
        ok &= tri_ok
        # docx 往返：article-format 出稿后标题是段落样式、正文无 `##`，
        # 这条断言防 parse_docx 退回「只认字面 ##」的旧缺陷。
        try:
            from docx import Document
        except ImportError:
            print("[SKIP] docx 往返（未安装 python-docx）")
        else:
            dx = Path(td) / "good.docx"
            doc = Document()

            def flush(rows):
                if not rows:
                    return
                t = doc.add_table(rows=len(rows), cols=len(rows[0]))
                for i, r in enumerate(rows):
                    for j, c in enumerate(r[:len(rows[0])]):
                        t.cell(i, j).text = c

            doc.add_paragraph("会议纪要：测试会议", style="Title")
            pending = []
            for line in SELFTEST_OK.splitlines():
                s = line.strip()
                if s.startswith("# "):
                    continue
                is_table = s.startswith("|")
                if not is_table:
                    flush(pending)
                    pending = []
                if is_table:
                    cells = [c.strip() for c in s.strip("|").split("|")]
                    if not all(set(c) <= set("-: ") for c in cells):
                        pending.append(cells)
                    continue
                if s.startswith("### "):
                    doc.add_paragraph(s[4:], style="Heading 2")
                elif s.startswith("## "):
                    doc.add_paragraph(s[3:].strip(), style="Heading 1")
                elif s:
                    doc.add_paragraph(s)
            flush(pending)
            doc.save(str(dx))
            rc4, res4 = validate(dx)
            docx_ok = rc4 == 0
            print(f"[{'PASS' if docx_ok else 'FAIL'}] docx 往返按样式识别标题（exit={rc4}）")
            if not docx_ok:
                for level, name, detail in res4:
                    if level != "PASS":
                        print(f"  {level} {name} {detail}")
            ok &= docx_ok

        # 禁用标注必须扫到基本信息区：只在四个必填节正文里查时，
        # 「会议时间：待确认」写进基本信息区能整篇通过（实测）
        basic_forbidden = SELFTEST_OK.replace("会议时间：2026-10-19", "会议时间：待确认")
        bf = Path(td) / "basic-forbidden.md"
        bf.write_text(basic_forbidden, encoding="utf-8")
        rc5, res5 = validate(bf)
        caught5 = any(r[0] == "FAIL" and r[1] == "禁用标注" for r in res5)
        print(f"[{'PASS' if caught5 else 'FAIL'}] 基本信息区的禁用标注应被抓（exit={rc5}）")
        ok &= caught5

        # 源文时间戳不得参与数字比对：源文里的 [00:12] 会让编造的「12 台」看起来有出处
        ts_src = "[00:12] 张三：今天讨论一下。\n[01:30] 李四：好的。"
        rc6, res6 = validate(good, source_text=ts_src)
        caught6 = any(r[0] == "WARN" and r[1] == "数字溯源" for r in res6)
        print(f"[{'PASS' if caught6 else 'FAIL'}] 时间戳不得为编造数字背书")
        ok &= caught6

        # 读取失败必须是 error 态，不能打印 complete
        junk = Path(td) / "junk.md"
        junk.write_bytes(b"\xff\xfe\x00\x00not-utf8")
        rc7, res7 = validate(junk)
        st7, _ = summarize(res7)
        err_ok = rc7 == 2 and st7 == "error"
        print(f"[{'PASS' if err_ok else 'FAIL'}] 读取失败应为 error 态（exit={rc7}，status={st7}）")
        ok &= err_ok
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def export_structured(path):
    """把纪要解析成机器可读结构：基本信息 / 各节文本 / 待办表 / 决策列表。

    纪要到这一步之前只是给人看的文档，下游（任务同步、跨会议检索、报表）
    拿不到任何字段。这里复用校验器已有的解析逻辑，不新增第二套解析器——
    两套解析器必然漂移，而校验器已经覆盖 md 与 docx 两种输入。"""
    blocks = parse_md(path.read_text(encoding="utf-8", errors="replace")) \
        if path.suffix.lower() == ".md" else parse_docx(path)
    basic, sections, _ = split_sections(blocks)
    action_items, _header = collect_action_items(sections.get("待办事项") or [])
    decisions = collect_decisions(sections.get("关键决策") or [])
    return {
        "schema_version": 1,
        "source_file": str(path),
        "basic_info": basic,
        "sections": {k: section_text(v) for k, v in sections.items()},
        "action_items": action_items,
        "decisions": decisions,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="结构化总结结构校验与源文核对（会议版章节名）")
    ap.add_argument("file", nargs="?", help="纪要 md/docx")
    ap.add_argument("--source", default=None, help="源材料文本（转写稿/笔记）")
    ap.add_argument("--strict", action="store_true", help="WARN 也判失败")
    ap.add_argument("--json", dest="json_path", default=None, help="结果落 JSON")
    ap.add_argument("--export", default=None, metavar="JSON",
                    help="导出结构化纪要（基本信息/各节/待办表/决策列表）到指定 JSON")
    ap.add_argument("--selftest", action="store_true")
    ns = ap.parse_args(argv)
    if ns.selftest:
        return selftest()
    if not ns.file:
        ap.error("需要纪要文件，或 --selftest")
    path = Path(ns.file)
    if not path.is_file():
        print(f"文件不存在：{path}")
        return 2
    if ns.export:
        data = export_structured(path)
        Path(ns.export).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"结构化纪要已导出：{ns.export}"
              f"（待办 {len(data['action_items'])} 条，决策 {len(data['decisions'])} 条）")
    source = Path(ns.source).read_text(encoding="utf-8", errors="replace") if ns.source else None
    rc, results = validate(path, source, ns.strict)
    for level, name, detail in results:
        print(f"[{level}] {name}" + (f"  {detail}" if detail else ""))
    fails = sum(1 for r in results if r[0] == "FAIL")
    warns = sum(1 for r in results if r[0] == "WARN")
    status, warnings = summarize(results)
    print(f"\n结论：{'PASS' if rc == 0 else 'FAIL'}（FAIL {fails}，WARN {warns}）")
    print(f"状态：{status}")
    for w in warnings:
        print(f"  告警步骤：{w['step']} — {w['reason']}")
    if ns.json_path:
        Path(ns.json_path).write_text(json.dumps(
            {"file": str(path), "verdict": "PASS" if rc == 0 else "FAIL",
             "status": status,
             "results": [{"level": a, "name": b, "detail": c} for a, b, c in results],
             "processing_warnings": warnings},
            ensure_ascii=False, indent=2), encoding="utf-8")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
