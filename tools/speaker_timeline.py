#!/usr/bin/env python3
"""speaker_timeline.py — 把转写结果渲染成说话人时间轴（泳道图）HTML。

用途：给人复核“谁在什么时候说了多久、哪里在抢话”。RTTM 与发言概览都是文字，
核对一次十几分钟的会议要来回翻；泳道图把整场压成一屏。

跑法：
    python tools/speaker_timeline.py 转写结果.json -o 时间轴.html
    python tools/speaker_timeline.py 会议原文-x.docx -o 时间轴.html   # 从会议原文解析
    python tools/speaker_timeline.py 会议.wav --rttm 会议.rttm -o 时间轴.html

依赖：只用标准库（docx 解析除外，需要 python-docx）。
"""
import argparse
import html
import json
import re
import sys
from pathlib import Path


def fmt_ts(ms, short=False):
    ms = max(0, int(ms))
    h, r = divmod(ms, 3600000)
    m, r = divmod(r, 60000)
    s = r // 1000
    if short and not h:
        return f"{m:02d}:{s:02d}"
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def load_json(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    lines = data.get("lines") or []
    if lines:
        total = max((l.get("end_ms") or 0) for l in lines)
        return [{"spk": l.get("spk_name") or f"说话人{l.get('spk')}",
                 "start": l.get("start_ms") or 0,
                 "end": l.get("end_ms") or 0,
                 "text": l.get("text") or ""} for l in lines], total, data.get("overlaps") or []
    # 无说话人标签的普通转写：全部归到单一轨道，至少能看到时间分布
    segs = data.get("segments") or []
    if segs:
        total = max((s.get("end_ms") or 0) for s in segs)
        return [{"spk": "单人", "start": s.get("start_ms") or 0, "end": s.get("end_ms") or 0,
                 "text": s.get("text") or ""} for s in segs], total, []
    raise SystemExit(f"{path} 里没有 lines 或 segments，无法画时间轴")


def load_rttm(path):
    """RTTM：SPEAKER <uri> <ch> <start_s> <dur_s> <NA> <NA> <spk> <NA> <NA>"""
    segs = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) < 8 or parts[0] != "SPEAKER":
            continue
        start = float(parts[3]) * 1000
        end = (float(parts[3]) + float(parts[4])) * 1000
        segs.append({"spk": parts[7], "start": start, "end": end, "text": ""})
    if not segs:
        raise SystemExit(f"{path} 没有可解析的 SPEAKER 行")
    return segs, max(s["end"] for s in segs), []


def load_docx(path):
    """从会议原文 docx 解析 `[00:00] 说话人N：文本` 行。"""
    from docx import Document
    pat = re.compile(r"^\[(\d+):(\d{2})\]\s*([^：:]+)[：:]\s*(.*)$")
    segs = []
    for para in Document(str(path)).paragraphs:
        m = pat.match(para.text.strip())
        if not m:
            continue
        start = (int(m.group(1)) * 60 + int(m.group(2))) * 1000
        segs.append({"spk": m.group(3).strip(), "start": start, "end": start, "text": m.group(4).strip()})
    if not segs:
        raise SystemExit(f"{path} 里没有 [分:秒] 说话人N： 形式的发言行")
    # docx 只有起点没有终点：用下一条的起点补齐，最后一条按字数估 5 秒
    for i, s in enumerate(segs):
        if i + 1 < len(segs):
            s["end"] = max(s["start"], segs[i + 1]["start"])
        else:
            s["end"] = s["start"] + max(5000, len(s["text"]) * 200)
    return segs, segs[-1]["end"], []


def render(segs, total_ms, overlaps, title):
    total_ms = max(total_ms, 1)
    speakers = []
    for s in segs:
        if s["spk"] not in speakers:
            speakers.append(s["spk"])
    # 每个说话人一条泳道，顺序按首次开口，避免编号跳来跳去
    lanes = {name: i for i, name in enumerate(speakers)}
    lane_h, gap, label_w = 34, 10, 132
    top, bottom = 24, 34
    height = top + len(speakers) * (lane_h + gap) + bottom
    colors = ["--viz-series-1", "--viz-series-2", "--viz-series-3",
              "--viz-series-4", "--viz-series-5", "--viz-series-6"]

    out = []
    for s in segs:
        left = s["start"] / total_ms * 100
        width = max(0.15, (s["end"] - s["start"]) / total_ms * 100)
        color = colors[lanes[s["spk"]] % len(colors)]
        label = html.escape(s["text"][:120]) if s["text"] else ""
        tip = f"{s['spk']} {fmt_ts(s['start'])}–{fmt_ts(s['end'])}"
        if label:
            tip += f"：{label}"
        out.append(
            f'<div class="seg" style="left:{left:.3f}%;width:{width:.3f}%;'
            f'top:{top + lanes[s["spk"]] * (lane_h + gap)}px;background:var({color});" '
            f'title="{html.escape(tip)}" tabindex="0" '
            f'aria-label="{html.escape(tip)}"></div>')

    lane_labels = "".join(
        f'<div class="lane-label" style="top:{top + lanes[n] * (lane_h + gap)}px;'
        f'height:{lane_h}px;">{html.escape(n)}</div>' for n in speakers)

    ov = "".join(
        f'<div class="ov" style="left:{o.get("start_ms", 0) / total_ms * 100:.3f}%;'
        f'width:{max(0.15, (o.get("end_ms", 0) - o.get("start_ms", 0)) / total_ms * 100):.3f}%;'
        f'top:{top}px;height:{len(speakers) * (lane_h + gap) - gap}px;"></div>' for o in overlaps)

    # 时间刻度：按总时长选 6 个等分点
    ticks = "".join(
        f'<div class="tick" style="left:{i / 6 * 100:.3f}%">'
        f'{fmt_ts(total_ms * i / 6, short=True)}</div>' for i in range(7))

    # 发言统计：谁占了多少，比读文字快
    stats = []
    for n in speakers:
        mine = [s for s in segs if s["spk"] == n]
        dur = sum(max(0, s["end"] - s["start"]) for s in mine)
        stats.append((n, len(mine), dur, dur / total_ms * 100))
    stats.sort(key=lambda x: -x[2])

    return f"""<div id="timeline-root">
  <h2 style="margin:0 0 2px;font-weight:500;">{html.escape(title)}</h2>
  <p style="margin:0 0 14px;color:var(--muted-foreground);">总时长 {fmt_ts(total_ms)}，{len(speakers)} 位说话人，{len(segs)} 段发言{('，重叠 ' + str(len(overlaps)) + ' 处') if overlaps else ''}</p>
  <div style="position:relative;height:{height}px;">
    {lane_labels}{ov}{''.join(out)}
    <div style="position:absolute;left:0;right:0;bottom:0;height:22px;">{ticks}</div>
  </div>
  <div class="viz-grid" style="margin-top:10px;">
    {''.join(f'<div class="card" style="padding:8px 10px;"><div style="color:var(--muted-foreground);">{html.escape(n)}</div><div class="viz-stat-value">{d / 1000:.0f}s · {p:.0f}%</div><div style="color:var(--muted-foreground);">{c} 段发言</div></div>' for n, c, d, p in stats[:6])}
  </div>
</div>
<style>
  #timeline-root {{ position:relative; }}
  #timeline-root .seg {{ position:absolute;height:34px;border-radius:4px;opacity:.85;cursor:default; }}
  #timeline-root .seg:hover, #timeline-root .seg:focus {{ opacity:1;outline:2px solid var(--foreground);outline-offset:1px; }}
  #timeline-root .lane-label {{ position:absolute;left:0;display:flex;align-items:center;font-size:12px;color:var(--foreground);max-width:124px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap; }}
  #timeline-root .ov {{ position:absolute;background:var(--muted);opacity:.45;pointer-events:none;border-radius:3px; }}
  #timeline-root .tick {{ position:absolute;font-size:11px;color:var(--muted-foreground);transform:translateX(-50%);white-space:nowrap; }}
  #timeline-root > div:nth-of-type(1) {{ padding-left:132px; }}
</style>
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="把转写结果/会议原文/RTTM 渲染成说话人泳道图 HTML 片段")
    ap.add_argument("input", help="转写结果 JSON / 会议原文 docx")
    ap.add_argument("--rttm", default=None, help="改用 RTTM 作为数据源")
    ap.add_argument("-o", "--output", required=True, help="输出 HTML 片段路径")
    ap.add_argument("--title", default=None, help="图标题（默认取输入文件名）")
    ns = ap.parse_args(argv)

    src = Path(ns.input)
    if ns.rttm:
        segs, total, overlaps = load_rttm(Path(ns.rttm))
    elif src.suffix.lower() == ".docx":
        segs, total, overlaps = load_docx(src)
    else:
        segs, total, overlaps = load_json(src)
    title = ns.title or src.stem
    Path(ns.output).write_text(render(segs, total, overlaps, title), encoding="utf-8")
    print(f"时间轴已生成：{ns.output}（{len(segs)} 段）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
