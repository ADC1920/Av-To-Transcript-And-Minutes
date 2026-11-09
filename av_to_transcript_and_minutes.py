#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音视频 → 转写文稿 / 结构化总结 全自动批量工具（拆轨 + Demucs 人声分离 + 说话人分离转写 + 规范 docx 排版）

流程（视频与音频通用；转写恒走说话人分离）：
  视频 → 无声视频 + 完整音轨（均无损 copy）；音频输入跳过本环（源即完整音频）
  音频/完整音轨 → 人声.wav + 背景音.wav（Demucs 分离）
  人声 → 文字（qwen_asr 恒走 --diarize：VAD 切段 + 声纹聚类说话人）
  文字 → 转写文稿（默认，每4句一段）或 会议原文（--meeting，按时间戳完整转写）
         → article-format 规范 docx；结构化总结由会话内 AI 从会议原文提炼

每个输入产出（同一子目录）：
  无声视频.<原扩展名>           视频流无损提取（仅视频输入）
  完整音轨.<按源音轨编码>       原音轨无损提取（仅视频输入）
  人声.wav / 背景音.wav         从音频中分离出的人声 / 背景音（分离产物）
  人声_转写用.wav               清除静音伪影后供转写的人声（可复现的中间件）
  转写结果.json                 转写结构化结果（说话人分段 / 文本 / 时间戳）
  转写文稿-<名>.md / .docx      转写文稿（默认模式）
  会议原文-<名>.md / .docx      完整原文（--meeting；总结由 AI 从原文提炼，会议场景为「会议纪要-<名>」）

用法：
  python av_to_transcript_and_minutes.py <视频/音频文件或文件夹> [-o 输出目录] [--force] [--no-asr] [--meeting]

常用参数：
  --meeting               产出带说话人与时间戳的会议原文（正式纪要由会话内 AI 提炼）
  --no-diarize            单人素材跳过说话人分离直接整段转写（更快;云端 message 走整文件一次调用）
  --asr-engine cloud      换云端引擎 qwen-audio-3.1-asr-message（免显存;换子模型加 --asr-extra "--cloud-model omni|filetrans"）
  --no-separate           纯人声素材跳过 Demucs 分离，直接转写（更快）
  --names "0=张三,1=李四"  固定说话人姓名（先听一段确认谁是谁）
  --srt                   额外产出字级时间戳字幕（事后加不重转，只补对齐）
  --align-engine native   多语言字幕对齐（Qwen3-ForcedAligner，需下载）；auto/fa-zh 见 --help
  --num-speakers N        已知说话人数（pyannote 引擎，最准的人数控法）
  --pyannote-chunk-s N    pyannote 分块时长（长素材降内存峰值，默认 900，0 关闭）
  --itn-backend auto      ITN 后端（有 WeTextProcessing 就用，否则自写规则）
  --asr-engine aed        换识别引擎（中/英/粤更准）；换引擎会自动用新缓存重转，不复用旧结果
  --replace "错=>对"       确定性专名纠错（同音异字可配 --asr-extra "--fuzzy"），比热词偏置可靠
  --replace-file [词典.txt]  纠错词典（每行 "错=>对"）；未指定=自动加载根目录 replace_dict.txt；裸写=根目录 replace_dict.txt
  --replace-save [词典.txt]  把本次纠错条目合并写回词典（裸写=根目录 replace_dict.txt），下次 --replace-file 裸写即自动带上
  --denoise               转写前 ZipEnhancer 降噪（仅真实含 BGM/强噪素材）
  --vad firered           改 VAD 后端（默认 fsmn）
  --asr-extra "--min-seg 300"  其余 qwen_asr 参数原样透传
  --clean                 完成后删除中间件（三份 wav 与转写缓存 json），只留成稿
  --flat                  平铺输出：成稿直接放素材同目录，删工作子目录与 md 底稿
  --log                   本次运行输出落 <输出目录>/run.log
  --verbose               实时透传子进程输出，排查卡顿与失败原因用

运行期自检：
  跑前查显存，可用量不足给出提示（不阻断）；素材无音轨时直接归类为「无音轨」而不是抛 ffmpeg 原始输出；
  转写后若只得到 1 个说话人且用的是默认 campp，会提示改用 --meeting 或 pyannote 重试；
  批量处理打印 [i/N] 计数与每个素材用时，收尾给总耗时与均值。

完成判定与续跑：
  md 与 docx 齐备才算完成；只有 md 而缺 docx（首跑时未装 article-format）时只补排版，
  不重跑分离与转写。转写按配置分文件缓存，阈值/热词/人名/字幕/识别引擎/额外参数任一不同
  即互不复用；--force 才全部重跑。注意 --clean 会删掉缓存 json 与中间 wav：之后若要换参数
  重转，会重新走一遍分离（这是有意取舍，用 --clean 换磁盘空间）。
"""
import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SENTS_PER_PARA = 4  # 转写文稿分段：每几句拼一段
# 管线自身保留的 qwen_asr 参数，禁止经 --asr-extra 二次传入（否则重复传参会破坏调用契约）
RESERVED_ASR_ARGS = {"--job-config", "--diarize", "--audio"}
# 经任务配置 JSON 透传给 qwen_asr 的键（其余参数一律走命令行 extra，避免"未知项"报错）
ASR_JOB_KEYS = ("threshold", "diarize_engine", "hotwords", "names", "srt", "vtt", "engine")

_asr_script_cache = None


def child_env(**overrides) -> dict:
    """子进程环境：把 Python 侧标准输出锁死为 UTF-8。
    中文 Windows 默认 cp936，父进程若按 cp936 解码子进程的 UTF-8 字节会抛
    UnicodeDecodeError（reader 线程崩掉、捕获输出静默变 None），故子进程输出编码
    与父进程解码编码两端必须同时锁定，缺一端就丢日志。"""
    env = os.environ | {"PYTHONIOENCODING": "utf-8",
                        "HF_HUB_DISABLE_SYMLINKS_WARNING": "1"}
    # demucs/qwen_asr 子进程抗显存碎片(前瞻);Windows 暂不支持 expandable_segments
    # (2026-10-11 实测告警后回落,无副作用),保留待 PyTorch 支持后自动生效
    env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    env.update(overrides)
    return env


def run_child(cmd: list, verbose: bool = False, env: dict = None):
    """统一子进程调用：默认捕获输出并按 UTF-8 解码（errors=replace 兜底不崩）；
    verbose=True 时输出实时透传终端（排查卡顿与失败原因用）"""
    if verbose:
        return subprocess.run(cmd, check=True, env=env, shell=False)
    return subprocess.run(cmd, check=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, shell=False)


def run_output(cmd: list, env: dict = None) -> str:
    """取一次性短输出（如 ffprobe 查询），固定 UTF-8 解码"""
    r = subprocess.run(cmd, check=False, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, shell=False)
    return r.stdout


def split_asr_extra(text: str) -> list:
    """分割 --asr-extra 参数串：按空白切分，支持单/双引号包裹含空格的值。
    刻意不用 shlex：其 posix 模式把反斜杠当转义符，Windows 路径
    （如 --replace-file D:\\dict.txt）会被吃掉一个字符。"""
    tokens, buf, quote = [], [], None
    for ch in text:
        if quote:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
        elif ch == "'" or ch == '"':
            quote = ch
        elif ch.isspace():
            if buf:
                tokens.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        tokens.append("".join(buf))
    return tokens


class AudioTrackMissing(RuntimeError):
    """素材没有可解析的音轨：视频无音轨，或音频文件损坏/非有效音频"""


class _Tee:
    """把标准输出/错误同时写到终端与日志文件（--log 用）"""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass
        return len(data)

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self):
        return False

    def reconfigure(self, **kwargs):  # 兼容调用方对 stdout 的 reconfigure
        pass


def gpu_memory_mb():
    """返回 (总显存, 已用显存) 单位 MB；无 nvidia-smi 或读不到时返回 None"""
    try:
        out = run_output(["nvidia-smi", "--query-gpu=memory.total,memory.used",
                          "--format=csv,noheader,nounits"])
    except (FileNotFoundError, OSError):
        return None
    first = out.strip().splitlines()[0].strip() if out.strip() else ""
    parts = [p.strip() for p in first.split(",")]
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def warn_low_vram(need_mb: int = 4600) -> None:
    """跑前显存预检（只提示不阻断）。实测参考：本链路峰值增量约 4.4 GB，
    阈值取 4600 MB 覆盖峰值（4.0~4.4 GB 区间不再漏报）；Demucs 与转写串行，
    各占一段峰值；卡上若已有常驻服务容易挤到上限。"""
    mem = gpu_memory_mb()
    if mem is None:
        return
    total, used = mem
    free = total - used
    if free < need_mb:
        print(f"[提示] 显存可用约 {free} MB（已用 {used} / 共 {total} MB），低于建议的 {need_mb} MB。"
              f"可先停掉其它占卡程序，或用 --no-separate 省下 Demucs 那一份", file=sys.stderr)


def clean_intermediates(stem_dir: Path) -> list:
    """--clean：删掉分离与转写的中间件，只留成稿（转写文稿/会议原文的 md、docx、srt）。
    拆轨产物（无声视频/完整音轨）与用户素材不动——它们是可以复用的分流件。"""
    names = ["人声.wav", "背景音.wav", "人声_转写用.wav",
             "人声_转写用.json", "人声_转写用.srt", "_asr_job.json"]
    names += [p.name for p in stem_dir.glob("转写结果*.json")]
    removed = []
    for name in sorted(set(names)):
        p = stem_dir / name
        if p.is_file():
            try:
                p.unlink()
                removed.append(name)
            except OSError:
                pass
    return removed


def flatten_outputs(stem_dir: Path, media: Path, out_stem: str) -> None:
    """平铺模式（--flat）收尾：成稿 docx（及 srt）移到素材同目录，删除整个工作子目录
    （含中间件 wav/缓存 json、md 提炼底稿、.demucs_model 标记与拆轨产物）。
    默认 separated_out 工作目录清空后一并移除；-o 指定的目录不删。

    没有搬出任何成稿时**不删目录**：docx 排版可能因 article-format 缺失而跳过，
    此时 md 是本次唯一成果，直接 rmtree 会把它一并删掉（实测：未装排版技能 + --flat
    → 只剩中间件也没了，稿子凭空消失）。"""
    moved = []
    for ext in (".docx", ".srt", ".vtt", ".rttm"):
        src_file = stem_dir / f"{out_stem}{ext}"
        if src_file.exists():
            target = media.parent / f"{out_stem}{ext}"
            try:
                if target.exists():
                    target.unlink()
                shutil.move(str(src_file), str(target))
                moved.append(target.name)
            except OSError:
                pass
    if not moved:
        print(f"[提示] {media.stem}：未产出 docx/字幕/说话人日志，跳过平铺清理以免删掉 md 成稿"
              f"（工作目录保留：{stem_dir}）", file=sys.stderr)
        return
    shutil.rmtree(stem_dir, ignore_errors=True)
    parent = stem_dir.parent
    try:
        if parent.name == "separated_out" and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass
    if moved:
        print(f"[平铺] {media.stem}：成稿已移入素材目录 → {', '.join(moved)}")


def wav_rms_db(path: Path):
    """读 wav 算整体 RMS（dBFS）；读不到或空文件返回 None"""
    try:
        import numpy as np
        import soundfile as sf
        data, _ = sf.read(str(path), dtype="float32", always_2d=True)
        if data.size == 0:
            return None
        mono = data.mean(axis=1)
        rms = float(np.sqrt((mono ** 2).mean()))
        return 20 * math.log10(max(rms, 1e-9))
    except Exception:
        return None


def estimate_snr_db(path: Path):
    """粗估信噪比（dB）：20ms 分帧算 RMS，取 90 分位当语音、10 分位当底噪。
    只用分位数做相对判断，不做绝对声压校准；样本不足或读不到返回 None。"""
    try:
        import numpy as np
        import soundfile as sf
        data, sr = sf.read(str(path), dtype="float32", always_2d=True)
        if data.size == 0:
            return None
        mono = data.mean(axis=1)
        win = max(int(sr * 0.02), 1)
        n = len(mono) // win
        if n < 5:
            return None
        frames = mono[:n * win].reshape(n, win)
        rms = np.sqrt((frames ** 2).mean(axis=1))
        rms = rms[rms > 0]
        if rms.size < 5:
            return None
        speech = float(np.percentile(rms, 90))
        noise = float(np.percentile(rms, 10))
        return 20 * math.log10(max(speech, 1e-9) / max(noise, 1e-9))
    except Exception:
        return None


def decode_16k_mono_tmp(media: Path):
    """ffmpeg 把任意容器解码为 16k mono wav 临时文件（供分离前 SNR 探测，视频/音频通用）。
    失败返回 None，不阻断主流程。"""
    import tempfile
    out = Path(tempfile.gettempdir()) / f"snr_probe_{media.stem[:40]}_{os.getpid()}.wav"
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(media), "-ac", "1",
                        "-ar", "16000", str(out)], capture_output=True)
    if r.returncode != 0 or not out.is_file():
        return None
    return out


def asr_script() -> Path:
    """定位转写引擎 qwen_asr.py（惰性查找：仅在真正转写时调用，不影响 --help）：
    环境变量 VOICE_ASR_SCRIPT → 同目录 → 技能目录（本仓库布局）"""
    global _asr_script_cache
    if _asr_script_cache is not None:
        return _asr_script_cache
    env = os.environ.get("VOICE_ASR_SCRIPT")
    cands = ([Path(env)] if env else []) + [
        SCRIPT_DIR / "qwen_asr.py",
        SCRIPT_DIR / "skills" / "funasr-transcribe" / "scripts" / "qwen_asr.py"]
    for c in cands:
        if c.is_file():
            _asr_script_cache = c
            return c
    sys.exit("找不到 qwen_asr.py：请用环境变量 VOICE_ASR_SCRIPT 指定其路径")


_article_skill_cache = None


def article_skill() -> Path:
    """定位「全局文章排版规范」技能脚本目录（article-format，惰性查找）：
    环境变量 ARTICLE_SKILL_DIR → ZCode 技能根 → 共享 .agents → 仓库 skills/ →
    Claude / 旧 ZCode 路径兜底；找不到时返回 None（跳过排版，产出标准 docx）"""
    global _article_skill_cache
    if _article_skill_cache is not None:
        return _article_skill_cache or None
    env = os.environ.get("ARTICLE_SKILL_DIR")
    cands = ([Path(env)] if env else []) + [
        Path.home() / ".agents" / "skills" / "article-format" / "scripts",
        SCRIPT_DIR / "skills" / "article-format" / "scripts",
        Path.home() / ".claude" / "skills" / "article-format" / "scripts",
        Path.home() / ".zcode" / "skills" / "article-format" / "scripts"]
    for c in cands:
        if (c / "md2docx.py").is_file():
            _article_skill_cache = c
            return c
    _article_skill_cache = False
    return None

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".ts",
              ".wmv", ".m4v", ".3gp", ".mpg", ".mpeg"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".amr"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS
# 源音轨编码 → 无损 copy 时的容器扩展名（codec 名不带点；aac 之外的冷门编码落 mka 万能容器）
AUDIO_EXT = {"aac": ".m4a", "mp3": ".mp3", "flac": ".flac",
             "opus": ".opus", "vorbis": ".ogg", "ac3": ".ac3", "eac3": ".eac3"}

# FireRedVAD 权重目录（与 qwen_asr.py 的 FIRERED_VAD_DIR 同源）
FIRERED_VAD_DIR = Path.home() / ".cache" / "fireredvad" / "FireRedVAD" / "VAD"


def resolve_vad_arg(vad, diarize: bool) -> str:
    """把 --vad auto 解析成具体后端名，供子进程与缓存键共用同一份口径。

    与 qwen_asr.resolve_vad_backend 同源：分离链路用 fsmn（段边界即声纹聚类单元，
    实测碎段会拉高 DER），纯分段链路（--no-diarize）用 FireRedVAD（只当识别批边界，
    边界越准越快，实测 8.5s → 0.37s）。这里只查权重文件是否存在，不 import
    fireredvad——主脚本不加载识别栈的重依赖，缺包时由子进程报错。
    """
    if vad and vad != "auto":
        return vad
    if not (FIRERED_VAD_DIR / "model.pth.tar").is_file():
        return "fsmn"
    return "fsmn" if diarize else "firered"



def probe_audio_codec(video: Path) -> str:
    return run_output(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(video)]).strip().lower()


def asr_cache_name(threshold=None, diarize_engine=None, hotwords=None, names=None,
                   srt=False, engine=None, extra=None, tag=None, single=False,
                   dict_hash=None) -> str:
    """转写缓存文件名：任一影响识别文本的配置不同 → 缓存不同。

    识别引擎（qwen/aed）输出的文本不同，是缓存键的必需维度——漏掉它会让人以为换了引擎、
    实际拿到的是旧引擎结果；热词、人名、额外 ASR 参数同理不可与默认结果混用。
    tag 用于同一目录内存在多份待转写音频的场景（声道分轨），缺了它两路会命中同一份缓存、
    转出完全相同的文本（实测踩过）。
    dict_hash = 纠错词典内容哈希：词典更新后旧缓存不再复用（否则新词条对已缓存素材不生效）。

    srt 刻意不进缓存键：字幕只影响 sentences 字段，lines 的文本完全相同。把 srt 计入键
    会让"先转写、后补字幕"变成整套重转，白烧最贵的识别环节。需要字幕而缓存里没有时，
    走 --align-only 只补对齐（见 call_asr）。参数 srt 保留仅为向后兼容调用签名。"""
    parts = []
    if tag:
        parts.append(str(tag))
    if single:
        parts.append("不分人")
    if engine == "aed":
        parts.append("aed")
    if diarize_engine == "pyannote":
        parts.append("pyannote")
    if threshold is not None:
        parts.append(f"阈值{threshold}")
    if hotwords:
        parts.append("热词" + hashlib.sha256(hotwords.encode("utf-8")).hexdigest()[:6])
    if names:
        parts.append("人名" + hashlib.sha256(names.encode("utf-8")).hexdigest()[:6])
    if dict_hash:
        parts.append("词典" + dict_hash)
    if extra:
        parts.append("扩展" + hashlib.sha256(" ".join(extra).encode("utf-8")).hexdigest()[:6])
    return "转写结果" + ("_" + "_".join(parts) if parts else "") + ".json"


def audio_fingerprint(path: Path) -> str:
    """源音频内容指纹：文件大小 + 首尾各 1MB 的 sha256 前 16 位。

    只取首尾是性能取舍：一小时 44.1kHz 立体声 wav 约 600MB，整文件哈希要 1~2 秒；
    首尾块足以识别「同一目录下人声.wav 被重新分离或替换」，而缓存误复用的代价
    （拿旧文本当新素材结果）远高于这点碰撞概率。
    """
    try:
        size = path.stat().st_size
        h = hashlib.sha256(str(size).encode())
        with path.open("rb") as f:
            h.update(f.read(1024 * 1024))
            if size > 2 * 1024 * 1024:
                f.seek(-1024 * 1024, os.SEEK_END)
                h.update(f.read(1024 * 1024))
        return h.hexdigest()[:16]
    except OSError:
        return ""


def write_cached_subtitles(subs, opts: dict, out_dir: Path, stem: str) -> list:
    """用缓存里已有的 sentences 直接写字幕文件，不重跑对齐。

    场景：先前只出 srt，这次加 --vtt。缓存 sentences 是完整对齐结果，
    格式转换纯属文本重排，没必要再跑一遍对齐模型（那是几秒到几十秒的 GPU 时间）。

    格式与 qwen_asr 的 write_srt/write_vtt 保持一致：SRT 毫秒用逗号、VTT 用点号，
    VTT 首行 WEBVTT。此处独立实现而非 import，是因为主脚本不应为写字幕去
    加载 qwen_asr（它 import 时会拉起 funasr 等重依赖）。
    """
    def fmt(ms, sep):
        ms = max(0, int(ms))
        h, r = divmod(ms, 3600000)
        m, r = divmod(r, 60000)
        s, ms3 = divmod(r, 1000)
        return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms3:03d}"

    cues = []
    for s in subs or []:
        label = ""
        if s.get("spk_name") or s.get("spk") is not None:
            label = f"[{s.get('spk_name') or ('说话人' + str(s['spk']))}] "
        cues.append((f"{label}{s.get('text', '')}", s.get("start_ms", 0), s.get("end_ms", 0)))

    written = []
    if opts.get("srt"):
        p = out_dir / f"{stem}.srt"
        blocks = [f"{i}\n{fmt(a, ',')} --> {fmt(b, ',')}\n{t}\n"
                  for i, (t, a, b) in enumerate(cues, 1)]
        p.write_text("\n".join(blocks), encoding="utf-8")
        written.append(p.name)
    if opts.get("vtt"):
        p = out_dir / f"{stem}.vtt"
        lines = ["WEBVTT", ""]
        for i, (t, a, b) in enumerate(cues, 1):
            lines += [str(i), f"{fmt(a, '.')} --> {fmt(b, '.')}", t, ""]
        p.write_text("\n".join(lines), encoding="utf-8")
        written.append(p.name)
    return written


def _align_srt_only(vocals: Path, cache_json: Path, opts: dict):
    """缓存已有转写、但没有字幕时，只补一遍对齐（--align-only），不重跑识别。

    补字幕的本质只是时间对齐，识别文本完全复用缓存。把这条路径单独开出来，
    才能让 srt 安全地移出缓存键：不然"先前跑过没字幕、现在要字幕"就得整套重转。
    补出的 sentences 写回缓存，下次直接命中，不会每次重算。
    """
    cmd = [sys.executable, str(asr_script()), str(vocals),
           "--align-only", str(cache_json)]
    # 补对齐同样要按用户选的字幕格式产出：qwen_asr 的 --align-only 分支按 --srt/--vtt 落盘
    if opts.get("srt"):
        cmd.append("--srt")
    if opts.get("vtt"):
        cmd.append("--vtt")
    align_engine = opts.get("align_engine")
    if align_engine:
        cmd += ["--align-engine", align_engine]
    if opts.get("language"):
        cmd += ["--language", str(opts["language"])]
    if opts.get("max_line"):
        cmd += ["--max-line", str(opts["max_line"])]
    print(f"[补字幕] {vocals.name}：缓存已有转写，只跑对齐不重转", file=sys.stderr)
    try:
        run_child(cmd, verbose=bool(opts.get("verbose")), env=child_env())
    except subprocess.CalledProcessError:
        # 对齐失败不该让整份素材判失败：转写成果是有效的，只是没字幕
        print(f"[补字幕] {vocals.name}：对齐失败，本次产出无字幕（转写结果不受影响）",
              file=sys.stderr)
        return _read_json(cache_json)
    payload = _read_json(cache_json)
    for ext in (".srt", ".vtt"):
        raw = vocals.with_suffix(ext)
        if payload and raw.exists():
            keep = vocals.parent / (vocals.stem + ext)
            if raw != keep:
                shutil.move(str(raw), str(keep))
    return payload


def call_asr(vocals: Path, opts: dict, force: bool = False, tag: str = None):
    """调 qwen_asr.py 转写（默认 --diarize：说话人分离；opts.no_diarize=True 跳过），返回结构化 JSON。
    opts 键：threshold / diarize_engine / hotwords / names / srt / engine（识别引擎）/ no_diarize
             extra（额外 ASR 参数列表，命令行追加）/ verbose（子进程输出实时透传）
    缓存已存在且非 --force 时直接复用（转写是最贵环节，支持断点续跑）；
    白名单键走任务配置 JSON，其余额外参数以命令行形式追加。

    命中缓存时校验 _source_fp：同一目录的人声.wav 被重新分离或手工替换后，缓存键
    （只含配置维度）不会变，不校验就会把旧文本当新素材结果。旧缓存没有该字段时
    视为兼容并补写当前指纹，不强制重转（避免升级即全量重跑）。

    缓存读失败（写缓存时被杀、磁盘满留下半截 json）按未命中处理并重转，不让一份
    坏缓存把这份素材永久卡死；写缓存一律先写 .tmp 再 os.replace，避免留下半截文件。"""
    extra = list(opts.get("extra") or [])
    # 纯字幕参数不进缓存键：它们只影响 sentences/.srt，不影响 lines 的文本。
    # 计入键会让"换对齐后端"或"调字幕字数"变成整套重转；正确做法是命中缓存后补对齐通道。
    SUBTITLE_ONLY_ARGS = {"--align-engine", "--max-line", "--min-dur", "--max-cps"}
    # 与升级前默认等价的取值也不进键：旧缓存是按 fsmn 跑的、键里没有 --vad，
    # 新版恒显式下发解析结果，把 fsmn 计进键等于让所有历史缓存失效、白烧一次全量重转。
    KEY_EQUIV = {("--vad", "fsmn")}
    key_extra = []
    i = 0
    while i < len(extra):
        tok = extra[i]
        nxt = extra[i + 1] if i + 1 < len(extra) else None
        if tok in SUBTITLE_ONLY_ARGS or (tok, nxt) in KEY_EQUIV:
            i += 2
            continue
        key_extra.append(tok)
        if nxt is not None and not nxt.startswith("--"):
            key_extra.append(nxt)
            i += 2
        else:
            i += 1
    cache_json = vocals.parent / asr_cache_name(
        opts.get("threshold"), opts.get("diarize_engine"), opts.get("hotwords"),
        opts.get("names"), bool(opts.get("srt")), opts.get("engine"), key_extra, tag,
        single=bool(opts.get("no_diarize")), dict_hash=opts.get("dict_hash"))
    source_fp = audio_fingerprint(vocals)

    def _need_srt(payload):
        """本素材要字幕、而缓存里没有 sentences 时，补一次对齐（不重转）。

        缓存已有 sentences 时不补对齐，但若用户这次要的格式（srt/vtt）还没落盘，
        仍需按缓存里的 sentences 直接写——否则"先前只出 srt，这次加 --vtt"会拿不到文件。
        """
        if not (opts.get("srt") or opts.get("vtt")):
            return payload
        subs = (payload or {}).get("sentences")
        if not subs:
            return _align_srt_only(vocals, cache_json, opts)
        written = write_cached_subtitles(subs, opts, vocals.parent, vocals.stem)
        if written:
            print(f"[补字幕] {vocals.name}：按缓存 sentences 补写 {', '.join(written)}",
                  file=sys.stderr)
        return payload

    if cache_json.exists() and not force:
        payload = _read_json(cache_json)
        if payload is not None and not _schema_ok(payload):
            # 结构版本不认识：字段含义可能已变，复用它等于拿错数据当结果
            print(f"[缓存] {cache_json.name} 结构版本 {payload.get('schema_version')} "
                  f"当前仅支持 {CACHE_SCHEMA_VERSION}，按未命中重转", file=sys.stderr)
            payload = None
        if payload is not None:
            cached_fp = payload.get("_source_fp")
            if not cached_fp:
                payload["_source_fp"] = source_fp
                _write_json(cache_json, payload)
                return _need_srt(payload)
            if cached_fp == source_fp:
                return _need_srt(payload)
            print(f"[缓存] {vocals.name} 源音频已变化（指纹 {cached_fp}→{source_fp}），重转",
                  file=sys.stderr)
        else:
            print(f"[缓存] {cache_json.name} 无法解析，按未命中重转", file=sys.stderr)
    raw_json = vocals.parent / (vocals.stem + ".json")  # qwen_asr 固定输出名：<音频名>.json
    raw_srt = vocals.with_suffix(".srt")
    cfg_path = vocals.parent / "_asr_job.json"
    # 只透传 qwen_asr 认识的键：opts 里的 extra / verbose 不是它的参数，混进去会被判"未知项"退出
    cfg = {k: opts[k] for k in ASR_JOB_KEYS if opts.get(k) not in (None, False)}
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    raw_json.unlink(missing_ok=True)
    raw_srt.unlink(missing_ok=True)
    cmd = [sys.executable, str(asr_script()), str(vocals)]
    if not opts.get("no_diarize"):
        cmd.append("--diarize")
    cmd += ["--job-config", str(cfg_path)] + extra
    run_child(cmd, verbose=bool(opts.get("verbose")), env=child_env())
    cfg_path.unlink(missing_ok=True)
    # 补写源指纹：下次命中时据此判断素材是否被换过
    payload = _read_json(raw_json) or {"lines": []}
    payload["_source_fp"] = source_fp
    _write_json(cache_json, payload)
    raw_json.unlink(missing_ok=True)
    return payload


def _read_json(path: Path):
    """读 JSON；解析失败或读不到返回 None（损坏缓存按未命中处理）"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# 转写结果结构版本：与 qwen_asr.SCHEMA_VERSION 同步，lines 结构改动时两边一起升
CACHE_SCHEMA_VERSION = 1


def _schema_ok(payload) -> bool:
    """缓存是否可安全复用的结构校验。

    只看能不能识别版本，不逐字段深检——字段级缺失由调用方 .get 兜底散步即可，
    真正会把错数据当结果用的是结构变体（比如 lines 改成嵌套字典）。
    版本号缺失时按 v1 认：升级前的老缓存正是这套字段，不自作主张重转白烧识别。
    """
    if not isinstance(payload, dict):
        return False
    return payload.get("schema_version", 1) == CACHE_SCHEMA_VERSION



def _write_json(path: Path, payload) -> None:
    """原子写 JSON：先写同目录 .tmp 再 os.replace，避免中途被杀留下半截文件"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def transcript_md(payload: dict, stem: str) -> str:
    """默认模式：转写文稿——从说话人分段结构拼全文，按 4 句一段成稿（不带说话人标签）"""
    lines = payload.get("lines") or []
    text = "\n".join((l.get("text") or "").strip() for l in lines).strip()
    if not text:
        text = (payload.get("text") or "").strip()  # 兜底非 diarize 结构
    if not text:
        return ""
    sents = [s.strip() for s in re.split(r"(?<=[。！？!?])", text.replace("\n", "")) if s.strip()]
    paras = ["".join(sents[i:i + SENTS_PER_PARA]) for i in range(0, len(sents), SENTS_PER_PARA)]
    return f"# 转写文稿：{stem}\n\n" + "\n\n".join(paras) + "\n"


def write_rttm(lines, path: Path, uri: str) -> int:
    """写 RTTM 说话人日志，返回写入行数。

    格式（dscore / pyannote.metrics / meeteval 都直接读）：
      SPEAKER <文件标识> 1 <起点秒> <时长秒> <NA> <NA> <说话人> <NA> <NA>
    由主管线自己写而不是转存子进程的产物：缓存命中时子进程根本没跑，而 lines 一定
    在 payload 里，一份数据源才能让"首次运行"和"补文件"两条路径都成立。
    """
    rows = []
    for l in lines or []:
        if l.get("spk") is None:
            continue
        start = max(0.0, int(l.get("start_ms", 0)) / 1000.0)
        end = max(start, int(l.get("end_ms", 0)) / 1000.0)
        if end - start <= 0:
            continue
        label = str(l.get("spk_name") or f"spk{l['spk']}")
        label = re.sub(r"\s+", "_", label) or f"spk{l['spk']}"
        rows.append(f"SPEAKER {uri} 1 {start:.3f} {end - start:.3f} <NA> <NA> {label} <NA> <NA>")
    path.write_text("".join(r + "\n" for r in rows), encoding="utf-8")
    return len(rows)


def _fmt_dur(ms) -> str:
    """毫秒 -> 人读时长（1小时03分05秒 / 3分05秒）"""
    s = max(0, int(ms)) // 1000
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}小时{m:02d}分{s:02d}秒" if h else f"{m}分{s:02d}秒"


def speaker_summary(payload: dict) -> str:
    """会议原文顶部的发言概览：谁在场、各说了多久、什么时候开口。

    为什么放在原文里而不是另出报告：提炼纪要的人是拿着这份 md 工作的，
    "谁占了 60% 的发言、谁只说了两句"是会直接改变纪要权重与责任归属判断的
    信息，放在同一份文本里才不会被漏读。只用列表不用表格——article-format
    的 md 转 docx 对列表最稳。
    """
    lines = [l for l in (payload.get("lines") or []) if (l.get("text") or "").strip()]
    if len({(l.get("spk_name") or l.get("spk")) for l in lines}) < 2:
        return ""      # 单人素材没有对比意义，不占版面
    agg = {}
    for l in lines:
        key = l.get("spk_name") or f"说话人{l.get('spk')}"
        d = agg.setdefault(key, {"n": 0, "ms": 0,
                                 "first": int(l.get("start_ms", 0)),
                                 "last": int(l.get("end_ms", 0))})
        d["n"] += 1
        d["ms"] += max(0, int(l.get("end_ms", 0)) - int(l.get("start_ms", 0)))
        d["first"] = min(d["first"], int(l.get("start_ms", 0)))
        d["last"] = max(d["last"], int(l.get("end_ms", 0)))
    total = sum(d["ms"] for d in agg.values()) or 1
    rows = ["## 发言概览", ""]
    for key, d in sorted(agg.items(), key=lambda kv: -kv[1]["ms"]):
        rows.append(f"- {key}：{d['n']} 段，累计 {_fmt_dur(d['ms'])}，"
                    f"占 {d['ms'] * 100 // total}%（{_fmt_dur(d['first'])} 起，"
                    f"至 {_fmt_dur(d['last'])}）")
    ov = payload.get("overlaps") or []
    if ov:
        ms = sum(max(0, int(o.get("end_ms", 0)) - int(o.get("start_ms", 0))) for o in ov)
        rows.append(f"- 重叠语音（两人以上同时说话）：{len(ov)} 处，累计 {_fmt_dur(ms)}")
    return "\n".join(rows) + "\n\n"


def meeting_md(payload: dict, stem: str) -> str:
    """会议模式：会议原文——按时间顺序完整转写，每段带 [分:秒] 时间戳与说话人标签
    （pyannote 分离可靠后恢复标签；--names 提供人名映射时显示真实姓名）
    供对照审核与纪要提炼；正式纪要由会话内 AI 按 meeting-notes-expert 模板从原文提炼"""
    body = []
    for l in payload.get("lines") or []:
        name = l.get("spk_name") or f"说话人{l.get('spk')}"
        m, s = divmod(int(l.get("start_ms", 0)) // 1000, 60)
        text = (l.get("text") or "").strip()
        if text:
            body.append(f"[{m:02d}:{s:02d}] {name}：{text}")
    joined = "\n\n".join(body) if body else "（未检测到语音）"
    return f"# 会议原文：{stem}\n\n{speaker_summary(payload)}{joined}\n"


def render_docx(md_path: Path, docx_path: Path, stem_dir: Path, verbose: bool = False) -> bool:
    """md -> article-format 规范 docx；技能缺失时只留 md 并提示，返回 False"""
    skill = article_skill()
    if skill is None:
        # 未安装 article-format 技能：跳过规范排版，仅保留 md（并在日志说明）
        print("[提示] 未找到 article-format 技能，跳过 docx 排版（仅产出 md）；"
              "如需规范排版请设置 ARTICLE_SKILL_DIR", file=sys.stderr)
        return False
    base = stem_dir / "_article_base_tmp.docx"
    # 主标题由 md 内首个 `# ` 行自动识别，无需 --title；
    # article-format 输出受 ARTICLE_OUT_DIR 约束，且两脚本对相对路径解析基准不同——传入路径一律绝对化
    art_env = child_env(ARTICLE_OUT_DIR=str(stem_dir.resolve()))
    run_child([sys.executable, str(skill / "md2docx.py"),
               str(md_path.resolve()), str(base.resolve())], verbose=verbose, env=art_env)
    run_child([sys.executable, str(skill / "article_style.py"),
               str(base.resolve()), str(docx_path.resolve())], verbose=verbose, env=art_env)
    base.unlink(missing_ok=True)
    return True


def transcribe_and_write_article(stem_dir: Path, stem: str, force: bool,
                                 meeting: bool = False, opts: dict = None) -> str:
    """人声 wav -> qwen_asr 转写 -> md（转写文稿/会议原文）-> article-format 排版 docx
    opts 键：threshold / diarize_engine / hotwords / names / srt / engine / extra / verbose
    返回 'done' / 'skip' / 'novoice' / 'redocx'（md 已在，仅补排版）"""
    opts = opts or {}
    out_stem = f"会议原文-{stem}" if (meeting or opts.get("split_channels")) else f"转写文稿-{stem}"
    md_path = stem_dir / f"{out_stem}.md"
    docx_path = stem_dir / f"{out_stem}.docx"
    srt_path = stem_dir / f"{out_stem}.srt"
    vtt_path = stem_dir / f"{out_stem}.vtt"
    rttm_path = stem_dir / f"{out_stem}.rttm"
    # 要字幕但字幕还没出：不能直接 skip，交给下面命中缓存后走"只补对齐"通道。
    # 否则 --srt 在已处理素材上完全无声失效（首次运行后补字幕永远拿不到文件）。
    # srt 与 vtt 各自独立判定：只缺 vtt 时要补一次对齐，不能因为 srt 在就跳过。
    # rttm 不依赖对齐，补它的成本是纯文本写盘（缓存里有 lines 就够）。
    needs_srt = ((opts.get("srt") and not srt_path.exists())
                 or (opts.get("vtt") and not vtt_path.exists())
                 or (opts.get("rttm") and not rttm_path.exists()))
    if md_path.exists() and not force and not needs_srt:
        # md 已在：docx 齐备或排版技能缺失才算完成；只差 docx（首跑时技能未装）则只补排版，
        # 不重跑分离与转写——旧行为要到 --force 全量重跑，连 Demucs 一起白烧
        if docx_path.exists() or article_skill() is None:
            return "skip"
        print(f"[补排版] {stem}：md 已在，仅补 article-format docx")
        render_docx(md_path, docx_path, stem_dir, bool(opts.get("verbose")))
        return "redocx"
    if needs_srt and md_path.exists():
        wants = [n for n, w in (("字幕", opts.get("srt") or opts.get("vtt")),
                                ("说话人日志", opts.get("rttm"))) if w]
        print(f"[补文件] {stem}：md 已在，仅补{'/'.join(wants) or '附属产物'}"
              f"（复用缓存转写，不重跑分离与识别）")
    vocals = stem_dir / "人声.wav"
    # demucs 在静音段输出低电平伪影（非真静音），VAD 能量门限会失效切不出段——
    # 先过噪声门：低于 -40dB 归零、人声直通，恢复段间真静音（实测 VAD 1 段 → 2 段）
    vocals_asr = stem_dir / "人声_转写用.wav"
    if force or not vocals_asr.exists():
        print(f"[噪声门] {stem}：agate 清理分离伪影")
        run_child(["ffmpeg", "-y", "-i", str(vocals), "-af",
                   "agate=threshold=-40dB:ratio=99:attack=2:release=100",
                   str(vocals_asr)], env=child_env())

    # 纯补字幕：只跑对齐，不动 md/docx（转写内容没变，重写 md 会覆盖用户可能已手工修改的稿）
    if needs_srt and md_path.exists() and vocals_asr.exists():
        payload = call_asr(vocals_asr, opts, force=False)
        if opts.get("rttm") and not rttm_path.exists():
            n = write_rttm(payload.get("lines"), rttm_path, out_stem)
            print(f"[补文件] {stem}：RTTM 说话人日志已生成（{n} 行）")
        raw_srt = vocals_asr.with_suffix(".srt")
        if raw_srt.exists():
            raw_srt.replace(srt_path)
        if srt_path.exists():
            print(f"[补字幕] {stem}：字幕已生成 {srt_path.name}")
            return "resrt"
        if rttm_path.exists() and not opts.get("srt") and not opts.get("vtt"):
            return "resrt"
        print(f"[补字幕] {stem}：未能产出字幕（对齐器不可用或音频过短）", file=sys.stderr)
        return "skip"

    print(f"[转写] {stem}：说话人分离转写中（首次较慢；命中缓存直接复用）")
    # 降噪评估：粗估信噪比并给建议。不自动开降噪——实测白噪声场景无收益甚至更差，
    # 只在真实低信噪素材上才值得试，所以给提示而不是替用户决定。
    snr = estimate_snr_db(vocals_asr)
    if snr is not None and snr < 15:
        print(f"[提示] {stem}：估计信噪比约 {snr:.0f} dB（偏低），可试 --denoise；"
              f"但白噪声场景实测无收益，仅真实含 BGM/强噪素材值得试", file=sys.stderr)
    payload = None
    if opts.get("split_channels"):
        payload = split_channels_payload(stem_dir, force, opts)
        if payload is None:
            print("[提示] --split-channels 需要立体声素材，本次源是单声道，按普通模式处理",
                  file=sys.stderr)
    if payload is None:
        payload = call_asr(vocals_asr, opts, force)
    # 说话人结果自检：默认 campp 在语音段不足 2 段时会跳过聚类，把所有人归到说话人 0；
    # 这时给出可执行的下一步，而不是让用户拿着错误的单一说话人继续用
    spk_ids = {l.get("spk") for l in (payload.get("lines") or [])}
    if (len(spk_ids) <= 1 and not meeting and not opts.get("no_diarize")
            and opts.get("diarize_engine") != "pyannote" and not opts.get("names")):
        print("[提示] 本次只输出 1 个说话人：campp 声纹聚类在语音段不足时会跳过聚类。"
              "如需区分说话人，可加 --meeting 或 --diarize-engine pyannote 并用 --force 重跑",
              file=sys.stderr)
    use_labels = meeting or bool(opts.get("split_channels"))
    md_body = meeting_md(payload, stem) if use_labels else transcript_md(payload, stem)
    if not md_body:
        return "novoice"
    md_path.write_text(md_body, encoding="utf-8")
    # rttm 直接从 payload.lines 写（不转存子进程产物）：缓存命中时子进程没跑，
    # lines 却是每次都有的，一份数据源覆盖"首次运行"与"事后补文件"两条路径
    if opts.get("rttm"):
        n = write_rttm(payload.get("lines"), rttm_path, out_stem)
        if n:
            print(f"[说话人日志] {stem}：RTTM 已生成（{n} 行）→ {rttm_path.name}")
        else:
            print(f"[提示] {stem}：本次结果没有说话人标签，未写 RTTM", file=sys.stderr)
    # qwen_asr 产出的字幕转移到本次输出名（srt 与 vtt 各自独立搬运）
    for ext, want in ((".srt", opts.get("srt")), (".vtt", opts.get("vtt"))):
        if not want:
            continue
        raw = vocals_asr.with_suffix(ext)
        if raw.exists():
            raw.replace(stem_dir / f"{out_stem}{ext}")
    if render_docx(md_path, docx_path, stem_dir, bool(opts.get("verbose"))):
        print(f"[排版] {stem}：article-format 规范 docx 已生成")
    return "done"


def _is_under(path: Path, root: Path) -> bool:
    """path 是否位于 root 之下（含 root 本身）；解析失败时按字面比较"""
    try:
        p = path.resolve()
    except OSError:
        p = path
    return p == root or root in p.parents


def probe_channels(media: Path) -> int:
    """取音频流声道数；读不到返回 0"""
    out = run_output(["ffprobe", "-v", "error", "-select_streams", "a:0",
                      "-show_entries", "stream=channels", "-of", "csv=p=0", str(media)])
    try:
        return int(out.strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0


def split_channels_payload(stem_dir: Path, force: bool, opts: dict):
    """按声道分轨转写：左右声道各转一遍，把声道号直接当说话人编号再按时间轴合并。

    适用的素材形态是双人分声道录制（电话录音、双麦、部分会议系统）——这类素材本可
    靠声道区分说话人，而原先 qwen_asr 会先 mean(axis=1) 下混成单声道，白丢这层信息，
    只能退回声纹聚类。返回 None 表示源不是立体声，调用方应回退普通路径。
    """
    src = stem_dir / "人声_转写用.wav"
    if probe_channels(src) < 2:
        return None
    ch_paths = [stem_dir / "声道_0.wav", stem_dir / "声道_1.wav"]
    if force or not all(p.exists() for p in ch_paths):
        print("[分轨] 按声道拆成两路单声道，分别转写")
        run_child(["ffmpeg", "-y", "-i", str(src),
                   "-filter_complex", "channelsplit=channel_layout=stereo[l][r]",
                   "-map", "[l]", str(ch_paths[0]),
                   "-map", "[r]", str(ch_paths[1])], env=child_env())
    # 声道号就是说话人号：人名必须按声道号取，不能让子进程内 campp 聚类的
    # spk_name 透传过来——单声道分轨里说话人恒为 0，两路都会拿到 names[0]，
    # 结果「李四」的发言被标成「张三」（实测）
    name_map = _parse_names(opts.get("names"))
    lines = []
    for idx, p in enumerate(ch_paths):
        if (wav_rms_db(p) or -999) < -60:      # 整条声道接近静音，跳过
            continue
        payload = call_asr(p, opts, force, tag=f"ch{idx}")
        for ln in (payload.get("lines") or []):
            item = dict(ln)
            item["spk"] = idx                  # 声道号即说话人号
            item["spk_name"] = name_map.get(idx)
            lines.append(item)
    lines.sort(key=lambda x: x.get("start_ms", 0))
    return {"lines": lines}


def _parse_names(names) -> dict:
    """把 '0=张三,1=李四' 解析成 {0: '张三', 1: '李四'}；非法项忽略"""
    out = {}
    for part in str(names or "").split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        try:
            out[int(k.strip())] = v.strip()
        except ValueError:
            continue
    return out


def separate_one(media: Path, out_dir: Path, force: bool, do_asr: bool = True,
                 meeting: bool = False, opts: dict = None,
                 no_separate: bool = False) -> str:
    opts = opts or {}
    stem_dir = out_dir / media.stem
    is_audio = media.suffix.lower() in AUDIO_EXTS
    separated = (stem_dir / "人声.wav").exists() and (stem_dir / "背景音.wav").exists()
    # 分离模型纳入幂等判定：同一目录若换过模型，要按新模型重跑，不能直接复用旧产物
    demucs_model = opts.get("demucs_model") or "htdemucs"
    marker = stem_dir / ".demucs_model"
    same_model = marker.is_file() and marker.read_text(encoding="utf-8").strip() == demucs_model
    out_stem = (f"会议原文-{media.stem}" if (meeting or opts.get("split_channels"))
                else f"转写文稿-{media.stem}")
    md_exists = (stem_dir / f"{out_stem}.md").exists()
    docx_exists = (stem_dir / f"{out_stem}.docx").exists()
    # md 为完成标记；但只有 md 而缺 docx（首跑时未装 article-format）不算完成，
    # 交给下游走"仅补排版"分支，避免补装技能后只能 --force 全量重跑
    article = md_exists and (docx_exists or article_skill() is None)
    # 平铺模式：成稿已摊到素材同目录（md 已删）时同样视为完成，保证幂等
    if opts.get("flat") and (media.parent / f"{out_stem}.docx").exists():
        article = True
    if do_asr:
        all_done = article
    else:
        # --no-separate 只产出「人声.wav」（无背景音），完成判定要对齐实际产物
        all_done = (stem_dir / "人声.wav").exists() if no_separate else separated
    # 要求了附属产物而它还没出时不算完成：否则 --srt/--vtt/--rttm 在已处理素材上会完全失效
    # （补文件永远被 skip 挡住）。它们是独立产物，不该由 md 是否存在决定其生命周期。
    # 三种产物各自判定：只缺 vtt 或只缺 rttm 都要放行，不能因为 srt 在就判完成。
    if do_asr:
        for ext, want in ((".srt", opts.get("srt")), (".vtt", opts.get("vtt")),
                          (".rttm", opts.get("rttm"))):
            if not want:
                continue
            if (stem_dir / f"{out_stem}{ext}").exists():
                continue
            if opts.get("flat") and (media.parent / f"{out_stem}{ext}").exists():
                continue
            all_done = False
    if all_done and not force:
        if opts.get("clean"):
            removed = clean_intermediates(stem_dir)
            if removed:
                print(f"[清理] {media.stem}：移除中间件 {len(removed)} 个（--clean）")
        if opts.get("flat"):
            flatten_outputs(stem_dir, media, out_stem)
        return "skip"
    stem_dir.mkdir(parents=True, exist_ok=True)

    # 音轨预检：无音轨的文件要到拆轨阶段才失败，而 ffmpeg 的原始输出会把真原因埋在末尾，
    # 这里提前给出可理解的结论
    if not probe_audio_codec(media):
        what = ("音频文件无法解析音轨（可能损坏或不是有效音频）" if is_audio
                else "视频里没有音轨")
        raise AudioTrackMissing(what)

    # 1) FFmpeg 拆轨（仅视频输入；音频输入源即完整音频，跳过）
    if not is_audio:
        video_out = stem_dir / f"无声视频{media.suffix}"
        audio_ext = AUDIO_EXT.get(probe_audio_codec(media), ".mka")
        audio_out = stem_dir / f"完整音轨{audio_ext}"
        if force or not video_out.exists():
            print(f"[拆轨] {media.name}：提取视频流（无损 copy）")
            run_child(["ffmpeg", "-y", "-i", str(media), "-an", "-c:v", "copy",
                       str(video_out)], env=child_env())
        if force or not audio_out.exists():
            print(f"[拆轨] {media.name}：提取完整音轨（无损 copy）")
            run_child(["ffmpeg", "-y", "-i", str(media), "-vn", "-c:a", "copy",
                       str(audio_out)], env=child_env())

    # 2) Demucs 人声/背景音分离（--no-separate 时跳过：纯人声音频直接转码为人声.wav 供下游复用）
    if no_separate or opts.get("split_channels"):
        # 分轨模式必须跳过 Demucs：它按立体声混音做人声分离，会打破左右声道的独立性，
        # 而分轨的全部价值就在于两路声道各自干净
        vocals_out = stem_dir / "人声.wav"
        if force or not vocals_out.exists():
            why = "分轨模式跳过分离" if opts.get("split_channels") else "跳过人声分离"
            print(f"[转码] {media.name}：{why}，直接转 44.1kHz wav")
            # 分轨模式不能写 -ac 2：单声道源会被复制成两路相同内容，下游 probe 到
            # 2 声道就照转两遍，文本整体重复还凭空造出第二个说话人（实测）。
            # 保留原声道数，让 split_channels_payload 的声道判定看到真实布局。
            ch_args = [] if opts.get("split_channels") else ["-ac", "2"]
            run_child(["ffmpeg", "-y", "-i", str(media)] + ch_args + ["-ar", "44100",
                       str(vocals_out)], env=child_env())
    elif force or not separated or not same_model:
        # 分离前对原始素材粗估信噪比（分位法；校准：纯人声 25.9 dB、人声+0.15 粉噪 14.6 dB），
        # 明显纯净时提示可 --no-separate 省 Demucs。只提示不自动跳——判据是相对估计，
        # 误判的代价是转写变差，宁多跑一次分离（与已证伪的「人声/背景音能量差」判据不同）。
        probe = decode_16k_mono_tmp(media)
        if probe is not None:
            snr0 = estimate_snr_db(probe)
            probe.unlink(missing_ok=True)
            if snr0 is not None and snr0 >= 25:
                print(f"[提示] {media.name}：原始素材估计信噪比约 {snr0:.0f} dB（较纯净），"
                      f"可加 --no-separate 跳过 Demucs 提速")
        tmp = out_dir / "_demucs_tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"[分离] {media.name}：Demucs({demucs_model}) 人声/背景音分离（GPU，耗时随时长增长）")
        # demucs 的进度条有实际参考价值，始终实时透传（不捕获）
        # HF_HUB_OFFLINE=1：demucs 默认先试 HuggingFace 元数据（不可达时重试约 30 秒才失败回落
        # 官方源）；离线模式使其立即失败并直接走 dl.fbaipublicfiles.com 兜底，省等待
        run_child(["demucs", "-n", demucs_model, "--two-stems=vocals",
                   "-o", str(tmp), str(media)], verbose=True,
                  env=child_env(HF_HUB_OFFLINE="1"))
        # demucs 固定输出 vocals.wav / no_vocals.wav，移入输出目录时改成自说明中文名
        rename = {"vocals.wav": "人声.wav", "no_vocals.wav": "背景音.wav"}
        # demucs 把模型名写进输出子目录，单模型与 bag 都取实际目录名，避免硬编码
        produced = [d for d in (tmp.iterdir()) if d.is_dir()] if tmp.is_dir() else []
        src_dir = produced[0] if produced else (tmp / demucs_model)
        for f in (src_dir / media.stem).glob("*.wav"):
            shutil.move(str(f), str(stem_dir / rename.get(f.name, f.name)))
        shutil.rmtree(tmp, ignore_errors=True)
        marker.write_text(demucs_model, encoding="utf-8")
        # 曾想用「人声与背景音的能量差」自动判断素材本来就干净（好提示用户跳过分离），
        # 对照实验证明不可行：纯人声 TTS 差值 15.1 dB、人声+440Hz 背景乐 12.7 dB，
        # 两者只差 2.4 dB，该判据区分不出有没有背景，故不实现（避免给出误导性建议）。
        # 需要跳过分离时仍由用户手动加 --no-separate。

    # 3) 人声转文字 + 文章/纪要排版（--no-asr 可跳过）
    if do_asr:
        result = transcribe_and_write_article(stem_dir, media.stem, force, meeting, opts)
        # resrt=只补字幕：不触发 --flat 的目录清理（md/docx 早就摊出去了或本就在原位），
        # 但 --clean 该照做，否则补字幕顺带留下的中间件会一直占盘
        if opts.get("clean") and result in ("done", "redocx", "resrt"):
            removed = clean_intermediates(stem_dir)
            if removed:
                print(f"[清理] {media.stem}：移除中间件 {len(removed)} 个（--clean）")
        if opts.get("flat") and result in ("done", "redocx"):
            flatten_outputs(stem_dir, media, out_stem)
        return result
    return "done"


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    # stderr 同样锁 UTF-8：提示类信息（说话人、显存、排版技能缺失）此前走 GBK，
    # 在 UTF-8 控制台下会显示成乱码
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="音视频全自动批量（拆轨 + 人声分离 + 说话人分离转写 → 转写文稿 / 结构化总结）")
    ap.add_argument("input", help="视频/音频文件或包含它们的文件夹")
    ap.add_argument("-o", "--output", help="输出目录（默认 <输入>/separated_out）")
    ap.add_argument("--force", action="store_true", help="已处理过的也重跑")
    ap.add_argument("--no-asr", action="store_true", help="只拆轨+人声分离，不转写不生成文稿")
    ap.add_argument("--no-separate", action="store_true",
                    help="跳过人声/背景音分离（纯人声音频适用，直接进转写）")
    ap.add_argument("--demucs-model", default="htdemucs",
                    choices=["htdemucs", "htdemucs_ft", "mdx_extra", "mdx_extra_q",
                             "hdemucs_mmi", "htdemucs_6s"],
                    help="人声分离模型：htdemucs 默认；htdemucs_ft 是官方微调版，质量更好但慢约 4 倍；"
                         "mdx_extra 更强但可能引入伪影。换模型会按新模型重跑分离")
    ap.add_argument("--meeting", action="store_true",
                    help="产出会议原文（带时间戳；默认产出转写文稿。转写恒带说话人分离）")
    ap.add_argument("--no-diarize", action="store_true",
                    help="单人素材跳过说话人分离直接整段转写（更快；云端 message 引擎将走整文件一次调用）；"
                         "与 --meeting 互斥")
    ap.add_argument("--diarize-engine", choices=["campp", "pyannote", "auto"], default="auto",
                    help="说话人分离引擎：auto=会议自动用 pyannote（重叠语音更准），其余用 campp；"
                         "campp=轻量声纹聚类；pyannote=分割模型（需已装）")
    ap.add_argument("--num-speakers", type=int, default=None,
                    help="已知说话人数（pyannote 引擎）：精确人数最准，如两人对谈就填 2")
    ap.add_argument("--min-speakers", type=int, default=None,
                    help="说话人数下限（pyannote 引擎），配合 --max-speakers 挡住过度切分")
    ap.add_argument("--max-speakers", type=int, default=None,
                    help="说话人数上限（pyannote 引擎）；人数不确定时用它，比让模型自由推断稳")
    ap.add_argument("--pyannote-chunk-s", type=int, default=None,
                    help="pyannote 单块时长上限秒（默认 900，0=不分块）：长素材分块显著降内存峰值")
    ap.add_argument("--pyannote-overlap-s", type=int, default=None,
                    help="pyannote 块间重叠秒（默认 30）：供跨块说话人标签映射，太小会多认出说话人")
    ap.add_argument("--align-engine", choices=["auto", "fa-zh", "native"], default="auto",
                    help="字幕对齐后端：auto=有 Qwen3-ForcedAligner 就用、否则 fa-zh（默认）；"
                         "fa-zh=中文专用；native=多语言（需先下载 Qwen3-ForcedAligner-0.6B）")
    ap.add_argument("--itn-backend", choices=["auto", "wtp", "legacy"], default="auto",
                    help="ITN 后端：auto=装了 WeTextProcessing 就用、否则自写规则（默认）；"
                         "wtp=强制 WeTextProcessing；legacy=自写零依赖规则")
    ap.add_argument("--threshold", type=float, default=None,
                    help="声纹聚类阈值（仅 campp 引擎，默认 0.75）：单人素材被误切成多人时调低试 0.6~0.65；"
                         "配合 --force 生效，不同配置存独立缓存")
    ap.add_argument("--hotwords", default=None,
                    help="热词/上下文（逗号分隔），偏置识别引擎提升专名准确率，如 "
                         "'胚布,白配检测,库龄机制'；未指定时自动加载根目录 hotwords.txt（每行一个词）；"
                         "变更热词后自动用新缓存重转")
    ap.add_argument("--names", default=None,
                    help="说话人改名（如 '0=赵总,1=张会计'）——会议原文的说话人标签将显示真实姓名")
    ap.add_argument("--srt", action="store_true",
                    help="额外产出 .srt 字幕（同名，含字级时间戳；加载 fa-zh 对齐模型）")
    ap.add_argument("--vtt", action="store_true",
                    help="额外产出 .vtt 字幕（WebVTT，HTML5 video 的 track 与视频平台直接可用）；"
                         "与 --srt 共用一次对齐，可同时开")
    ap.add_argument("--rttm", action="store_true",
                    help="额外产出 .rttm 说话人日志（RTTM 标准格式：pyannote.metrics / meeteval / "
                         "dscore 可直接读，外部复核分离结果不用再写转换脚本）")
    ap.add_argument("--min-dur", type=int, default=None,
                    help="字幕最短显示时长 ms（默认 700；语速快时避免一闪而过，0=不补）")
    ap.add_argument("--max-cps", type=float, default=None,
                    help="字幕最大阅读速度 字/秒（默认 12；超出按字数拉长显示，0=只按最小时长兜底）")
    ap.add_argument("--asr-engine", choices=["qwen", "aed", "auto", "cloud"], default="qwen",
                    help="识别引擎：qwen=Qwen3-ASR-1.7B（默认，多语言）；aed=FireRedASR2-AED（中/英/粤更准）；"
                         "auto=按 --language 路由（中/英/粤走 aed，其余走 qwen）；"
                         "cloud=阿里云百炼（默认 qwen-audio-3.1-asr-message，需 DASHSCOPE_API_KEY，免显存；"
                         "换子模型加 --asr-extra \"--cloud-model omni|filetrans\"）")
    ap.add_argument("--itn", action="store_true",
                    help="中文逆文本正则化：三百二十万元->320万元、百分之八十->80%%，"
                         "二零二六年十月十五日->2026年10月15日（自写规则，零依赖）")
    ap.add_argument("--split-channels", action="store_true",
                    help="按声道分轨转写：双人分声道录制（电话/双麦）把左右声道当两个说话人，"
                         "输出带说话人标签的文稿；会自动跳过 Demucs 与声纹聚类，非立体声回退普通模式")
    ap.add_argument("--emotion", action="store_true",
                    help="emotion2vec 整段情绪分析（8 类情绪写入结果 json 的 emotions 字段；不做笑声/掌声事件检测）")
    ap.add_argument("--speaker-db", nargs="?", const="auto", default=None,
                    help="声纹库：跨文件复用说话人身份（campp 与 pyannote 两条路径都支持）。裸写或 "
                         "auto=输出目录 speaker_db.json（有则复用无则建库、命中自动更新）；也可给显式 JSON 路径")
    ap.add_argument("--replace", default=None,
                    help="专名/错词确定性纠错（如 '小蜜=>小米,开饭时间=>开放时间'）——"
                         "比 --hotwords 的 prompt 偏置可靠；变更后自动用新缓存重转")
    ap.add_argument("--denoise", action="store_true",
                    help="转写前跑 ZipEnhancer 降噪（仅真实含 BGM/强噪素材；干净素材不加更好）")
    ap.add_argument("--vad", choices=["auto", "fsmn", "firered"], default="auto",
                    help="VAD 切段后端：auto=按用途自动选（分离链路 fsmn、--no-diarize 长音频 firered，"
                         "装了 FireRedVAD 才换）；fsmn=FunASR 内置；firered=FireRedVAD（误报更低）")
    ap.add_argument("--language", default=None,
                    help="强制语言（zh/en/yue/ja 等代码），默认自动检测")
    ap.add_argument("--max-line", type=int, default=None,
                    help="单条字幕最大字数（默认 28，配合 --srt 使用）")
    ap.add_argument("--merge-gap", type=int, default=None,
                    help="相邻同说话人合并间隔 ms（默认 800，0 关闭）")
    ap.add_argument("--asr-extra", default=None,
                    help="其余 qwen_asr 参数原样透传（如 '--fuzzy --min-seg 300'）；"
                         "变更后自动用新缓存重转")
    ap.add_argument("--verbose", action="store_true",
                    help="实时透传各子进程输出（排查卡顿与失败原因时用）")
    ap.add_argument("--clean", action="store_true",
                    help="完成后删除中间件（人声/背景音/转写用 wav 与转写缓存 json），"
                         "只留成稿 md/docx/srt；拆轨产物保留")
    ap.add_argument("--flat", action="store_true",
                    help="平铺输出：成稿直接放到素材同目录，并删除工作子目录"
                         "（含中间件、md 底稿、.demucs_model 标记与拆轨产物）")
    ap.add_argument("--log", action="store_true",
                    help="把本次运行输出追加写入 <输出目录>/run.log，便于批量跑完回溯")
    ap.add_argument("--replace-file", nargs="?", const="auto", default=None,
                    help="纠错词典文件（每行 '错=>对'）；未指定时自动加载根目录 replace_dict.txt（存在即加载）；"
                         "裸写或 auto=根目录 replace_dict.txt")
    ap.add_argument("--replace-save", nargs="?", const="auto", default=None,
                    help="把本次纠错条目合并写回词典（裸写或 auto=根目录 replace_dict.txt，键同新值覆盖）")
    args = ap.parse_args()
    if args.meeting and args.no_diarize:
        sys.exit("--meeting 需要说话人分离产出说话人标签，不能与 --no-diarize 同用")

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"输入不存在：{src}")
    out_dir = Path(args.output) if args.output else (
        src.parent / "separated_out" if src.is_file() else src / "separated_out")
    if src.is_file():
        if src.suffix.lower() not in MEDIA_EXTS:
            sys.exit(f"不支持的文件类型：{src.suffix}")
        medias = [src]
    else:
        # 排除输出目录：默认输出就落在输入目录下的 separated_out，其拆轨产物
        # （完整音轨.m4a / 无声视频.mp4）与分离中间件同样是 MEDIA_EXTS，不排除的话
        # 第二次运行会把它们当新素材再跑一遍 Demucs，既白烧算力又互相覆盖同名 stem
        try:
            out_abs = out_dir.resolve()
        except OSError:
            out_abs = out_dir
        medias = sorted(
            p for p in src.rglob("*")
            if p.suffix.lower() in MEDIA_EXTS and not _is_under(p, out_abs))
    if not medias:
        sys.exit("没找到视频/音频文件")

    # auto：会议模式走 pyannote（重叠语音更准），其余走 campp（轻量）
    diarize_engine = ("pyannote" if args.meeting else None) \
        if args.diarize_engine == "auto" else args.diarize_engine
    # auto 持久化文件：声纹库仍放输出目录（跨素材共享）；纠错词典与热词表改放管线根目录固定位置
    # ——平铺模式（--flat）会删除整个工作子目录，词典放输出目录会被一并删掉、积累失效。
    speaker_db_arg = str(out_dir / "speaker_db.json") if args.speaker_db == "auto" else args.speaker_db
    default_dict = SCRIPT_DIR / "replace_dict.txt"
    if args.replace_file is None:
        # 未显式指定时：固定词典存在则自动加载（无需每次带参数）
        replace_file_arg = str(default_dict) if default_dict.is_file() else None
    elif args.replace_file == "auto":
        replace_file_arg = str(default_dict)
    else:
        replace_file_arg = args.replace_file
    replace_save_arg = str(default_dict) if args.replace_save == "auto" else args.replace_save
    # 热词表：未显式给出时读固定位置 hotwords.txt（每行一个词，# 注释）
    if args.hotwords is None:
        hot_file = SCRIPT_DIR / "hotwords.txt"
        if hot_file.is_file():
            words = [ln.strip() for ln in hot_file.read_text(encoding="utf-8").splitlines()
                     if ln.strip() and not ln.startswith("#")]
            if words:
                args.hotwords = ",".join(words)
    # 纠错词典内容哈希进缓存指纹：词典更新后旧缓存不复用，保证修正结果一致
    dict_hash = None
    if replace_file_arg:
        try:
            dict_hash = hashlib.sha256(Path(replace_file_arg).read_bytes()).hexdigest()[:8]
        except OSError:
            dict_hash = None
    # 额外 ASR 参数：显式项 + --asr-extra 合并，统一进命令行与缓存键
    # VAD 后端先在这里解析成确定名字：子进程的 auto 会按它自己的 --diarize 判断，
    # 而管线这里还知道 no_diarize，两处判断口径必须统一在管线这一侧，否则
    # "子进程实际用 firered、缓存键却记 auto"会让换后端后继续复用旧结果。
    vad_arg = resolve_vad_arg(args.vad, diarize=not args.no_diarize)
    extra = []
    for flag, val in (("--replace", args.replace), ("--replace-file", replace_file_arg),
                      ("--replace-save", replace_save_arg),
                      ("--language", args.language), ("--speaker-db", speaker_db_arg),
                      ("--max-line", args.max_line), ("--merge-gap", args.merge_gap),
                      ("--min-dur", args.min_dur), ("--max-cps", args.max_cps),
                      ("--vad", vad_arg),
                      # 说话人数只影响分离结果，必须进缓存键（不同人数=不同 lines）
                      ("--num-speakers", args.num_speakers),
                      ("--min-speakers", args.min_speakers),
                      ("--max-speakers", args.max_speakers),
                      ("--pyannote-chunk-s", args.pyannote_chunk_s),
                      ("--pyannote-overlap-s", args.pyannote_overlap_s)):
        if val is not None:
            extra += [flag, str(val)]
    if args.speaker_db == "auto":
        extra.append("--speaker-db-save")   # auto=有库复用无库建库，命中/新增都更新库
    if args.denoise:
        extra.append("--denoise")
    if args.itn:
        extra.append("--itn")
        if args.itn_backend and args.itn_backend != "auto":
            extra += ["--itn-backend", args.itn_backend]
    if args.emotion:
        extra.append("--emotion")
    if args.align_engine and args.align_engine != "auto":
        # 对齐后端只影响字幕，不进缓存键（否则换后端要重转），但补字幕通道要用
        extra += ["--align-engine", args.align_engine]
    if args.asr_extra:
        extra += split_asr_extra(args.asr_extra)
    for bad in RESERVED_ASR_ARGS:
        if bad in extra:
            sys.exit(f"--asr-extra 不允许传入管线保留参数：{bad}")
    opts = {"threshold": args.threshold, "diarize_engine": diarize_engine,
            "hotwords": args.hotwords, "names": args.names, "srt": args.srt, "vtt": args.vtt,
            "engine": args.asr_engine if args.asr_engine != "qwen" else None,
            "extra": extra, "verbose": args.verbose, "clean": args.clean,
            "flat": args.flat, "dict_hash": dict_hash,
            "demucs_model": args.demucs_model, "split_channels": args.split_channels,
            "no_diarize": args.no_diarize,
            # rttm 只由主管线写（不进 ASR 子进程：它的产物会在工作目录里重复一份）
            "rttm": args.rttm,
            # 供 _align_srt_only 补字幕时透传（不进缓存键）
            "align_engine": args.align_engine, "language": args.language,
            "max_line": args.max_line}

    # 日志落盘：终端与 run.log 双写，批量跑完还能回溯
    log_path = None
    log_handle = None
    if args.log:
        out_dir.mkdir(parents=True, exist_ok=True)
        log_path = out_dir / "run.log"
        log_handle = log_path.open("a", encoding="utf-8")
        sys.stdout = _Tee(sys.stdout, log_handle)
        sys.stderr = _Tee(sys.stderr, log_handle)
        print(f"\n===== 运行开始 {time.strftime('%Y-%m-%d %H:%M:%S')}，素材 {len(medias)} 个 =====")

    warn_low_vram()

    total = len(medias)
    run_t0 = time.time()
    ok = skip = fail = novoice = redocx = resrt = invalid = 0
    for idx, v in enumerate(medias, 1):
        item_t0 = time.time()
        print(f"\n[{idx}/{total}] {v.name}")
        try:
            r = separate_one(v, out_dir, args.force, do_asr=not args.no_asr,
                             meeting=args.meeting, opts=opts,
                             no_separate=args.no_separate)
            if r == "skip":
                skip += 1
                print(f"[跳过] {v.name}（已处理过，--force 可重跑）")
            elif r == "novoice":
                novoice += 1
                print(f"[无人声] {v.name}（转写为空，仅出拆轨+分离件）")
            elif r == "redocx":
                redocx += 1
                print(f"[补排版] {v.name} → {out_dir / v.stem}")
            elif r == "resrt":
                resrt += 1
                print(f"[补字幕] {v.name} → {out_dir / v.stem}")
            else:
                ok += 1
                print(f"[完成] {v.name} → {out_dir / v.stem}")
        except AudioTrackMissing as e:
            # 素材本身没有音轨（或音频损坏）：不算处理失败，单独归类
            invalid += 1
            print(f"[无音轨] {v.name}：{e}")
        except subprocess.CalledProcessError as e:
            fail += 1
            # 打印失败尾部 20 行：原先只留最后一行，真正原因往往在上面几行；
            # 输出已按 UTF-8 解码，不会因 GBK 错位丢成空
            detail = (e.stderr or e.stdout or "").strip()
            if detail:
                tail = "\n".join(detail.splitlines()[-20:])
                print(f"[失败] {v.name}：\n{tail}")
            else:
                print(f"[失败] {v.name}：{e}")
        except KeyboardInterrupt:
            print("\n用户中断"); break
        except Exception as e:
            # 兜底：缺 ffmpeg/demucs（FileNotFoundError）、缓存 JSON 损坏等不再中断整批
            fail += 1
            print(f"[失败] {v.name}：{type(e).__name__}: {e}")
        print(f"    用时 {time.time() - item_t0:.1f} 秒（{idx}/{total}）")

    elapsed = time.time() - run_t0
    print(f"\n汇总：完成 {ok} / 补排版 {redocx} / 补字幕 {resrt} / 无人声 {novoice} / 跳过 {skip} / 失败 {fail}"
          + (f" / 无音轨 {invalid}" if invalid else "")
          + f"，输出目录：{out_dir}")
    if total:
        print(f"耗时：总计 {elapsed:.1f} 秒，平均 {elapsed / total:.1f} 秒/个（{total} 个素材）")

    if log_handle is not None:
        print(f"===== 运行结束 {time.strftime('%Y-%m-%d %H:%M:%S')} =====")
        # 先恢复原始流再关闭文件，避免 close 后仍有输出落在已关闭的句柄上
        sys.stdout = sys.stdout.streams[0]
        sys.stderr = sys.stderr.streams[0]
        log_handle.close()
        print(f"[日志] 已写入 {log_path}")


if __name__ == "__main__":
    main()
