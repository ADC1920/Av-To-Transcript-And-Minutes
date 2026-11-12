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
5. 提交前跑 CI 等价检查：`python -m py_compile` 全脚本，再依次跑 `skills/funasr-transcribe/scripts/` 下各 `test_*.py` 与 `tests/test_tools_units.py`。
6. 依赖用 uv 管理：`uv sync` 按 `uv.lock` 安装（torch 走 cu130 索引）；改依赖改 `pyproject.toml` 后重跑 `uv lock`，不要手改 `uv.lock`。判断装对没有看版本后缀有没有 `+cu130`，只看版本号会漏掉 CPU 版。

## 工具层（tools/）

| 工具 | 跑法 | 说明 |
|---|---|---|
| `speaker_timeline.py` | `python tools/speaker_timeline.py <json\\|docx> -o 时间轴.html` | 说话人泳道图；输出是 HTML 片段，喂给支持内联渲染的宿主 |
| `meeting_index.py` | `python tools/meeting_index.py add <目录>` / `search <词>` | 跨会议检索；中文走 trigram，两字词退 LIKE |
| `mcp_server.py` | `python tools/mcp_server.py` | MCP 工具服务（stdio），需 `uv sync --extra mcp` |
| `validate_minutes.py` | 加 `--export <json>` | 纪要导出结构化 JSON（待办表、决策列表） |

调用约定：MCP 的 `transcribe_start` 立即返回 `job_id`，不要在同一次调用里等转写完成；查询用 `transcribe_status` 轮询。参数只接受白名单，拼错的键会被拒而不是静默忽略。

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
- 用户主目录下另有两处本机数据，在项目外故 gitignore 不覆盖，也都不入库：
  - `~/.av2m/meetings.db` 跨会议检索索引（`tools/meeting_index.py`，可用 `AV2M_INDEX` 覆盖）
  - `~/.av2m/jobs/` MCP 任务状态（`tools/mcp_server.py`，可用 `AV2M_JOBS` 覆盖）
  两者都含真实会议路径与逐段原文，换机需自行迁移，删掉只影响检索与任务查询、不影响成稿。

## 文档

- 用法与参数：`README.md`
- 项目档案与流水：`memory/agent-log.md`
