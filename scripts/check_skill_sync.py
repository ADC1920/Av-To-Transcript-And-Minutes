"""双副本同步校验：比对仓库内技能与用户级安装副本是否一致。

用途
    技能同时存在于仓库（随项目分发）与用户级目录（实际被加载），两处手工同步无机检，
    历史上出过用户级副本长期停在 v1.8.1 未被发现的事故。本脚本把「是否同步」变成一条命令。

跑法
    python scripts/check_skill_sync.py                    # 检查，不一致 exit 1
    python scripts/check_skill_sync.py --skill meeting-notes-expert
    python scripts/check_skill_sync.py --fix              # 把仓库版覆盖到用户级
    python scripts/check_skill_sync.py --verbose          # 列出每个文件的状态
    python scripts/check_skill_sync.py --selftest         # 零依赖自检（CI 用）

判据
    · 只比技能实际运行需要的文件：SKILL.md / CHANGELOG.md / references / scripts
    · 换行符归一后再比对（.ps1 在仓库是 CRLF、用户级曾被写成 LF，纯字节比对必然误报）
    · 用户级多出的 backups/ 与 *.bak* 不算差异——那是历史备份，不是生效文件
    · 版本号从 SKILL.md frontmatter 读，两边版本不同直接判失败（这是最直观的脱节信号）

注意
    本脚本依赖本机 ~/.zcode/skills，**不适合作 CI 门槛**。CI 只跑 --selftest 验证工具本身。

退出码
    0 一致 / 1 不一致 / 2 路径缺失等环境问题
"""

import argparse
import hashlib
import os
import re
import shutil
import sys
from pathlib import Path

# 技能运行时需要的文件；备份、缓存、pycache 不在其中
SKIP_DIRS = {"__pycache__", "backups", ".git"}
SKIP_SUFFIXES = (".bak", ".pyc")
SKIP_PATTERNS = (r"\.bak-", r"\.bak$")
# 仓库级配置文件：独立仓库是技能自己的仓，本仓副本只是被嵌进别人的仓，
# 两者的 .gitignore/.gitattributes 本就该不同（副本的忽略规则由所在仓决定），不算漂移。
REPO_CONFIG_FILES = {".gitignore", ".gitattributes", ".gitmodules"}


def iter_skill_files(root: Path):
    """产出技能生效文件的相对路径（posix 形式），跳过备份与缓存。"""
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if p.suffix in SKIP_SUFFIXES:
            continue
        if p.name in REPO_CONFIG_FILES:
            continue
        if any(re.search(pat, p.name) for pat in SKIP_PATTERNS):
            continue
        yield rel.as_posix()


def norm_bytes(data: bytes) -> bytes:
    """换行归一后再比：CRLF→LF。Windows 交付物与仓库文件本就允许换行符不同。"""
    return data.replace(b"\r\n", b"\n")


def digest(path: Path) -> str:
    return hashlib.md5(norm_bytes(path.read_bytes())).hexdigest()


def read_version(skill_dir: Path) -> str:
    """从 SKILL.md frontmatter 读 version，读不到返回空串（不拦，只提示）。"""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return ""
    try:
        head = skill_md.read_text(encoding="utf-8", errors="replace")[:1200]
    except OSError:
        return ""
    m = re.match(r"---\n(.*?)\n---", head, re.S)
    if not m:
        return ""
    vm = re.search(r'version:\s*"?([0-9][^"\n]*)"?', m.group(1))
    return vm.group(1).strip() if vm else ""


def compare(repo: Path, user: Path, verbose: bool):
    """返回 (missing_in_user, missing_in_repo, differing) 三个列表。"""
    repo_files = set(iter_skill_files(repo))
    user_files = set(iter_skill_files(user))
    missing_in_user = sorted(repo_files - user_files)
    missing_in_repo = sorted(user_files - repo_files)
    differing = []
    for rel in sorted(repo_files & user_files):
        if digest(repo / rel) != digest(user / rel):
            differing.append(rel)
    if verbose:
        for rel in sorted(repo_files):
            status = "OK" if rel not in differing and rel not in missing_in_user else "DIFF"
            print(f"  [{status}] {rel}")
    return missing_in_user, missing_in_repo, differing


def selftest() -> int:
    """零依赖自检：临时目录造两套技能，验证「一致/版本漂移/缺文件/换行差异」四种判定。

    CI 环境没有本机 ~/.zcode/skills，只能靠这里验证判断逻辑本身。
    """
    import tempfile

    passed = failed = 0

    def check(name, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"[PASS] {name}")
        else:
            failed += 1
            print(f"[FAIL] {name}" + (f"  <<{detail}>>" if detail else ""))

    def make_skill(root: Path, version="1.0.0", extra_file=True):
        d = root / "meeting-notes-expert"
        (d / "references").mkdir(parents=True, exist_ok=True)
        (d / "scripts").mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f'---\nname: t\nmetadata:\n  version: "{version}"\n---\n# t\n', encoding="utf-8")
        (d / "CHANGELOG.md").write_text("# log\n", encoding="utf-8")
        if extra_file:
            (d / "references" / "templates.md").write_text("# 模板\n", encoding="utf-8")
        return d

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        repo_root, user_root = base / "repo", base / "user"
        (repo_root / "skills").mkdir(parents=True)
        make_skill(repo_root / "skills")
        make_skill(user_root)

        # 1. 一致 → 0
        argv = ["--repo-root", str(repo_root), "--user-root", str(user_root)]
        check("一致时应 exit 0", main(argv) == 0)

        # 2. 换行符不同 → 仍算一致（CRLF vs LF 是 Windows 交付物常态）
        p = user_root / "meeting-notes-expert" / "scripts" / "docx2pdf_wps.ps1"
        p.write_bytes(b"param()\r\nWrite-Output 1\r\n")
        p2 = repo_root / "skills" / "meeting-notes-expert" / "scripts" / "docx2pdf_wps.ps1"
        p2.write_bytes(b"param()\nWrite-Output 1\n")
        check("仅换行符不同不算漂移", main(argv) == 0)

        # 3. 版本号漂移 → 1
        (user_root / "meeting-notes-expert" / "SKILL.md").write_text(
            '---\nname: t\nmetadata:\n  version: "0.9.0"\n---\n# t\n', encoding="utf-8")
        check("版本号不同应 exit 1", main(argv) == 1)

        # 4. 内容漂移 → 1，且 --fix 后归零
        (user_root / "meeting-notes-expert" / "SKILL.md").write_text(
            '---\nname: t\nmetadata:\n  version: "1.0.0"\n---\n# 改过\n', encoding="utf-8")
        check("内容不同应 exit 1", main(argv) == 1)
        check("--fix 后应 exit 0", main(argv + ["--fix"]) == 0)

        # 5. 用户级缺文件 → 1
        (user_root / "meeting-notes-expert" / "references" / "templates.md").unlink()
        check("缺文件应 exit 1", main(argv) == 1)

        # 6. backups/ 与 *.bak 不算差异
        bd = user_root / "meeting-notes-expert" / "backups"
        bd.mkdir(exist_ok=True)
        (bd / "SKILL.v1.1.0.bak.md").write_text("old\n", encoding="utf-8")
        (user_root / "meeting-notes-expert" / "SKILL.md.bak-20261028").write_text("old\n", encoding="utf-8")
        check("仅多出备份文件不算漂移", main(argv + ["--fix"]) == 0)

        # 7. 路径不存在 → 2
        check("用户级目录不存在应 exit 2",
              main(["--repo-root", str(repo_root), "--user-root", str(base / "nope")]) == 2)

        # 8. 独立仓库作为权威源：它直接对用户级比对（本仓副本已删除，不再参与）
        # 注意先清空用户级——前面用例往里面塞过文件和备份，不清会被算成「用户级多出文件」
        user_skill = user_root / "meeting-notes-expert"
        if user_skill.exists():
            shutil.rmtree(user_skill)
        upstream_root = base / "upstream"
        up = make_skill(upstream_root)   # 返回的是 <upstream_root>/meeting-notes-expert
        shutil.copytree(up, user_skill)
        up_argv = ["--repo-root", str(repo_root), "--user-root", str(user_root),
                   "--upstream-root", str(up)]
        check("独立仓库与用户级一致时 exit 0", main(up_argv) == 0)
        (up / "SKILL.md").write_text(
            '---\nname: t\nmetadata:\n  version: "2.0.0"\n---\n# 技能自己演化了\n', encoding="utf-8")
        check("独立仓库版本领先应 exit 1", main(up_argv) == 1)

        # 9. 独立仓库的 .gitignore 不算技能内容差异（它不是技能运行时文件）
        (up / "SKILL.md").write_text(
            '---\nname: t\nmetadata:\n  version: "1.0.0"\n---\n# t\n', encoding="utf-8")
        (up / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
        check("独立仓库的 .gitignore 不算漂移", main(up_argv) == 0)

        # 10. 本仓无副本时，权威源正确回退到独立仓库（删副本后的主场景）
        check("本仓无副本仍能工作",
              main(["--repo-root", str(base / "empty-repo"), "--user-root", str(user_root),
                    "--upstream-root", str(up)]) == 0)

    print(f"\nSELFTEST {'PASS' if not failed else 'FAIL'}（{passed} 通过 / {failed} 失败）")
    return 0 if not failed else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="比对仓库技能与用户级副本是否同步")
    ap.add_argument("--skill", default="meeting-notes-expert", help="技能目录名")
    ap.add_argument("--repo-root", default=None, help="仓库根（默认脚本上两级）")
    ap.add_argument("--user-root", default=None,
                    help="用户级技能根（默认 ~/.zcode/skills）")
    ap.add_argument("--upstream-root", default=None,
                    help="独立仓库的技能目录（默认仓库同级目录下的同名技能；不存在则跳过比对）")
    ap.add_argument("--fix", action="store_true", help="用仓库版覆盖用户级缺失/不同的文件")
    ap.add_argument("--verbose", action="store_true", help="逐文件列出状态")
    ap.add_argument("--selftest", action="store_true", help="零依赖自检（CI 用，不碰本机目录）")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    repo_root = Path(args.repo_root) if args.repo_root else Path(__file__).resolve().parent.parent
    user_root = Path(args.user_root) if args.user_root else Path(os.path.expanduser("~/.zcode/skills"))
    repo = repo_root / "skills" / args.skill
    user = user_root / args.skill
    # 权威源：独立仓库优先；仓库副本已删除时就用独立仓库直接对用户级。
    up_arg = Path(args.upstream_root) if args.upstream_root else repo_root.parent / args.skill
    source = up_arg if (up_arg / "SKILL.md").is_file() else repo

    if not source.is_dir():
        print(f"找不到技能权威源：{up_arg}（独立仓库）与 {repo}（本仓副本）均不存在", file=sys.stderr)
        return 2
    if not user.is_dir():
        print(f"用户级技能目录不存在：{user}", file=sys.stderr)
        print("（技能未安装到用户级，或安装位置不同——本检查只覆盖 ~/.zcode/skills）", file=sys.stderr)
        return 2

    using_upstream = source == up_arg
    tag = "独立仓库" if using_upstream else "本仓副本"
    rv, uv = read_version(source), read_version(user)
    print(f"{tag} v{rv or '?'}  →  用户级 v{uv or '?'}")

    missing_in_user, missing_in_repo, differing = compare(source, user, args.verbose)

    if args.fix:
        fixed = 0
        for rel in missing_in_user + differing:
            dst = user / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / rel, dst)
            fixed += 1
        print(f"已用{tag}覆盖 {fixed} 个文件到用户级")
        # 覆盖后必须重新读版本号与文件状态——沿用 fix 之前的值会让刚修好的项继续报错（实测）
        uv = read_version(user)
        missing_in_user, missing_in_repo, differing = compare(source, user, False)

    problems = []
    if rv and uv and rv != uv:
        problems.append(f"版本号不同：{tag} v{rv} vs 用户级 v{uv}")
    if missing_in_user:
        problems.append("用户级缺文件：" + "、".join(missing_in_user))
    if differing:
        problems.append("内容不同：" + "、".join(differing))
    if missing_in_repo:
        # 用户级独有的生效文件也值得报——说明权威源漏了东西
        problems.append(f"{tag}缺文件（用户级有）：" + "、".join(missing_in_repo))

    # 若本次以独立仓库为权威源，且本仓还残留副本，一并核对两者是否一致
    if using_upstream and repo.is_dir() and repo.resolve() != up_arg.resolve():
        uv2 = read_version(repo)
        m2, _r2, d2 = compare(up_arg, repo, False)
        if uv2 and rv and uv2 != rv:
            problems.append(f"本仓残留副本 v{uv2} 与独立仓库 v{rv} 版本不同")
        if m2:
            problems.append("本仓副本缺文件（独立仓库有）：" + "、".join(m2))
        if d2:
            problems.append("本仓副本与独立仓库内容不同：" + "、".join(d2))
        print(f"（另核对了本仓残留副本 v{uv2 or '?'}）")

    if problems:
        for p in problems:
            print(f"[DIFF] {p}")
        print(f"结论：不同步（--fix 把{tag}覆盖到用户级；本仓残留副本与独立仓库需手工对齐）")
        return 1
    print("结论：已比对的所有位置一致")
    return 0

if __name__ == "__main__":
    sys.exit(main())
