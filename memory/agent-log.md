# Agent 日志 · Av-To-Transcript-And-Minutes

> 权威承载：项目根 `.git`（GitHub ADC1920/av-to-transcript-and-minutes）+ 本文件
> 通用规则见全局 `~/.zcode/AGENTS.md`

## 状态段

- 当前状态：主仓库已从 `Daily Use/publish/av-to-transcript-and-minutes` 整体移入项目根，工作区干净。
- 分支：`feat/quality-baseline-batch`（当前，已推 origin）+ `main`。原 4 个残留分支已删并打 `backup/*` 标签。
- 未提交改动：无。原 784 行改动已拆成 5 个提交入库，见流水区。
- 远端：`origin = https://github.com/ADC1920/av-to-transcript-and-minutes.git`
- 进行中：PR #11（https://github.com/ADC1920/av-to-transcript-and-minutes/pull/11），CI 6 项全绿，待合并。

## 本机归集目录（不入库，见 .gitignore）

- `archive/`：旧仓库 `av-to-minutes-deprecated`（只读镜像，含 .git）、`legacy-script-copy/`（Daily Use/scripts 单脚本副本 1021 行）、`funasr-skill-copy/`（.agents 技能脚本副本）。
- `samples/`：会议纪要样例（`会议纪要-面料花型采样与开发能力建设.docx` 与同名 .md），来源 `Daily Use/plan/meeting-20261020`。

## 教训区

- **squash 合并会让分支"看起来未合并"**：`git cherry` 靠补丁等价性判断，squash 后 commit hash 全变，会被判为未合并（`+`）。真正可靠的判据是 `git diff --stat <分支> <main 上对应的 squash commit>` 为空。本项目 4 个残留分支即如此，靠这条才敢删。

## 偏好段

（待积累）

## 流水区

- 2026-11-02 18:32｜归集本机相关内容到项目目录｜主仓库 robocopy 移入（356 文件、字节级 md5 全对、git fsck 通过、4 分支 HEAD 一致），源目录已删；三处重复副本与纪要样例复制留档｜未验证：主脚本功能未跑（未改代码）
- 2026-11-02 18:45｜784 行未提交改动进 Git｜拆 5 个提交：feat(asr) 字幕补对齐+pyannote 分块、feat(itn) WTP 后端、test(benchmark) 质量基准、docs(meeting-notes) 引擎默认值修正、chore(project) 项目承载｜验证：offline/skill-minutes/engine-units 三 job 本地全过
- 2026-11-02 18:58｜清理残留分支并推送｜4 个 feat 分支经 diff 验证与 main 完全一致后删除，打 backup/* 标签留档；分支推送 origin 并建 PR #11｜验证：远端 CI 6 项全绿，mergeable
