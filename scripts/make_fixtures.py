#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合成带标注的测试素材（回归基线用）。

为什么需要：合成 TTS 素材只能验证"管道不炸"，验不出"转得准不准"。但真实会议录音涉及
业务内容、体积大、版权不明，不适合进仓库。折中做法是把"造素材"变成可复现的脚本：
素材本身不入库（gitignore），拿料方式与人工标注一并固化，谁跑都能得到同一份基线。

产物（默认写 .tmp/fixtures/）：
  dialog.wav        双人交替对话，带 1.2s 静音间隔，用于验说话人分离
  dialog.wav.ref.json   对应的 DER 标注（真实说话人分段）
  overlap.wav       双人抢话（重叠语音），用于验 overlaps 字段
  single.wav        单人连续语音，用于验 CER 与最短路径
  quiet.wav         低信噪（叠白噪），用于验 VAD 与降噪提示

各素材都用 Windows TTS 生成，两个说话人靠不同系统音色 + 音高偏移拉开声纹距离
（实测跨音色相似度 0.30，低于 0.5 的判定阈值）。

用法：
  python scripts/make_fixtures.py                 # 生成全部到 .tmp/fixtures/
  python scripts/make_fixtures.py --out DIR       # 指定输出目录
  python scripts/make_fixtures.py --only dialog   # 只生成一类
  python scripts/make_fixtures.py --check         # 只检查环境依赖是否齐备

依赖：Windows + PowerShell（System.Speech）+ ffmpeg。非 Windows 环境给出提示并跳过，
不报错退出——CI 里 Linux runner 跑单测即可，fixture 生成属本机/本地回归环节。
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

# 说话人音色配方：音色名 + 语速 + 变调系数。
# 只用中文音色 Huihui：本机另一音色 Zira 是 en-US，念中文直接产出 0 帧（实测）。
# 两个说话人靠"语速 + 变调"拉开：pitch 0.78 让声纹落到 0.15 量级，
# 远低于 0.5 的同人判定阈值，足够让 pyannote 分出两人。
SPEAKERS = {
    "A": {"voice": "Microsoft Huihui Desktop", "rate": 0, "pitch": 1.0},
    "B": {"voice": "Microsoft Huihui Desktop", "rate": -1, "pitch": 0.78},
}

# 对话脚本：(说话人, 文本)。文本用日常工作会议口吻，含数字与专名，顺带能验 ITN 与纠错。
DIALOG = [
    ("A", "第一季度的销售数据已经整理完毕，整体比去年同期增长百分之十五。"),
    ("B", "好的，那华南和华北的情况呢，有没有出现下滑的迹象。"),
    ("A", "其中华东区域的贡献最大，增长达到百分之二十三。"),
    ("B", "另外库存周转率这一块，我也需要重点关注一下。"),
]

OVERLAP_LINES = [
    ("A", "这个方案我建议先在小范围试点，观察两周数据再决定是否全面推。"),
    ("B", "我同意试点，但时间上可能要多留一周，供应链那边反馈准备需要时间。"),
]

SINGLE_TEXT = "今天讨论三件事，第一是季度目标，第二是人员安排，第三是预算调整。"
QUIET_TEXT = "现在测试的是低信噪比场景下的识别稳定性，请仔细听清每一个数字。"

GAP_S = 1.2          # 对话段间静音，给 pyannote 明确的换人边界
SR = 16000


def need(cond, msg):
    if not cond:
        sys.exit(f"[环境不足] {msg}")


def check_env():
    """检查生成素材所需的外部工具，返回 (ok, 原因)"""
    if sys.platform != "win32":
        return False, "System.Speech 仅 Windows 可用"
    if not shutil.which("ffmpeg"):
        return False, "ffmpeg 不在 PATH"
    if not shutil.which("powershell") and not shutil.which("pwsh"):
        return False, "找不到 powershell"
    return True, ""


def ps_exe():
    return shutil.which("pwsh") or shutil.which("powershell")


def tts(text, voice, rate, out_wav: Path):
    """调 Windows TTS 合成单段语音。脚本用 utf-8-sig（PowerShell 5.1 读中文必需 BOM）。"""
    ps = out_wav.with_suffix(".ps1")
    ps.write_text(
        "Add-Type -AssemblyName System.Speech\n"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
        f"$s.SelectVoice('{voice}')\n"
        f"$s.Rate = {rate}\n"
        f"$s.SetOutputToWaveFile('{out_wav}')\n"
        f"$s.Speak('{text}')\n"
        "$s.Dispose()\n", encoding="utf-8-sig")
    subprocess.run([ps_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps)],
                   check=True, capture_output=True)
    ps.unlink(missing_ok=True)
    need(out_wav.exists() and out_wav.stat().st_size > 0, f"TTS 未产出 {out_wav.name}")
    # 空音频也要拦：音量或音色不支持时 PowerShell 不报错但写出 0 帧 wav，
    # 静默放过会让后续标注全变成零长度段（实测 Zira 念中文即如此）
    need(dur(out_wav) > 0.05, f"TTS 产出为空（{out_wav.name}，音色可能不支持该语言）")


def pitch_shift(src: Path, dst: Path, factor: float):
    """变调不改时长：rubberband 保持语速，只移动音高，声纹差异更明显"""
    if abs(factor - 1.0) < 1e-6:
        shutil.copyfile(src, dst)
        return
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(src), "-af", f"rubberband=pitch={factor}",
                    "-ar", str(SR), "-ac", "1", str(dst)], check=True, capture_output=True)


def dur(wav: Path) -> float:
    """读音频时长（秒）。

    不用 ffprobe 的 format=duration：rubberband 输出的流式 wav 常缺 header 里的
    时长字段，ffprobe 会返回 "N/A"。按采样数除以采样率算，对任意 wav 都成立。
    """
    import wave
    with wave.open(str(wav), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def concat(parts, out: Path, gap_s: float = GAP_S):
    """把多段 wav 用静音间隔串起来，统一重采样到 16k 单声道"""
    if not parts:
        sys.exit("没有可拼接的片段")
    inputs, filters, chain = [], [], []
    t = 0.0
    for i, p in enumerate(parts):
        inputs += ["-i", str(p)]
        filters.append(f"[{i}:a]aresample={SR},aformat=channel_layouts=mono[s{i}]")
        chain.append(f"[s{i}]")
        t += dur(p)
        if i < len(parts) - 1 and gap_s > 0:
            filters.append(f"anullsrc=r={SR}:cl=mono:d={gap_s}[g{i}]")
            chain.append(f"[g{i}]")
            t += gap_s
    fc = ";".join(filters) + ";" + "".join(chain) + f"concat=n={len(chain)}:v=0:a=1[out]"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + inputs +
                   ["-filter_complex", fc, "-map", "[out]", "-ar", str(SR), "-ac", "1", str(out)],
                   check=True, capture_output=True)
    return t


def build_dialog(outdir: Path):
    """双人交替对话 + DER 标注。标注按实际语速向前偏移，不写死时间。"""
    work = outdir / "_parts"
    work.mkdir(parents=True, exist_ok=True)
    segs, speakers = [], []
    t = 0.0
    for i, (spk, text) in enumerate(DIALOG):
        cfg = SPEAKERS[spk]
        raw = work / f"d{i}_raw.wav"
        tts(text, cfg["voice"], cfg["rate"], raw)
        shifted = work / f"d{i}.wav"
        pitch_shift(raw, shifted, cfg["pitch"])
        segs.append(shifted)
        d = dur(shifted)
        speakers.append({"start_ms": int(t * 1000), "end_ms": int((t + d) * 1000), "spk": spk})
        t += d + (GAP_S if i < len(DIALOG) - 1 else 0)
    wav = outdir / "dialog.wav"
    concat(segs, wav)
    ref = outdir / "dialog.wav.ref.json"
    ref.write_text(json.dumps({"speakers": speakers}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    return wav, ref


def build_overlap(outdir: Path):
    """抢话素材：两句同时起播，制造真实重叠语音，用于验 overlaps 字段。
    没有标注——重叠段的归属本身无唯一正解，这里只验"能被检测出来"。"""
    work = outdir / "_parts"
    work.mkdir(parents=True, exist_ok=True)
    clips = []
    for i, (spk, text) in enumerate(OVERLAP_LINES):
        cfg = SPEAKERS[spk]
        raw = work / f"o{i}_raw.wav"
        tts(text, cfg["voice"], cfg["rate"], raw)
        shifted = work / f"o{i}.wav"
        pitch_shift(raw, shifted, cfg["pitch"])
        clips.append(shifted)
    # 第二句延迟 1.5s 起播，与第一句尾部交叠；再补足尾部静音让交叠区完整
    fc = (f"[0:a]aresample={SR},aformat=channel_layouts=mono[a0];"
          f"[1:a]aresample={SR},aformat=channel_layouts=mono,adelay=1500|1500[a1];"
          f"[a0][a1]amix=inputs=2:duration=longest:normalize=0[out]")
    wav = outdir / "overlap.wav"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(clips[0]), "-i", str(clips[1]),
                    "-filter_complex", fc, "-map", "[out]",
                    "-ar", str(SR), "-ac", "1", str(wav)], check=True, capture_output=True)
    return wav


def build_single(outdir: Path):
    cfg = SPEAKERS["A"]
    raw = outdir / "single.wav"
    tts(SINGLE_TEXT, cfg["voice"], cfg["rate"], raw)
    return raw


def build_quiet(outdir: Path):
    """低信噪素材：叠白噪压到约 8dB SNR，触发降噪提示分支。
    anoisesrc 必须给有限时长（dur=0 无效会直接报错），按语音实际时长传。
    amix 用 duration=first 保证输出长度等于语音长度，不被噪声拉长。"""
    cfg = SPEAKERS["A"]
    raw = outdir / "_parts" / "quiet_raw.wav"
    raw.parent.mkdir(parents=True, exist_ok=True)
    tts(QUIET_TEXT, cfg["voice"], cfg["rate"], raw)
    wav = outdir / "quiet.wav"
    fc = (f"[0:a]aresample={SR},aformat=channel_layouts=mono[sp];"
          f"anoisesrc=d={dur(raw):.3f}:c=white:r={SR}:a=0.06[nz];"
          f"[sp][nz]amix=inputs=2:duration=first:normalize=0[out]")
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(raw), "-filter_complex", fc, "-map", "[out]",
                    "-ar", str(SR), "-ac", "1", str(wav)], check=True, capture_output=True)
    return wav


BUILDERS = {
    "dialog": build_dialog,
    "overlap": build_overlap,
    "single": build_single,
    "quiet": build_quiet,
}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="合成带标注的回归测试素材")
    ap.add_argument("--out", default=".tmp/fixtures", help="输出目录（默认 .tmp/fixtures）")
    ap.add_argument("--only", choices=sorted(BUILDERS), help="只生成某一类")
    ap.add_argument("--check", action="store_true", help="只检查环境，不生成")
    args = ap.parse_args()

    ok, why = check_env()
    if args.check or not ok:
        print(f"[环境] {'可用' if ok else '不可用：' + why}")
        return 0 if ok else 0   # 环境不够不算失败：非 Windows / 无 ffmpeg 时跳过即可

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    targets = [args.only] if args.only else list(BUILDERS)
    made = []
    for name in targets:
        r = BUILDERS[name](outdir)
        files = r if isinstance(r, tuple) else (r,)
        for f in files:
            detail = f"{dur(f):.1f}s" if f.suffix == ".wav" else "-"
            print(f"[生成] {f.relative_to(outdir)}  {f.stat().st_size / 1024:.0f} KB  {detail}")
            made.append(f)
    print(f"\n完成：{len(made)} 个文件 → {outdir}")
    print("提示：素材是可复现产物，已 gitignore，不要提交音频本体；标注 ref.json 随素材一起生成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
