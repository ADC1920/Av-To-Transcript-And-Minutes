#!/usr/bin/env python3
"""mcp_server.py — 把转写管线暴露成 MCP 工具，供 ZCode / Claude / Cursor 等客户端调用。

为什么要有它：现在让 agent 用这条管线，靠的是 SKILL.md 里写十几条命令行示例，
参数一多必然漂移（用错引擎、漏了 --rttm、把 --no-diarize 和 --meeting 混用）。
MCP 把参数校验、任务状态、结果分页收敛到工具契约里，agent 不需要记住命令行。

工具面（job 语义，参照 agentctl 的那套 start/status/result/cancel）：
    transcribe_start(path, options) -> job_id      提交任务，立即返回
    transcribe_status(job_id)      -> 状态与进度    queued/running/completed/failed
    transcribe_result(job_id, offset, limit)       分页取逐段转写
    transcribe_cancel(job_id)                      取消并保留已落盘分块
    transcribe_list()                              最近任务
    search_meetings(query, speaker, since, limit)  跨会议检索（FTS5）
    render_timeline(job_id)                        说话人泳道图 HTML 片段

关键约定（照抄 talkthrough-mcp 的成熟做法）：
- 幂等：按输入文件内容哈希 + options 哈希去重，重复提交同一文件秒回已有 job。
- 长任务不在工具调用里阻塞：start 立即返回 job_id，管线在子进程里跑。
- 状态来自磁盘上的 job.json，进程重启后仍可查询。

跑法（stdio，MCP 客户端拉起）：
    python tools/mcp_server.py

依赖：pip install mcp（或 uv sync --extra mcp）
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PIPELINE = ROOT / "av_to_transcript_and_minutes.py"
JOBS_DIR = Path(os.environ.get("AV2M_JOBS") or (Path.home() / ".av2m" / "jobs"))

_ALLOWED_OPTIONS = {
    "meeting", "no_diarize", "diarize_engine", "num_speakers", "min_speakers",
    "max_speakers", "srt", "vtt", "rttm", "itn", "emotion", "denoise",
    "split_channels", "no_separate", "asr_engine", "language", "names",
    "hotwords", "replace", "speaker_db", "vad", "output",
}


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def content_hash(path):
    """输入文件内容指纹：大小 + 首尾各 1MB 的 sha256。
    与主管线的 audio_fingerprint 同口径——整文件哈希一小时音频要一两秒，
    而这里只需要认出"是不是同一份素材"。"""
    p = Path(path)
    size = p.stat().st_size
    h = hashlib.sha256(str(size).encode())
    with p.open("rb") as f:
        h.update(f.read(1024 * 1024))
        if size > 2 * 1024 * 1024:
            f.seek(-1024 * 1024, os.SEEK_END)
            h.update(f.read(1024 * 1024))
    return h.hexdigest()[:16]


def job_key(path, options):
    opt = json.dumps(options, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256((content_hash(path) + "|" + opt).encode()).hexdigest()[:16]


def job_dir(job_id):
    return JOBS_DIR / job_id


def job_file(job_id):
    return job_dir(job_id) / "job.json"


def read_job(job_id):
    p = job_file(job_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None


def write_job(job_id, data):
    d = job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    p = job_file(job_id)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)   # 原子替换：查询方永远读不到半截 JSON


def build_cmd(src, options):
    """把工具参数翻译成管线命令行。只接受白名单键——拼错的键静默忽略比报错更危险，
    所以未知键直接拒。"""
    unknown = sorted(set(options) - _ALLOWED_OPTIONS)
    if unknown:
        raise ValueError(f"不认识的参数：{', '.join(unknown)}；可用：{', '.join(sorted(_ALLOWED_OPTIONS))}")
    cmd = [sys.executable, str(PIPELINE), str(src)]
    flag_map = {
        "meeting": "--meeting", "no_diarize": "--no-diarize", "srt": "--srt",
        "vtt": "--vtt", "rttm": "--rttm", "itn": "--itn", "emotion": "--emotion",
        "denoise": "--denoise", "split_channels": "--split-channels",
        "no_separate": "--no-separate",
    }
    value_map = {
        "diarize_engine": "--diarize-engine", "num_speakers": "--num-speakers",
        "min_speakers": "--min-speakers", "max_speakers": "--max-speakers",
        "asr_engine": "--asr-engine", "language": "--language", "names": "--names",
        "hotwords": "--hotwords", "replace": "--replace", "speaker_db": "--speaker-db",
        "vad": "--vad", "output": "--output",
    }
    for k, flag in flag_map.items():
        if options.get(k):
            cmd.append(flag)
    for k, flag in value_map.items():
        v = options.get(k)
        if v not in (None, "", False):
            cmd += [flag, str(v)]
    return cmd


def run_job(job_id, cmd):
    data = read_job(job_id) or {}
    try:
        log_path = job_dir(job_id) / "run.log"
        with log_path.open("w", encoding="utf-8") as log:
            env = os.environ | {"PYTHONIOENCODING": "utf-8"}
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                    text=True, env=env, cwd=str(ROOT))
            data.update({"status": "running", "started_at": now(), "pid": proc.pid})
            write_job(job_id, data)
            rc = proc.wait()
        data = read_job(job_id) or data
        # 取消过的任务保留 cancelled，不被这里覆盖
        if data.get("status") != "cancelled":
            data["status"] = "completed" if rc == 0 else "failed"
            if rc != 0:
                tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
                data["error"] = "\n".join(tail)
        data["finished_at"] = now()
        data["exit_code"] = rc
        write_job(job_id, data)
    except Exception as e:
        data.update({"status": "failed", "error": f"{type(e).__name__}: {e}",
                     "finished_at": now()})
        write_job(job_id, data)


def start_job(path, options):
    src = Path(path).expanduser()
    if not src.is_file():
        raise ValueError(f"文件不存在：{src}")
    jid = job_key(src, options)
    existing = read_job(jid)
    if existing and existing.get("status") in ("queued", "running", "completed"):
        # 幂等：同一份内容 + 同一份参数，直接复用
        existing["reused"] = True
        return jid, existing
    cmd = build_cmd(src, options)
    data = {"job_id": jid, "source": str(src), "options": options,
            "command": cmd, "status": "queued", "created_at": now(),
            "output": options.get("output") or str(src.parent / "separated_out")}
    write_job(jid, data)
    threading.Thread(target=run_job, args=(jid, cmd), daemon=True).start()
    return jid, data


def read_transcript(job):
    """找该 job 的转写 JSON：输出目录下与素材同名的 转写结果*.json，或 <stem>/转写结果*.json。
    两种结构都要认：说话人分离模式落 lines，--no-diarize 落 segments；统一成 segments 返回。"""
    out = Path(job.get("output") or "")
    stem = Path(job.get("source") or "").stem
    cands = []
    for base in (out / stem, out):
        if base.is_dir():
            cands.extend(sorted(base.glob("转写结果*.json"),
                                key=lambda p: p.stat().st_mtime, reverse=True))
    for c in cands:
        try:
            data = json.loads(c.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if not data.get("lines") and data.get("segments"):
            # 不分人模式：把 segments 映射成统一的段结构，下游不必分两套
            data["lines"] = [{"spk": None, "spk_name": None,
                              "start_ms": s.get("start_ms") or 0,
                              "end_ms": s.get("end_ms") or 0,
                              "text": s.get("text") or ""}
                             for s in data["segments"]]
        return data, c
    return None, None


def list_recent(limit=20):
    if not JOBS_DIR.is_dir():
        return []
    jobs = []
    for d in JOBS_DIR.iterdir():
        p = d / "job.json"
        if p.is_file():
            try:
                jobs.append(json.loads(p.read_text(encoding="utf-8")))
            except ValueError:
                continue
    jobs.sort(key=lambda j: j.get("created_at") or "", reverse=True)
    return jobs[:limit]


def build_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        raise SystemExit("需要 MCP Python SDK：pip install mcp（或 uv sync --extra mcp）")
    mcp = FastMCP("av-to-minutes")

    @mcp.tool()
    def transcribe_start(path: str, options: dict | None = None) -> str:
        """提交转写任务，立即返回 job_id（不等转写跑完）。

        同一份文件 + 同一份参数重复提交会直接复用已有任务（幂等）。
        options 可用键：meeting, no_diarize, diarize_engine(campp|pyannote|auto),
        num_speakers, min_speakers, max_speakers, srt, vtt, rttm, itn, emotion,
        denoise, split_channels, no_separate, asr_engine(qwen|aed|auto|cloud),
        language, names, hotwords, replace, speaker_db, vad, output。
        """
        jid, data = start_job(path, options or {})
        return json.dumps({"job_id": jid, "status": data.get("status"),
                           "reused": data.get("reused", False),
                           "output": data.get("output")}, ensure_ascii=False)

    @mcp.tool()
    def transcribe_status(job_id: str) -> str:
        """查询任务状态：queued / running / completed / failed / cancelled。"""
        data = read_job(job_id)
        if not data:
            return json.dumps({"error": f"没有这个任务：{job_id}"}, ensure_ascii=False)
        out = {k: data.get(k) for k in
               ("job_id", "status", "source", "created_at", "started_at",
                "finished_at", "exit_code", "error")}
        out["log"] = str(job_dir(job_id) / "run.log")
        return json.dumps(out, ensure_ascii=False)

    @mcp.tool()
    def transcribe_result(job_id: str, offset: int = 0, limit: int = 200) -> str:
        """分页取逐段转写结果。完成前也能取到已完成的部分（若 JSON 已落盘）。"""
        data = read_job(job_id)
        if not data:
            return json.dumps({"error": f"没有这个任务：{job_id}"}, ensure_ascii=False)
        payload, path = read_transcript(data)
        if not payload:
            return json.dumps({"status": data.get("status"), "segments": [],
                               "note": "转写结果尚未落盘"}, ensure_ascii=False)
        lines = payload.get("lines") or []
        page = lines[offset:offset + limit]
        return json.dumps({
            "status": data.get("status"),
            "total": len(lines),
            "offset": offset,
            "next_offset": offset + len(page) if offset + len(page) < len(lines) else None,
            "speakers": payload.get("speakers"),
            "overlaps": payload.get("overlaps") or [],
            "segments": page,
            "result_file": str(path),
        }, ensure_ascii=False)

    @mcp.tool()
    def transcribe_cancel(job_id: str) -> str:
        """取消任务：终止子进程并保留已落盘的分块与部分结果。"""
        data = read_job(job_id)
        if not data:
            return json.dumps({"error": f"没有这个任务：{job_id}"}, ensure_ascii=False)
        pid = data.get("pid")
        killed = False
        if pid:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                   capture_output=True, check=False)
                else:
                    os.kill(pid, 15)
                killed = True
            except OSError:
                pass
        data.update({"status": "cancelled", "cancelled_at": now()})
        write_job(job_id, data)
        return json.dumps({"job_id": job_id, "status": "cancelled", "killed": killed},
                          ensure_ascii=False)

    @mcp.tool()
    def transcribe_list(limit: int = 20) -> str:
        """最近的转写任务。"""
        rows = [{k: j.get(k) for k in ("job_id", "status", "source", "created_at")}
                for j in list_recent(limit)]
        return json.dumps({"jobs": rows}, ensure_ascii=False)

    @mcp.tool()
    def search_meetings(query: str, speaker: str | None = None,
                        since: str | None = None, limit: int = 20) -> str:
        """跨会议检索已索引的转写稿与纪要（关键词，多个词用空格分隔）。
        since 形如 2026-11，只查该月及之后的文件。"""
        sys.path.insert(0, str(ROOT / "tools"))
        import meeting_index
        argv = ["search", *query.split()]
        if speaker:
            argv += ["--speaker", speaker]
        if since:
            argv += ["--since", since]
        argv += ["--limit", str(limit)]
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                rc = meeting_index.main(argv)
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
        return json.dumps({"exit_code": rc, "output": buf.getvalue()}, ensure_ascii=False)

    @mcp.tool()
    def index_meetings(path: str) -> str:
        """把目录或文件加入跨会议索引（幂等，按 mtime 跳过未变化文件）。"""
        sys.path.insert(0, str(ROOT / "tools"))
        import meeting_index
        from contextlib import redirect_stdout
        import io
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                rc = meeting_index.main(["add", path])
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
        return json.dumps({"exit_code": rc, "output": buf.getvalue()}, ensure_ascii=False)

    @mcp.tool()
    def render_timeline(job_id: str, output: str | None = None) -> str:
        """为某个任务生成说话人泳道图 HTML 片段，返回其路径。"""
        data = read_job(job_id)
        if not data:
            return json.dumps({"error": f"没有这个任务：{job_id}"}, ensure_ascii=False)
        payload, src = read_transcript(data)
        if not src:
            return json.dumps({"status": data.get("status"),
                               "error": "转写结果尚未落盘"}, ensure_ascii=False)
        out = Path(output) if output else (job_dir(job_id) / "timeline.html")
        sys.path.insert(0, str(ROOT / "tools"))
        import speaker_timeline
        from contextlib import redirect_stdout
        import io
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = speaker_timeline.main([str(src), "-o", str(out)])
        return json.dumps({"exit_code": rc, "path": str(out),
                           "output": buf.getvalue()}, ensure_ascii=False)

    return mcp


def main():
    server = build_server()
    server.run()


if __name__ == "__main__":
    main()
