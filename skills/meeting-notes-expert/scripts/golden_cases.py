#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""golden_cases.py — golden 用例集批量回归（meeting-notes-expert 配套）

跑 references/golden/（旧单例）与 references/golden/cases/*/（多场景用例）下
所有 source.md + expected.md 成对用例，逐个调 validate_minutes.py，
最后汇总。用例口径参考 AutoMin 这类自动纪要评测的分场景做法：
每条用例只压一个提炼难点，坏了能立刻定位是哪条口径退步。

用法：
    python golden_cases.py              # 跑全部
    python golden_cases.py --list       # 只列用例
    python golden_cases.py soft         # 只跑名字含 soft 的用例

退出码：0 全过；1 有用例失败；2 用法错误。
"""
import argparse
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent / "references" / "golden"
TOOL = Path(__file__).resolve().parent / "validate_minutes.py"


def discover():
    """返回 [(用例名, source, expected)]。旧单例放最后，兼容既有调用习惯。"""
    cases = []
    root_pairs = (BASE / "source.md", BASE / "expected.md")
    if root_pairs[0].is_file() and root_pairs[1].is_file():
        cases.append(("原单例", root_pairs[0], root_pairs[1]))
    cases_dir = BASE / "cases"
    if cases_dir.is_dir():
        for d in sorted(cases_dir.iterdir()):
            if not d.is_dir():
                continue
            src, exp = d / "source.md", d / "expected.md"
            if src.is_file() and exp.is_file():
                cases.append((d.name, src, exp))
    return cases


def run_one(source, expected):
    proc = subprocess.run(
        [sys.executable, str(TOOL), str(expected), "--source", str(source)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main(argv=None):
    ap = argparse.ArgumentParser(description="golden 用例集批量回归")
    ap.add_argument("filter", nargs="?", default=None, help="只跑用例名含该子串的")
    ap.add_argument("--list", action="store_true", help="只列用例不执行")
    ns = ap.parse_args(argv)

    cases = discover()
    if ns.filter:
        cases = [c for c in cases if ns.filter in c[0]]
    if not cases:
        print("没找到用例（需要 source.md 与 expected.md 成对）")
        return 2
    if ns.list:
        for name, src, _ in cases:
            print(f"{name}\t{src}")
        return 0

    failed = []
    for name, src, exp in cases:
        rc, out = run_one(src, exp)
        verdict = "PASS" if rc == 0 else "FAIL"
        print(f"[{verdict}] {name}")
        if rc != 0:
            failed.append(name)
            for line in out.splitlines():
                if line.startswith(("[FAIL]", "[WARN]")):
                    print(f"    {line}")

    print(f"\n合计：{len(cases)} 用例，PASS {len(cases) - len(failed)}，FAIL {len(failed)}")
    if failed:
        print("失败用例：" + "、".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
