# Agent 日志 · Av-To-Transcript-And-Minutes

> 权威承载：项目根 `.git`（GitHub ADC1920/av-to-transcript-and-minutes）+ 本文件
> 通用规则见全局 `~/.zcode/AGENTS.md`

## 状态段

- 当前状态：主仓库已从 `Daily Use/publish/av-to-transcript-and-minutes` 整体移入项目根，工作区与 .git 完整（HEAD `a551ba6`）。
- 分支：`main`（当前）+ 4 个未合并 feat 分支（model-layer-upgrades / pipeline-p1p6-robustness / pipeline-p2p7 / v2.4）。
- 未提交改动：8 改 + 3 未跟踪（`benchmark_quality.py`、`test_pyannote_chunking.py`、`test_split_and_cluster.py`），+784 行。**这批改动只在磁盘、不在 Git**，动它前先另存。
- 远端：`origin = https://github.com/ADC1920/av-to-transcript-and-minutes.git`

## 本机归集目录（不入库，见 .gitignore）

- `archive/`：旧仓库 `av-to-minutes-deprecated`（只读镜像，含 .git）、`legacy-script-copy/`（Daily Use/scripts 单脚本副本 1021 行）、`funasr-skill-copy/`（.agents 技能脚本副本）。
- `samples/`：会议纪要样例（`会议纪要-面料花型采样与开发能力建设.docx` 与同名 .md），来源 `Daily Use/plan/meeting-20261020`。

## 教训区

（待积累）

## 偏好段

（待积累）

## 流水区

- 2026-11-02 18:32｜归集本机相关内容到项目目录｜主仓库 robocopy 移入（356 文件、字节级 md5 全对、git fsck 通过、4 分支 HEAD 一致），源目录已删；三处重复副本与纪要样例复制留档｜未验证：主脚本功能未跑（未改代码）
