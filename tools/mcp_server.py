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
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parent.parent
PIPELINE = ROOT / "av_to_transcript_and_minutes.py"
JOBS_DIR = Path(os.environ.get("AV2M_JOBS") or (Path.home() / ".av2m" / "jobs"))


class TranscribeOptions(BaseModel):
    """转写选项。单一事实源——schema、校验与命令行拼装都从这里出，
    不再维护「白名单集合 + bool 映射表 + 值映射表」三张需要人工保持一致的表。

    对外暴露成有类型的字段后，agent 从 inputSchema 就能看到每个参数的类型与合法取值，
    拼错在 schema 校验阶段即失败，不必等运行到 build_cmd 才报错。
    """

    model_config = ConfigDict(extra="forbid")   # 拼错的键直接拒，不静默忽略

    # —— 布尔开关 ——
    meeting: bool = Field(False, description="产出带时间戳与说话人标签的会议原文（默认产出转写文稿）")
    no_diarize: bool = Field(False, description="跳过说话人分离，单人素材更快；与 meeting 互斥")
    srt: bool = Field(False, description="额外产出 SRT 字幕文件")
    vtt: bool = Field(False, description="额外产出 WebVTT 字幕文件")
    rttm: bool = Field(False, description="额外产出标准 RTTM 说话人日志，供外部复核工具直读")
    itn: bool = Field(False, description="启用逆文本正则化，把「二零二六年」规整成 2026 年")
    emotion: bool = Field(False, description="标注每段语音的情绪")
    denoise: bool = Field(False, description="人声增强前处理，适用嘈杂或带背景音乐的素材")
    split_channels: bool = Field(False, description="按声道拆分后分别转写，适用双轨录音")
    no_separate: bool = Field(False, description="跳过人声/背景音分离，纯人声素材可省大量时间")

    # —— 带值参数 ——
    diarize_engine: Optional[Literal["campp", "pyannote", "auto"]] = Field(
        None, description="说话人分离引擎。auto=会议用 pyannote、其余用 campp")
    asr_engine: Optional[Literal["qwen", "aed", "auto", "cloud"]] = Field(
        None, description="识别引擎。qwen/aed 本地，cloud 走百炼云端，auto 按语言路由")
    vad: Optional[Literal["auto", "fsmn", "firered"]] = Field(
        None, description="VAD 后端，auto 按素材自动选择")
    num_speakers: Optional[int] = Field(
        None, ge=1, description="已知说话人数，精确人数最准（仅 pyannote 引擎）")
    min_speakers: Optional[int] = Field(None, ge=1, description="说话人数下限（仅 pyannote 引擎）")
    max_speakers: Optional[int] = Field(None, ge=1, description="说话人数上限，不确定人数时用它更稳")
    language: Optional[str] = Field(None, description="指定语言，如 zh / en / ja；不填则自动识别")
    names: Optional[str] = Field(None, description="说话人改名，形如「1=张三,2=李四」")
    hotwords: Optional[str] = Field(None, description="热词表文件路径，提升专有名词识别率")
    replace: Optional[str] = Field(None, description="确定性替换，形如「错词=>对词」，可重复逗号分隔")
    speaker_db: Optional[str] = Field(
        None, description="声纹库路径，跨文件复用说话人身份；传 auto 用默认位置")
    output: Optional[str] = Field(None, description="输出目录；默认 <输入所在目录>/separated_out")

    def to_cli(self) -> dict:
        """转成 build_cmd 认的扁平 dict：只保留显式设置过的项（exclude_unset 保住
        「传 False 与不传」的区别——不传时用管线默认值，传 False 才显式关闭）。"""
        out = {}
        for name, val in self.model_dump(exclude_unset=True).items():
            if name in _BOOL_FLAGS:
                if val:
                    out[name] = True
            elif val not in (None, "", False):
                out[name] = val
        return out

    @model_validator(mode="after")
    def _check_mutual_exclusion(self):
        """互斥组合必须在提交时就拒。放到子进程里炸的话，transcribe_start 已经返回了
        job_id，agent 拿到的是 queued，要等轮询到 failed 才知道参数错了——
        而错误原因埋在 run.log 里。这与「参数错误应在 schema 校验阶段失败」的原则冲突。
        """
        if self.meeting and self.no_diarize:
            raise ValueError("meeting 与 no_diarize 互斥：--meeting 需要说话人分离产出说话人标签")
        lo, hi = self.min_speakers, self.max_speakers
        if lo and hi and lo > hi:
            raise ValueError(f"min_speakers({lo}) 不能大于 max_speakers({hi})")
        if self.num_speakers and (lo or hi):
            # 管线里 num_speakers 优先级更高，同时给会让 min/max 静默失效
            raise ValueError("num_speakers 与 min_speakers/max_speakers 不要同时给；"
                             "已知人数用 num_speakers，不确定才用区间")
        return self


# 布尔开关 → 命令行 flag；其余字段统一走 _VALUE_FLAGS
_BOOL_FLAGS = {
    "meeting": "--meeting", "no_diarize": "--no-diarize", "srt": "--srt",
    "vtt": "--vtt", "rttm": "--rttm", "itn": "--itn", "emotion": "--emotion",
    "denoise": "--denoise", "split_channels": "--split-channels",
    "no_separate": "--no-separate",
}
_VALUE_FLAGS = {
    "diarize_engine": "--diarize-engine", "num_speakers": "--num-speakers",
    "min_speakers": "--min-speakers", "max_speakers": "--max-speakers",
    "asr_engine": "--asr-engine", "language": "--language", "names": "--names",
    "hotwords": "--hotwords", "replace": "--replace", "speaker_db": "--speaker-db",
    "vad": "--vad", "output": "--output",
}
# 白名单由模型字段派生，不再手写第二份
_ALLOWED_OPTIONS = set(TranscribeOptions.model_fields)

# 字段必须全部有命令行映射，否则新增字段会被 to_cli 静默丢弃（agent 传了却不生效）。
# 在导入期就断，比在运行期排查「参数没起作用」便宜得多。
_mapped = set(_BOOL_FLAGS) | set(_VALUE_FLAGS)
assert _ALLOWED_OPTIONS == _mapped, (
    f"TranscribeOptions 字段与命令行映射表不一致："
    f"缺映射={sorted(_ALLOWED_OPTIONS - _mapped)}，多余={sorted(_mapped - _ALLOWED_OPTIONS)}")


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
    for k, flag in _BOOL_FLAGS.items():
        if options.get(k):
            cmd.append(flag)
    for k, flag in _VALUE_FLAGS.items():
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
    # options 可以是 TranscribeOptions 或普通 dict（测试与内部调用传 dict）
    opts = options if isinstance(options, TranscribeOptions) else TranscribeOptions(**options)
    flat = opts.to_cli()
    jid = job_key(src, flat)
    existing = read_job(jid)
    if existing and existing.get("status") in ("queued", "running", "completed"):
        # 幂等：同一份内容 + 同一份参数，直接复用
        existing["reused"] = True
        return jid, existing
    cmd = build_cmd(src, flat)
    data = {"job_id": jid, "source": str(src), "options": flat,
            "command": cmd, "status": "queued", "created_at": now(),
            "output": flat.get("output") or str(src.parent / "separated_out")}
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
        from mcp.server.fastmcp import FastMCP, Context
        from mcp.types import ToolAnnotations
    except ImportError:
        raise SystemExit("需要 MCP Python SDK：pip install mcp（或 uv sync --extra mcp）")
    mcp = FastMCP("av-to-minutes")

    # —— 输出模型：schema 由类型注解自动生成，返回时由框架校验 ——
    # 声明 outputSchema 后客户端能按 schema 校验，字段名也成了稳定契约。
    class StartOut(BaseModel):
        job_id: str
        status: str
        reused: bool = False
        output: Optional[str] = None
        message: str

    class StatusOut(BaseModel):
        job_id: str
        status: str
        source: Optional[str] = None
        created_at: Optional[str] = None
        started_at: Optional[str] = None
        finished_at: Optional[str] = None
        exit_code: Optional[int] = None
        error: Optional[str] = None
        log: Optional[str] = None
        waited_seconds: float = 0.0

    class ResultOut(BaseModel):
        status: str
        total: int = 0
        offset: int = 0
        next_offset: Optional[int] = None
        has_more: bool = False
        speakers: Optional[list] = None
        overlaps: Optional[list] = None
        segments: list = []
        result_file: Optional[str] = None
        note: str

    class CancelOut(BaseModel):
        job_id: str
        status: str
        killed: bool

    class ListOut(BaseModel):
        jobs: list
        count: int

    class SearchOut(BaseModel):
        exit_code: int
        output: str

    class TimelineOut(BaseModel):
        exit_code: int
        path: Optional[str] = None
        output: str = ""
        status: Optional[str] = None
        error: Optional[str] = None

    class MinutesExportOut(BaseModel):
        exit_code: int
        exported: bool
        json_path: Optional[str] = None
        action_items: int = 0
        decisions: int = 0
        sections: int = 0
        verdict: Optional[str] = None
        output: str = ""

    class MinutesEvalOut(BaseModel):
        exit_code: int
        verdict: Optional[str] = None
        metrics: Optional[dict] = None
        failed_checks: list = []
        unjudged: list = []
        output: str = ""

    def _run_module(fn, argv):
        """跑一个带 main(argv) 的 tools 模块，捕获 stdout 与 SystemExit。"""
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                rc = fn(argv)
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
            except Exception as e:               # 工具内部异常不该炸掉整个 server
                rc = 1
                print(f"{type(e).__name__}: {e}", file=buf)
        return rc, buf.getvalue()

    @mcp.tool(
        structured_output=True,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                    idempotentHint=True, openWorldHint=False),
    )
    def transcribe_start(path: str, options: Optional[TranscribeOptions] = None) -> StartOut:
        """提交转写任务，立即返回 job_id，不等转写跑完。

        何时用：用户给了音视频文件、要转文字或要会议原文时。文件必须是服务端本机可读的绝对路径。
        何时不用：只想查已有任务状态用 transcribe_status；只想取结果用 transcribe_result。
        幂等：同一份文件内容 + 同一份参数重复提交会直接复用已有任务（reused=true），不会重复烧算力。
        长任务：本调用只投递，立刻返回。随后用 transcribe_status 轮询，status 为 completed 再取结果。
        """
        jid, data = start_job(path, options or TranscribeOptions())
        return StartOut(
            job_id=jid, status=data.get("status") or "queued",
            reused=bool(data.get("reused")), output=data.get("output"),
            message=("已复用同内容同参数的既有任务，直接用 transcribe_result 取结果"
                     if data.get("reused") else
                     "任务已提交，用 transcribe_status 轮询直至 status=completed"),
        )

    @mcp.tool(
        structured_output=True,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                    idempotentHint=True, openWorldHint=False),
    )
    async def transcribe_status(job_id: str, wait_seconds: int = 0, ctx: Context = None) -> StatusOut:
        """查询任务状态，可选有界长轮询等待。

        何时用：提交任务后跟进进度；或想知道任务为何失败（看 error 与 log 路径）。
        何时不用：要取转写内容用 transcribe_result——本工具只回状态，不回逐段文本。
        长轮询：wait_seconds 大于 0 时最多等这么久（内部夹到 60 秒，避免撞客户端调用超时），
        任务提前结束就提前返回。默认 0 即立即返回，与旧行为一致。
        状态取值：queued / running / completed / failed / cancelled。
        """
        wait = max(0, min(int(wait_seconds or 0), 60))
        started = time.time()
        data = read_job(job_id)
        if not data:
            raise ValueError(f"没有这个任务：{job_id}")
        # 有界长轮询：状态进入终态或等待用尽即返回
        while wait > 0:
            st = data.get("status")
            if st in ("completed", "failed", "cancelled"):
                break
            elapsed = time.time() - started
            if elapsed >= wait:
                break
            if ctx is not None:
                # progress 必须递增，且完成即停——这里按秒推进
                try:
                    await ctx.report_progress(progress=round(elapsed, 1), total=float(wait),
                                              message=f"{st}，已等 {elapsed:.0f}/{wait}s")
                except Exception:
                    pass                      # 客户端不支持 progress 时静默降级，不影响轮询
            await asyncio.sleep(min(1.0, wait - elapsed))
            data = read_job(job_id) or data
        out = {k: data.get(k) for k in
               ("job_id", "status", "source", "created_at", "started_at",
                "finished_at", "exit_code", "error")}
        out["log"] = str(job_dir(job_id) / "run.log")
        out["waited_seconds"] = round(time.time() - started, 2)
        return StatusOut(**out)

    @mcp.tool(
        structured_output=True,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                    idempotentHint=True, openWorldHint=False),
    )
    def transcribe_result(job_id: str, offset: int = 0, limit: int = 200) -> ResultOut:
        """分页取逐段转写结果。任务完成前也能取到已完成的部分（若中间结果已落盘）。

        何时用：任务已投递、要读逐段文本时。也用于分段续读长音频——两小时会议轻松上千段。
        何时不用：任务刚提交还没产出任何结果时先调 transcribe_status。
        分页语义：has_more 为 true 说明后面还有内容，必须继续用 next_offset 拉取直到 has_more 为 false，
        才能对整段素材下结论。只拿一页就总结会漏掉后半程，并对未覆盖部分错判为「未提及」。
        """
        data = read_job(job_id)
        if not data:
            raise ValueError(f"没有这个任务：{job_id}")
        payload, path = read_transcript(data)
        if not payload:
            return ResultOut(status=data.get("status") or "unknown", note="转写结果尚未落盘")
        lines = payload.get("lines") or []
        page = lines[offset:offset + limit]
        end = offset + len(page)
        has_more = end < len(lines)
        return ResultOut(
            status=data.get("status"), total=len(lines), offset=offset,
            next_offset=end if has_more else None, has_more=has_more,
            speakers=payload.get("speakers"), overlaps=payload.get("overlaps") or [],
            segments=page, result_file=str(path),
            note=(f"还有 {len(lines) - end} 段未取，用 offset={end} 继续；"
                  f"未拉完全部段落前不要总结，也不要对未覆盖部分断言「未讨论」"
                  if has_more else
                  f"已取完全部 {len(lines)} 段，可以基于这些内容做总结"),
        )

    @mcp.tool(
        structured_output=True,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                    idempotentHint=False, openWorldHint=False),
    )
    def transcribe_cancel(job_id: str) -> CancelOut:
        """取消一个正在跑的任务，终止子进程并保留已落盘的分块与部分结果。

        何时用：投错素材、参数错了、或用户明确不要了。
        何时不用：任务已完成就用不到它，取消不会删除已有产物。
        可逆性：不可撤销，但素材与参数都在，重新 transcribe_start 即可重跑，因此是可恢复操作。
        """
        data = read_job(job_id)
        if not data:
            raise ValueError(f"没有这个任务：{job_id}")
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
        return CancelOut(job_id=job_id, status="cancelled", killed=killed)

    @mcp.tool(
        structured_output=True,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                    idempotentHint=True, openWorldHint=False),
    )
    def transcribe_list(limit: int = 20) -> ListOut:
        """列出最近的转写任务（按创建时间倒序）。

        何时用：想知道有哪些任务、找某个素材对应的 job_id、或确认之前的任务是否完成。
        何时不用：已知 job_id 时直接 transcribe_status 更省往返。
        与 transcribe_status 的区别：本工具一次回多个任务的摘要，不含逐段内容与错误详情。
        """
        rows = [{k: j.get(k) for k in ("job_id", "status", "source", "created_at")}
                for j in list_recent(limit)]
        return ListOut(jobs=rows, count=len(rows))

    @mcp.tool(
        structured_output=True,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                    idempotentHint=True, openWorldHint=False),
    )
    def search_meetings(query: str, speaker: Optional[str] = None,
                        since: Optional[str] = None, limit: int = 20) -> SearchOut:
        """跨会议检索已索引的转写稿与纪要（SQLite FTS5 全文检索）。

        何时用：用户问「上次谁提过某件事」、要跨多个会议找某个词的上下文。
        何时不用：只想看单个任务的转写内容用 transcribe_result；索引为空时先 index_meetings。
        参数：query 关键词用空格分隔多个词；speaker 按说话人过滤；since 形如 2026-11 限定月份之后。
        """
        sys.path.insert(0, str(ROOT / "tools"))
        import meeting_index
        argv = ["search", *query.split()]
        if speaker:
            argv += ["--speaker", speaker]
        if since:
            argv += ["--since", since]
        argv += ["--limit", str(limit)]
        rc, out = _run_module(meeting_index.main, argv)
        return SearchOut(exit_code=rc, output=out)

    @mcp.tool(
        structured_output=True,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                    idempotentHint=True, openWorldHint=False),
    )
    def index_meetings(path: str) -> SearchOut:
        """把目录或单个文件加入跨会议检索索引。

        何时用：首次使用、或新增了会议产物后，让 search_meetings 能检索到它们。
        何时不用：已经索引过且文件未变——本工具按 mtime 跳过未变化文件，重复调用无副作用。
        产物落 ~/.av2m/meetings.db，可用 AV2M_INDEX 覆盖位置。
        """
        sys.path.insert(0, str(ROOT / "tools"))
        import meeting_index
        rc, out = _run_module(meeting_index.main, ["add", path])
        return SearchOut(exit_code=rc, output=out)

    @mcp.tool(
        structured_output=True,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                    idempotentHint=True, openWorldHint=False),
    )
    def render_timeline(job_id: str, output: Optional[str] = None) -> TimelineOut:
        """为某个任务生成说话人泳道图（HTML 片段），返回文件路径。

        何时用：需要直观展示「谁在什么时候说话、重叠多少」；或要交给支持内联渲染的宿主展示。
        何时不用：只需要文字内容时不必生成图。
        前提：任务必须有说话人分离结果（会议模式或 --diarize），否则无话可画。
        """
        data = read_job(job_id)
        if not data:
            raise ValueError(f"没有这个任务：{job_id}")
        payload, src = read_transcript(data)
        if not src:
            return TimelineOut(exit_code=1, status=data.get("status"),
                               error="转写结果尚未落盘，等任务完成后再试")
        out = Path(output) if output else (job_dir(job_id) / "timeline.html")
        sys.path.insert(0, str(ROOT / "tools"))
        import speaker_timeline
        rc, log = _run_module(speaker_timeline.main, [str(src), "-o", str(out)])
        return TimelineOut(exit_code=rc, path=str(out), output=log)

    @mcp.tool(
        structured_output=True,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                    idempotentHint=True, openWorldHint=False),
    )
    def export_minutes(minutes: str, source: Optional[str] = None,
                       out: Optional[str] = None) -> MinutesExportOut:
        """把写好的会议纪要导出成结构化 JSON（待办表、决策列表、各节文本）。

        何时用：纪要靠会话内 AI 按模板写出后，下游要同步任务、跨会跟踪或做报表时。
        何时不用：纪要还没写出来；或只想校验格式而不需要 JSON 用 eval_minutes。
        参数：minutes 是纪要 md/docx 路径；source 是原始转写稿路径，给它能额外算溯源类指标；
        out 是 JSON 落盘位置，不填则落在纪要同目录。技能未安装时退出码非零并说明原因。
        """
        sys.path.insert(0, str(ROOT / "tools"))
        import minutes_quality
        argv = ["--export", minutes]
        if out:
            argv += ["--out", out]
        if source:
            argv += ["--source", source]
        rc, log = _run_module(minutes_quality.main, argv)
        # 从导出结果里回填计数，让 agent 不必解析 stdout
        json_path = Path(out) if out else Path(minutes).with_suffix(".json")
        n_items = n_dec = n_sec = 0
        verdict = None
        if json_path.is_file():
            try:
                payload = json.loads(json_path.read_text(encoding="utf-8"))
                n_items = len(payload.get("action_items") or [])
                n_dec = len(payload.get("decisions") or [])
                n_sec = len(payload.get("sections") or {})
                verdict = payload.get("document_status")
            except ValueError:
                pass
        return MinutesExportOut(
            exit_code=rc, exported=json_path.is_file(), json_path=str(json_path),
            action_items=n_items, decisions=n_dec, sections=n_sec,
            verdict=verdict, output=log)

    @mcp.tool(
        structured_output=True,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                    idempotentHint=True, openWorldHint=False),
    )
    def eval_minutes(minutes: str, source: Optional[str] = None,
                     gate: bool = False) -> MinutesEvalOut:
        """对会议纪要跑质量门禁：编造数字、责任人溯源率、数字覆盖率、必填节齐全度、出处覆盖率。

        何时用：纪要写完要验收时；想让「这份纪要是否可信」变成可判定的结论时。
        何时不用：还没有纪要；或只要结构化 JSON 用 export_minutes。
        限制：只做能机械判定的指标。语义忠实度、无依据量化、说话人张冠李戴三类需读语义，
        会出现在 unjudged 字段里，不要把它们当成「已评估且通过」。
        gate=true 时任一机械指标不达标即退出码非零，可直接当 CI 门禁用；gate=false（默认）
        退出码只反映「评测是否跑成」，判断结论一律看 verdict 与 failed_checks，
        不要用 exit_code 判断纪要是否合格。
        """
        sys.path.insert(0, str(ROOT / "tools"))
        import minutes_eval
        if not source:
            # 没给原文时评测会大量误判「编造数字」，明确要求补齐
            raise ValueError("eval_minutes 需要 source 参数（原始转写稿路径），"
                             "否则数字溯源类指标全部失真")
        argv = [minutes, "--source", source]
        if gate:
            argv.append("--gate")
        rc, log = _run_module(minutes_eval.main, argv)
        rep = minutes_eval.evaluate(Path(minutes), Path(source).read_text(
            encoding="utf-8", errors="replace"))
        failed = [k for k, ok in (rep.get("checks") or {}).items() if not ok]
        return MinutesEvalOut(exit_code=rc, verdict=rep.get("verdict"),
                              metrics=rep.get("metrics"), failed_checks=failed,
                              unjudged=rep.get("unjudged") or [], output=log)

    # —— P2-6：把成稿挂成资源，agent 按需拉片段而不是一次塞全文 ——
    @mcp.resource("meeting://{stem}/transcript", mime_type="text/markdown")
    def resource_transcript(stem: str) -> str:
        """某个会议的转写稿全文（按素材名 stem 定位）。"""
        p = _find_artifact(stem, ("转写文稿", "会议原文"), (".md",))
        if not p:
            raise ValueError(f"找不到 {stem} 的转写稿")
        return p.read_text(encoding="utf-8", errors="replace")

    @mcp.resource("meeting://{stem}/minutes", mime_type="text/markdown")
    def resource_minutes(stem: str) -> str:
        """某个会议的纪要全文（按素材名 stem 定位）。"""
        p = _find_artifact(stem, ("会议纪要",), (".md",))
        if not p:
            raise ValueError(f"找不到 {stem} 的纪要")
        return p.read_text(encoding="utf-8", errors="replace")

    @mcp.resource("meeting://{stem}/timeline", mime_type="text/html")
    def resource_timeline(stem: str) -> str:
        """某个会议的说话人泳道图 HTML 片段（需先渲染过）。"""
        p = _find_artifact(stem, ("时间轴", "timeline"), (".html",))
        if not p:
            raise ValueError(f"找不到 {stem} 的泳道图，先调 render_timeline 生成")
        return p.read_text(encoding="utf-8", errors="replace")

    return mcp


def _find_artifact(stem: str, prefixes, exts):
    """找某个素材的产物文件，返回第一个命中的路径。

    查找顺序：先把每个 job 的 output 目录纳入候选（成稿真正落地的地方），
    再加上 jobs 目录自身（泳道图等 MCP 生成物落在这里）。
    """
    bases = []
    if JOBS_DIR.is_dir():
        for d in JOBS_DIR.iterdir():
            if not d.is_dir():
                continue
            bases.append(d)                          # 泳道图等 MCP 直出物
            jf = d / "job.json"
            if jf.is_file():
                try:
                    o = json.loads(jf.read_text(encoding="utf-8")).get("output")
                    if o and Path(o).is_dir():
                        bases.append(Path(o))        # 成稿真正所在的输出目录
                except ValueError:
                    pass
    for base in bases:
        for sub in (base / stem, base):
            if not sub.is_dir():
                continue
            for pre in prefixes:
                for ext in exts:
                    p = sub / f"{pre}-{stem}{ext}"
                    if p.is_file():
                        return p
            # 泳道图没有固定前缀，认 <stem>.html
            if "timeline" in prefixes:
                p2 = sub / f"{stem}.html"
                if p2.is_file():
                    return p2
    return None


def main():
    server = build_server()
    server.run()


if __name__ == "__main__":
    main()
