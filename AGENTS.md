# 项目规则 · Av-To-Transcript-And-Minutes

通用规则见全局 `~/.zcode/AGENTS.md`。

## 核心资产

| 资产 | 位置 | 说明 |
|---|---|---|
| 主管线脚本 | `av_to_transcript_and_minutes.py` | 音视频 → 转写文稿 / 会议纪要，一条命令全流程 |
| 质量基准 | `benchmark_quality.py` | 转写质量对比（DER / CER / 重叠检测），已入库，支持 `--selftest` |
| 技能副本 | `skills/funasr-transcribe/`、`skills/meeting-notes-expert/` | 识别与纪要技能，随仓库分发 |
| 端到端测试 | `test_pipeline_e2e.py` | 管线冒烟 |
| 项目档案 | `docs/project-info.md` | 架构、模块表、依赖与环境分层 |

## 操作流程

1. 改代码前先看 `git status`：本机存在未提交的业务改进，勿盲目 reset。
2. 本地运行：`python av_to_transcript_and_minutes.py --help`；系统依赖 FFmpeg 需在 PATH。
3. 真实词表（`hotwords.txt` / `replace_dict.txt`）含业务信息，永不入库，用 `*.example.txt` 模板。
4. 改动影响识别参数后，跑 `python tests/test_quality_baseline.py` 对照门槛（DER ≤ 0.25、CER ≤ 0.30）。
5. 提交前跑 CI 等价检查：`python -m py_compile` 全脚本，再依次跑 `skills/funasr-transcribe/scripts/` 下各 `test_*.py`。

## 产物与缓存约定

- 主产物：`{名}.md`（稿件）、`{名}.docx`（排版稿）；可选 `.srt` / `.vtt` 字幕、`.rttm` 说话人日志。
- 附属产物独立判定：缺 `.vtt` 或 `.rttm` 时走「补文件」通道，复用缓存转写，不重跑分离与识别。
- 缓存键不含纯字幕参数（`--align-engine` / `--max-line` / `--min-dur` / `--max-cps`），也不含 `--vad fsmn`（与升级前默认等价），改这些不会触发全量重转。

## 识别引擎

- 默认识别引擎可用环境变量 `ASR_ENGINE` 覆盖本机默认值（如 `ASR_ENGINE=cloud`），命令行显式传参优先。
- `--diarize-engine auto` 在会议模式下先探测 pyannote.audio，未装时回落 campp 并提示，不再子进程崩溃。
- 未装 demucs 时分离环节直接报可读结论并按单素材计失败，批量不中断；纯人声素材加 `--no-separate` 绕开。
- `article-format` 定位顺序：`ARTICLE_SKILL_DIR` → `~/.zcode/skills` → `~/.agents/skills` → `~/.claude/skills` → 仓库 `skills/`。

## 本机专属目录（已 gitignore）

- `archive/` 旧版本归档，`samples/` 纪要样例，`out/` video2code 产物，`.v2c/` 插件工作区，`.tmp/` 中间产物。
- `memory/agent-log.md` 与含本机盘符路径的接续记录只留本机，不入库。

## 文档

- 用法与参数：`README.md`
- 项目档案与流水：`memory/agent-log.md`
