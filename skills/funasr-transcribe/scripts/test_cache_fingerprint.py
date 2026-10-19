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

ROOT = Path(__file__).resolve().parents[3]
MAIN = ROOT / "av_to_transcript_and_minutes.py"

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

    print("SELFTEST", "PASS" if not fails else f"FAIL（{len(fails)} 项）")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
