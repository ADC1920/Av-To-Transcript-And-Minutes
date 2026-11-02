# Agent 日志 · Av-To-Transcript-And-Minutes

> 权威承载：项目根 `.git`（GitHub ADC1920/Av-To-Transcript-And-Minutes）+ 本文件
> 通用规则见全局 `~/.zcode/AGENTS.md`

## 状态段

- 当前状态：主仓库已从 `Daily Use/publish/av-to-transcript-and-minutes` 整体移入项目根，工作区干净。
- 分支：`main`（当前）。PR #11 `b655448`、#12 `d483383`、#13 `a22647a` 均已合并。
- 未提交改动：无。
- 远端：`origin = https://github.com/ADC1920/Av-To-Transcript-And-Minutes.git`（2026-11-02 由小写名改来，旧 URL 仍重定向）
- 验证状态：pyannote 实跑、e2e、质量基线均已完成，见下方环境段。

## 质量基线与门禁

- 跑法：`python tests/test_quality_baseline.py`（缺素材自动调 `scripts/make_fixtures.py` 生成）
- 素材：`.tmp/fixtures/`，音频文件不入库（可复现）。
  注意：合成素材只能用 Windows TTS；本机另一音色 Zira 是 en-US，念中文产出 0 帧。
- 当前基线：DER 9.86%（pyannote.metrics 标准口径，0.25s collar）、CER 0、重叠检测 1 处 5.4 秒
- 门槛：DER ≤ 0.25、CER ≤ 0.30；改任何影响识别的参数后必须重跑，看是变好还是变坏
- CI：`quality-baseline` job 在 Linux 上只验脚本可导入（环境不支持则跳过），真实门槛在本机跑

## 本机验证环境（2026-11-02 落地）

- pyannote community-1 模型：`~/.cache/modelscope/models/pyannote--speaker-diarization-community-1/snapshots/master`（33M，segmentation + embedding + plda 全套，ModelScope 下载，无需 HF token）
- 依赖齐备：torch 2.13.0+cu130、pyannote.audio 4.0.7、demucs 4.1.0、funasr 1.4.8、ffmpeg 9.0-full
- GPU：RTX 5070 Ti 16GB
- 测试素材：`.tmp/verify/meeting.wav`（31.4s 双人交替，Windows TTS + rubberband 降调，含 `.ref.json` 人工标注）。`.tmp/` 已 gitignore，属一次性产物

## 本机归集目录（不入库，见 .gitignore）

- `archive/`：旧仓库 `av-to-minutes-deprecated`（只读镜像，含 .git）、`legacy-script-copy/`（Daily Use/scripts 单脚本副本 1021 行）、`funasr-skill-copy/`（.agents 技能脚本副本）。
- `samples/`：会议纪要样例（`会议纪要-面料花型采样与开发能力建设.docx` 与同名 .md），来源 `Daily Use/plan/meeting-20261020`。

## 教训区

- **squash 合并会让分支"看起来未合并"**：`git cherry` 靠补丁等价性判断，squash 后 commit hash 全变，会被判为未合并（`+`）。真正可靠的判据是 `git diff --stat <分支> <main 上对应的 squash commit>` 为空。本项目 4 个残留分支即如此，靠这条才敢删。
- **文档声称的机制要对着代码验一遍**：分块映射的注释与 README 都写"用声纹相似度"，实现却是时间重合投票，声纹函数在分块路径一次都没调用。文档写了不等于代码做了，看文档判断功能是否成立会踩空。
- **"能跑通"不等于"结果对"**：10-28 那次分块只用 3 秒块验了退出码与产物存在，没验说话人身份是否保住，于是把失效实现当成通过写进了 README。合成素材要带标注才能验质量。
- **短块是 pyannote 的精度下限**：10 秒块内两人被合并成单标签，强制 `num_speakers=2` 也无效（上下文太短，聚类无从展开）。块长不要低于 30 秒。
- **自写评测算法要先用标准实现校验**：为 DER 自写 100ms 网格版，把同一份完美标注误判成 0.50，pyannote.metrics 标准实现给出 0.0986。评测器本身出错比被测代码出错更隐蔽——它会让好改动看起来是坏的。
- **TTS 音色要匹配语言**：Zira 是 en-US，念中文 PowerShell 不报错但写出 0 帧 wav。空音频必须显式拦截，否则标注全变成零长度段。

## 偏好段

（待积累）

## 流水区

- 2026-11-02 18:32｜归集本机相关内容到项目目录｜主仓库 robocopy 移入（356 文件、字节级 md5 全对、git fsck 通过、4 分支 HEAD 一致），源目录已删；三处重复副本与纪要样例复制留档｜未验证：主脚本功能未跑（未改代码）
- 2026-11-02 18:45｜784 行未提交改动进 Git｜拆 5 个提交：feat(asr) 字幕补对齐+pyannote 分块、feat(itn) WTP 后端、test(benchmark) 质量基准、docs(meeting-notes) 引擎默认值修正、chore(project) 项目承载｜验证：offline/skill-minutes/engine-units 三 job 本地全过
- 2026-11-02 18:58｜清理残留分支并推送｜4 个 feat 分支经 diff 验证与 main 完全一致后删除，打 backup/* 标签留档；分支推送 origin 并建 PR #11｜验证：远端 CI 6 项全绿，mergeable
- 2026-11-02 19:05｜合并 PR #11 + 仓库改名｜GitHub 名改为 Av-To-Transcript-And-Minutes，remote/README/requirements/agent-log 同步｜验证：fetch 通过，旧 URL 仍 200
- 2026-11-02 19:15｜补做两项未验证｜装 pyannote 模型、合成双人标注素材、e2e 19 项断言全过、整段分离 DER 13.61%、完整管线 15.1 秒出会议原文｜验证：转写内容与人名映射正确
- 2026-11-02 20:30｜4 项工程改进（对照 WhisperX/Meetily/FluidAudio 差距分析后落地）｜schema 版本号、VTT 输出、依赖上界、合成素材质量基线｜验证：11 组测试 ALL PASS，PR #13 CI 6 项全绿已合并｜实测 DER 9.86% / CER 0 / 重叠 5.4s
- 2026-11-02 19:50｜修复分块丢说话人 bug｜跨块映射由时间投票改为声纹匹配，抽两个纯函数并补 12 项单测；实测 900s/30s/20s 块均正确分出两人｜验证：9 组单测 ALL PASS、e2e ALL PASS、PR #12 CI 6 项全绿已合并｜遗留：15s/10s 块受 pyannote 短块精度限制仍失败，已加 30 秒下限提示
