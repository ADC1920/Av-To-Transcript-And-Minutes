#!/usr/bin/env python3
"""meeting_index.py — 跨会议检索：把转写结果与结构化纪要收进 SQLite FTS5。

用途：回答“上次谁提过某个专名”“哪些会议定了这件事”。会议一多，
靠翻目录找文件就没有价值了，需要按内容检索。

跑法：
    python tools/meeting_index.py add out/                    # 递归索引整个输出目录
    python tools/meeting_index.py add out/会议原文-x.md
    python tools/meeting_index.py search 胚布 库龄
    python tools/meeting_index.py search 预算 --speaker 张会计 --since 2026-10
    python tools/meeting_index.py stats

设计取舍：先用 FTS5 而不是向量库。会议检索的典型查询是专名、人名、项目名，
关键词命中率高、结果可解释、零额外依赖；向量检索等有真实查询日志再评估。
索引默认落在 ~/.av2m/meetings.db，可用 AV2M_INDEX 覆盖。

依赖：只用标准库（sqlite3 自带 FTS5）。
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
  id INTEGER PRIMARY KEY,
  source TEXT UNIQUE NOT NULL,     -- 文件绝对路径，兼作幂等键
  title TEXT,
  path TEXT,
  kind TEXT,                       -- transcript / minutes
  indexed_at TEXT,
  mtime REAL,
  line_count INTEGER DEFAULT 0,
  action_item_count INTEGER DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS segments USING fts5(
  source UNINDEXED,
  meeting_id UNINDEXED,
  speaker,
  ts,
  text,
  tokenize = 'trigram'
);
"""
# 分词器用 trigram 而不是 unicode61：中文没有词边界，unicode61 会把整句切成
# 一个大 token，"销售数据"这种子串查询永远命中不了。trigram 按三字滑动窗口建索引，
# 任意长度 ≥3 的中文子串都能匹配，代价是索引体积约翻倍（会议文本量级可接受）。


def index_path():
    return Path(os.environ.get("AV2M_INDEX") or (Path.home() / ".av2m" / "meetings.db"))


def connect(path=None):
    """返回 (连接, 实际库路径)。sqlite3.Connection 不允许附加属性，
    所以库路径靠元组带出，避免各命令各自推导默认路径导致打印与实际不一致。"""
    p = Path(path) if path else index_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(p))
    db.executescript(SCHEMA)
    return db, str(p)


TIMESTAMP_RE = re.compile(r"^\[(\d+):(\d{2})\]\s*([^：:]+)[：:]\s*(.*)$")
MINUTES_HEADING_RE = re.compile(r"^#{1,3}\s+(.+)$")


def parse_transcript_json(path):
    """从 qwen_asr 的结构化 JSON 取逐段发言"""
    data = json.loads(path.read_text(encoding="utf-8"))
    lines = data.get("lines") or []
    if lines:
        return [{"speaker": l.get("spk_name") or f"说话人{l.get('spk')}",
                 "ts": f"{(l.get('start_ms') or 0) // 60000:02d}:{((l.get('start_ms') or 0) // 1000) % 60:02d}",
                 "text": l.get("text") or ""} for l in lines], "transcript"
    segs = data.get("segments") or []
    return [{"speaker": "单人", "ts": f"{(s.get('start_ms') or 0) // 60000:02d}:{((s.get('start_ms') or 0) // 1000) % 60:02d}",
             "text": s.get("text") or ""} for s in segs], "transcript"


def parse_transcript_md(path):
    """会议原文 md：`[00:00] 说话人N：文本`"""
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = TIMESTAMP_RE.match(line.strip())
        if m:
            out.append({"speaker": m.group(3).strip(),
                        "ts": f"{int(m.group(1)):02d}:{m.group(2)}", "text": m.group(4).strip()})
    return out, "transcript"


def parse_minutes(path):
    """纪要 md：按小节切段，整节作为可检索块，避免逐句碎片化"""
    out, cur, buf = [], "概述", []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = MINUTES_HEADING_RE.match(line.strip())
        if m:
            if buf and "".join(buf).strip():
                out.append({"speaker": cur, "ts": "", "text": " ".join(buf).strip()})
            cur, buf = m.group(1).strip(), []
        elif line.strip():
            buf.append(line.strip())
    if buf and "".join(buf).strip():
        out.append({"speaker": cur, "ts": "", "text": " ".join(buf).strip()})
    return out, "minutes"


def load_source(path):
    suffix = path.suffix.lower()
    if suffix == ".json":
        return parse_transcript_json(path)
    if suffix == ".md":
        # 会议原文与纪要都可能是 md：带时间戳行的是原文，否则按纪要处理
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(TIMESTAMP_RE.match(l.strip()) for l in text.splitlines()[:400]):
            return parse_transcript_md(path)
        return parse_minutes(path)
    return None, None


def collect_files(root):
    if root.is_file():
        return [root]
    files = []
    for ext in ("*.json", "*.md"):
        files.extend(root.rglob(ext))
    # 缓存 json 与过程件不索引：它们不是给人读的成稿
    skip = ("_asr_job", "manifest")
    return sorted(p for p in files if not any(s in p.name for s in skip))


def cmd_add(ns):
    root = Path(ns.path)
    if not root.exists():
        raise SystemExit(f"路径不存在：{root}")
    db, db_path = connect(ns.db)
    files = collect_files(root)
    added = skipped = 0
    for f in files:
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        row = db.execute("SELECT id, mtime FROM meetings WHERE source = ?", (str(f),)).fetchone()
        if row and abs(row[1] - mtime) < 1 and not ns.force:
            skipped += 1
            continue
        try:
            segs, kind = load_source(f)
        except (ValueError, UnicodeDecodeError) as e:
            print(f"[跳过] {f.name}：{e}", file=sys.stderr)
            continue
        if not segs:
            continue
        action_items = 0
        if kind == "minutes":
            action_items = sum(1 for s in segs if "待办" in s["speaker"])
        if row:
            db.execute("DELETE FROM segments WHERE meeting_id = ?", (row[0],))
            db.execute("UPDATE meetings SET title=?, path=?, kind=?, indexed_at=?, mtime=?, "
                       "line_count=?, action_item_count=? WHERE id=?",
                       (f.stem, str(f.parent), kind, time.strftime("%Y-%m-%d %H:%M:%S"),
                        mtime, len(segs), action_items, row[0]))
            mid = row[0]
        else:
            cur = db.execute(
                "INSERT INTO meetings(source,title,path,kind,indexed_at,mtime,line_count,action_item_count)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (str(f), f.stem, str(f.parent), kind, time.strftime("%Y-%m-%d %H:%M:%S"),
                 mtime, len(segs), action_items))
            mid = cur.lastrowid
        db.executemany(
            "INSERT INTO segments(source,meeting_id,speaker,ts,text) VALUES(?,?,?,?,?)",
            [(str(f), mid, s["speaker"], s["ts"], s["text"]) for s in segs])
        added += 1
    db.commit()
    total = db.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
    print(f"索引完成：新增/更新 {added}，未变化跳过 {skipped}，库内共 {total} 份（{db_path}）")
    db.close()   # Windows 上未关闭的连接会占住库文件，调用方删不掉目录
    return 0


def cmd_search(ns):
    db, _db_path = connect(ns.db)
    # trigram 分词下 MATCH 支持子串；多词用 AND 连接体现"同时出现"的意图。
    # 两字及以下的词 trigram 覆盖不到，退回 LIKE 子串扫描（会议量级可接受）。
    short = [t for t in ns.query if len(t) < 3]
    long_terms = [t for t in ns.query if len(t) >= 3]
    where, params = [], []
    if long_terms:
        where.append("segments MATCH ?")
        params.append(" AND ".join('"' + t.replace('"', '""') + '"' for t in long_terms))
    for t in short:
        where.append("s.text LIKE ?")
        params.append(f"%{t}%")
    sql = ("SELECT m.title, m.kind, s.speaker, s.ts, s.text, m.path "
           "FROM segments s JOIN meetings m ON m.id = s.meeting_id "
           "WHERE " + (" AND ".join(where) if where else "1=1"))
    if ns.speaker:
        sql += " AND s.speaker LIKE ?"
        params.append(f"%{ns.speaker}%")
    if ns.since:
        sql += " AND m.mtime >= ?"
        params.append(time.mktime(time.strptime(ns.since + "-01", "%Y-%m-%d")))
    if long_terms:
        sql += " ORDER BY rank"
    else:
        sql += " ORDER BY m.mtime DESC"
    sql += " LIMIT ?"
    params.append(ns.limit)
    rows = db.execute(sql, params).fetchall()
    if not rows:
        print("没有命中")
        db.close()
        return 1
    for title, kind, speaker, ts, text, folder in rows:
        head = f"{title}"
        if ts:
            head += f" [{ts}]"
        if speaker:
            head += f" {speaker}"
        snippet = text[:180].replace("\n", " ")
        print(f"{head}\n    {snippet}\n    {folder}")
    print(f"\n命中 {len(rows)} 条")
    db.close()
    return 0


def cmd_stats(ns):
    db, db_path = connect(ns.db)
    n = db.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
    segs = db.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    print(f"库：{db_path}\n会议/文稿 {n} 份，可检索块 {segs} 条")
    for title, kind, lines, at in db.execute(
            "SELECT title, kind, line_count, indexed_at FROM meetings ORDER BY mtime DESC LIMIT 10"):
        print(f"  {at}  {kind:10s} {lines:4d} 段  {title}")
    db.close()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="跨会议检索：SQLite FTS5 索引与查询")
    # --db 挂在顶层与每个子命令上（父解析器共享），两种位置都能写：
    # `--db x add y` 与 `add y --db x` 都合法。子命令用 SUPPRESS 作默认值，
    # 否则它会用 None 覆盖父级已解析出的值——那正是「传了 --db 却静默落到默认库」的原因
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS,
                        help=f"索引库路径（默认 {index_path()}）")
    ap.add_argument("--db", default=None, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", parents=[common],
                           help="索引文件或目录（幂等：按 mtime 跳过未变化的文件）")
    p_add.add_argument("path")
    p_add.add_argument("--force", action="store_true", help="忽略 mtime 强制重建")
    p_add.set_defaults(func=cmd_add)

    p_s = sub.add_parser("search", parents=[common], help="检索")
    p_s.add_argument("query", nargs="+", help="关键词（多个词按 AND）")
    p_s.add_argument("--speaker", default=None, help="按说话人过滤")
    p_s.add_argument("--since", default=None, metavar="YYYY-MM", help="只查该月及之后的文件")
    p_s.add_argument("--limit", type=int, default=20)
    p_s.set_defaults(func=cmd_search)

    p_st = sub.add_parser("stats", parents=[common], help="库概况")
    p_st.set_defaults(func=cmd_stats)

    ns = ap.parse_args(argv)
    # 顶层 --db 与子命令 --db 共用同一 dest：子命令未给时父级值生效，
    # 两者都不给才落默认路径
    if getattr(ns, "db", None) is None:
        ns.db = None
    return ns.func(ns)


if __name__ == "__main__":
    raise SystemExit(main())
