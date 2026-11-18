#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""minutes_quality.py — 纪要结构化导出与质量校验的统一入口。

管线只负责产出会议原文，纪要是会话内 AI 按 meeting-notes-expert 模板写的。
因此这里不生成纪要，只做两件下游需要的事：

  1. 导出：把纪要 md/docx 解析成结构化 JSON（基本信息/各节/待办表/决策列表），
     供任务同步与跨会议检索消费；
  2. 校验：跑 meeting-notes-expert 自带的校验器，把 FAIL/WARN 转成机器可读结论。

两条实现都复用技能里的 `validate_minutes.py`，不另写解析器——两套解析必然漂移。

跑法：
  python tools/minutes_quality.py --export <纪要.md|docx> [--out <json>] [--source <原文.md>]
  python tools/minutes_quality.py --check  <纪要.md|docx> [--source <原文.md>]
  python tools/minutes_quality.py --export <纪要> --check     # 两者一起做
  python tools/minutes_quality.py --selftest
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent

# 技能目录查找顺序与主脚本 article_skill() 保持同一口径：
# 仓库副本优先（随代码分发、版本一致），环境变量可覆盖到用户级安装。
SKILL_CANDIDATES = ("skills/meeting-notes-expert",)


def _skill_dir() -> Path | None:
    """定位 meeting-notes-expert 技能目录；找不到返回 None。"""
    import os
    env = os.environ.get("MEETING_SKILL_DIR")
    cands = ([Path(env)] if env else []) + [REPO_ROOT / c for c in SKILL_CANDIDATES]
    home = Path.home()
    cands += [home / ".agents/skills/meeting-notes-expert",
              home / ".zcode/skills/meeting-notes-expert",
              home / ".claude/skills/meeting-notes-expert"]
    for c in cands:
        if (c / "scripts/validate_minutes.py").is_file():
            return c
    return None


def _load_validator():
    """按路径加载 validate_minutes 模块，避免依赖 sys.path 顺序。"""
    skill = _skill_dir()
    if skill is None:
        raise FileNotFoundError(
            "找不到 meeting-notes-expert 技能（需要其 scripts/validate_minutes.py）；"
            "可用 MEETING_SKILL_DIR 指定")
    path = skill / "scripts/validate_minutes.py"
    spec = importlib.util.spec_from_file_location("validate_minutes", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_source_text(source: Path | None) -> str | None:
    """读源材料纯文本；会议原文 md 的 `## 发言概览` 段与标题不含发言，剔掉可减少噪声，
    但不删时间戳——校验器自己要靠剥时间戳来防止「[00:12] 给编造的 12 背书」。"""
    if source is None:
        return None
    return Path(source).read_text(encoding="utf-8", errors="replace")


def export(path: Path, out: Path | None = None) -> dict:
    """导出结构化纪要；默认写同目录 `<stem>.json`。

    返回统计与 JSON 内容，但调用方打印时只取 json/action_items/decisions 三个计数，
    不要把 data 整体串进日志——整份待办与决策会把一行日志撑成几百字（实测）。
    """
    mod = _load_validator()
    data = mod.export_structured(Path(path))
    target = Path(out) if out else Path(path).with_suffix(".json")
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"json": str(target),
            "action_items": len(data.get("action_items") or []),
            "decisions": len(data.get("decisions") or []),
            "data": data}


def check(path: Path, source: Path | None = None, strict: bool = False) -> dict:
    """跑校验，返回 {status, fails, warns, results}。

    不抛异常也不以非零退出表达「纪要有问题」：纪要是人写的稿子，
    校验结论是给它看的元信息，不是让管线判死的开关。
    """
    mod = _load_validator()
    rc, results = mod.validate(Path(path), read_source_text(source), strict)
    status, warnings = mod.summarize(results)
    fails = [{"step": r[1], "detail": r[2]} for r in results if r[0] == "FAIL"]
    warns = [{"step": r[1], "detail": r[2]} for r in results if r[0] == "WARN"]
    return {"status": status, "rc": rc, "fails": fails, "warns": warns,
            "results": [[r[0], r[1], r[2]] for r in results]}


def _print_check(res: dict, name: str) -> None:
    total = len(res["results"])
    passed = sum(1 for r in res["results"] if r[0] == "PASS")
    print(f"[校验] {name}：{res['status']}（PASS {passed}/{total}）")
    for item in res["fails"]:
        print(f"  FAIL {item['step']}  {item['detail']}", file=sys.stderr)
    for item in res["warns"]:
        print(f"  WARN {item['step']}  {item['detail']}", file=sys.stderr)


def selftest() -> int:
    """自检：技能可定位、校验器可加载、导出结构含必备键。"""
    fails = []

    def ck(name, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'} {name}" + (f"  {detail}" if not cond else ""))
        if not cond:
            fails.append(name)

    skill = _skill_dir()
    ck("能定位 meeting-notes-expert", skill is not None)
    if skill is None:
        print("SELFTEST FAIL（1 项）")
        return 1
    mod = _load_validator()
    ck("校验器可加载", hasattr(mod, "validate") and hasattr(mod, "export_structured"))

    # 拿技能自带的 golden 期望稿做端到端导出，验证结构键齐全
    golden = skill / "references/golden/expected.md"
    if golden.is_file():
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "x.json"
            data = export(golden, out)
            ck("导出落盘", out.is_file())
            for k in ("basic_info", "sections", "action_items", "decisions"):
                ck(f"导出含 {k}", k in data["data"])
            res = check(golden)
            ck("校验返回三态", res["status"] in ("complete", "degraded", "failed"),
               res["status"])
    else:
        ck("golden 样例存在", False, str(golden))

    print("SELFTEST", "PASS" if not fails else f"FAIL（{len(fails)} 项）")
    return 0 if not fails else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="纪要结构化导出与质量校验")
    ap.add_argument("file", nargs="?", help="纪要 md/docx")
    ap.add_argument("--export", action="store_true", help="导出结构化 JSON")
    ap.add_argument("--check", action="store_true", help="跑质量校验")
    ap.add_argument("--out", default=None, help="导出 JSON 路径（默认同目录 <stem>.json）")
    ap.add_argument("--source", default=None, help="源材料（会议原文 md），用于数字与责任人溯源")
    ap.add_argument("--strict", action="store_true", help="WARN 也判失败")
    ap.add_argument("--selftest", action="store_true")
    ns = ap.parse_args(argv)

    if ns.selftest:
        return selftest()
    if not ns.file:
        ap.error("需要纪要文件，或 --selftest")
    path = Path(ns.file)
    if not path.is_file():
        print(f"文件不存在：{path}", file=sys.stderr)
        return 2
    if not ns.export and not ns.check:
        ns.export = ns.check = True

    if ns.export:
        try:
            data = export(path, Path(ns.out) if ns.out else None)
        except Exception as e:
            print(f"[导出] 失败：{e}", file=sys.stderr)
            return 1
        print(f"[导出] {data['json']}（待办 {data['action_items']} 条，"
              f"决策 {data['decisions']} 条）")
    if ns.check:
        res = check(path, Path(ns.source) if ns.source else None, ns.strict)
        _print_check(res, path.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
