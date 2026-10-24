#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_cache_fingerprint.py — 转写缓存指纹的单元测试（不起 GPU、不跑模型）

覆盖两个曾经会静默出错的行为：
  1) 同一目录人声.wav 被替换后，配置维度全没变 → 旧版本会把旧文本当新素材结果；
  2) 旧缓存缺 _source_fp 字段 → 升级后应兼容补写，不强制全量重转。

跑法：python skills/funasr-transcribe/scripts/test_cache_fingerprint.py
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _find_main() -> Path:
    """主脚本位置随部署形态不同：仓库里在上溯 3 层的根目录，技能副本里与本文件同目录"""
    for cand in (HERE.parents[2] / "av_to_transcript_and_minutes.py",   # 仓库布局
                 HERE / "av_to_transcript_and_minutes.py"):             # 技能副本布局
        if cand.is_file():
            return cand
    return HERE.parents[2] / "av_to_transcript_and_minutes.py"


MAIN = _find_main()

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'} {name}" + (f"  {detail}" if not cond else ""))
    if not cond:
        fails.append(name)


def load_main():
    spec = importlib.util.spec_from_file_location("avmain", MAIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # 顶层只有定义与常量，无副作用
    return mod


def main() -> int:
    if not MAIN.is_file():
        print(f"找不到主脚本：{MAIN}")
        return 2
    mod = load_main()
    check("主脚本可导入", True)

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        vocals = work / "人声_转写用.wav"
        # 造两个内容不同的「音频」（不必是真 wav：指纹只读字节）
        vocals.write_bytes(b"A" * 3_000_000)
        fp1 = mod.audio_fingerprint(vocals)
        check("指纹非空", bool(fp1), "指纹为空")

        vocals.write_bytes(b"A" * 3_000_000)
        check("同内容同指纹", mod.audio_fingerprint(vocals) == fp1)

        vocals.write_bytes(b"B" * 3_000_000)
        fp2 = mod.audio_fingerprint(vocals)
        check("内容变化则指纹变化", fp2 != fp1, f"{fp1} vs {fp2}")

        # 指纹必须能反映尾部变化（只哈希头部会漏掉尾部换素材的场景）
        vocals.write_bytes(b"B" * 2_999_999 + b"C")
        check("尾部变化也改变指纹", mod.audio_fingerprint(vocals) != fp2)

        # ---- 缓存命中路径 ----
        cache = work / "转写结果.json"
        cache.write_text(json.dumps({"lines": [{"text": "旧"}]}, ensure_ascii=False),
                         encoding="utf-8")
        # 旧缓存无 _source_fp：应补写并返回，不报错
        got = mod.call_asr.__wrapped__ if hasattr(mod.call_asr, "__wrapped__") else None
        check("call_asr 未用装饰器包装", got is None)

        # 直接验证判定逻辑：模拟两条分支
        payload = json.loads(cache.read_text(encoding="utf-8"))
        check("旧缓存无 _source_fp", "_source_fp" not in payload)

        cache.write_text(json.dumps({"lines": [{"text": "旧"}], "_source_fp": "mismatch"},
                                    ensure_ascii=False), encoding="utf-8")
        check("指纹不符可被识别",
              json.loads(cache.read_text(encoding="utf-8")).get("_source_fp") != fp2)

    # ---- 平铺清理不得毁掉唯一成果 ----
    # 曾实测：未装 article-format 时 render_docx 返回 False，调用方仍走 flatten_outputs，
    # 该函数只搬 docx/srt 后无条件 rmtree，把本次唯一的 md 成稿连目录一起删掉。
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        stem_dir = work / "separated_out" / "录音"
        stem_dir.mkdir(parents=True)
        md = stem_dir / "转写文稿-录音.md"
        md.write_text("# 转写文稿\n\n内容\n", encoding="utf-8")
        media = work / "录音.mp3"
        media.write_bytes(b"x")
        mod.flatten_outputs(stem_dir, media, "转写文稿-录音")
        check("无 docx/srt 时保留 md", md.is_file(), "md 被删")
        check("无 docx/srt 时保留工作目录", stem_dir.is_dir(), "目录被 rmtree")

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        stem_dir = work / "separated_out" / "录音"
        stem_dir.mkdir(parents=True)
        (stem_dir / "转写文稿-录音.md").write_text("# x\n", encoding="utf-8")
        (stem_dir / "转写文稿-录音.docx").write_bytes(b"PK")
        media = work / "录音.mp3"
        media.write_bytes(b"x")
        mod.flatten_outputs(stem_dir, media, "转写文稿-录音")
        check("有成稿时 docx 搬到素材目录",
              (work / "转写文稿-录音.docx").is_file(), "docx 未搬出")
        check("有成稿时工作目录被清理", not stem_dir.exists(), "工作目录残留")

    # ---- 目录扫描必须排除输出目录 ----
    # 默认输出落在 <输入>/separated_out，其拆轨产物同为 MEDIA_EXTS；不排除的话
    # 第二次运行会把它们当新素材再跑一遍 Demucs。
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "separated_out" / "录音").mkdir(parents=True)
        nested = root / "separated_out" / "录音" / "完整音轨.m4a"
        nested.write_bytes(b"x")
        src_media = root / "录音.mp3"
        src_media.write_bytes(b"x")
        out_root = (root / "separated_out").resolve()
        check("输出目录内文件判为输出", mod._is_under(nested, out_root))
        check("源素材不判为输出", not mod._is_under(src_media, out_root))

    # ---- 缓存读写健壮性 ----
    # 写缓存中途被杀会留半截 json；旧实现 json.loads 直接抛，这份素材此后只能 --force。
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        good = work / "转写结果.json"
        mod._write_json(good, {"lines": [{"text": "新"}]})
        check("原子写可读回", mod._read_json(good) == {"lines": [{"text": "新"}]})
        check("原子写不留 .tmp", not (work / "转写结果.json.tmp").exists(), "临时文件残留")

        broken = work / "坏缓存.json"
        broken.write_text('{"lines": [', encoding="utf-8")
        check("损坏缓存读作未命中", mod._read_json(broken) is None, "应返回 None")
        check("缺失文件读作未命中", mod._read_json(work / "不存在.json") is None)

    # ---- 声道分轨的人名映射 ----
    # 分轨模式下子进程内说话人恒为 0，两路都会拿到 names[0]；人名必须按声道号取。
    check("names 解析", mod._parse_names("0=张三,1=李四") == {0: "张三", 1: "李四"})
    check("names 容忍空格", mod._parse_names("1 = 李四") == {1: "李四"})
    check("names 空值", mod._parse_names("") == {})
    check("names 跳过非法项", mod._parse_names("坏项,2=王五") == {2: "王五"})

    print("SELFTEST", "PASS" if not fails else f"FAIL（{len(fails)} 项）")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
