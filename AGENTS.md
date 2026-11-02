# 项目规则 · Av-To-Transcript-And-Minutes

通用规则见全局 `~/.zcode/AGENTS.md`。

## 核心资产

| 资产 | 位置 | 说明 |
|---|---|---|
| 主管线脚本 | `av_to_transcript_and_minutes.py` | 音视频 → 转写文稿 / 会议纪要，一条命令全流程 |
| 质量基准 | `benchmark_quality.py` | 转写质量对比（未跟踪，待入库） |
| 技能副本 | `skills/funasr-transcribe/`、`skills/meeting-notes-expert/` | 识别与纪要技能，随仓库分发 |
| 端到端测试 | `test_pipeline_e2e.py` | 管线冒烟 |

## 操作流程

1. 改代码前先看 `git status`：本机存在未提交的业务改进，勿盲目 reset。
2. 本地运行：`python av_to_transcript_and_minutes.py --help`；系统依赖 FFmpeg 需在 PATH。
3. 真实词表（`hotwords.txt` / `replace_dict.txt`）含业务信息，永不入库，用 `*.example.txt` 模板。

## 本机专属目录（已 gitignore）

- `archive/` 旧版本归档，`samples/` 纪要样例，`out/` video2code 产物，`.v2c/` 插件工作区。

## 文档

- 用法与参数：`README.md`
- 项目档案与流水：`memory/agent-log.md`
