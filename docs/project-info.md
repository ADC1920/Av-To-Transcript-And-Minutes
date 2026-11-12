# 项目说明

## 架构

音视频 → 转写文稿 / 结构化总结的单进程管线，所有环节在一个脚本内串行编排，重度计算下沉到子进程。

- `av_to_transcript_and_minutes.py`：主入口。负责拆轨（ffmpeg）、人声分离（demucs）、调用转写引擎、组稿与 docx 排版。
- `skills/funasr-transcribe/scripts/qwen_asr.py`：转写引擎。四选一：`qwen`（本地 Qwen3-ASR）、`aed`（本地 FireRedASR2-AED）、`cloud`（阿里云百炼）、`auto`（按语言路由）。
- `skills/meeting-notes-expert/`：纪要提炼技能，含七节模板、`validate_minutes.py` 结构校验器与 golden 回归用例。

关键分层：**识别可上云，说话人分离始终在本地**。`--meeting` 或带说话人的转写依赖本地 funasr VAD + cam++ 声纹聚类；`--no-diarize` 单人场景才有纯云端轻量路径。

## 目标

把会议录像、访谈录音、课程视频转成可直接使用的文本底稿，再由会话内 AI 提炼成结构化总结。管线不负责总结本身，只负责产出带时间戳与说话人标签的「会议原文」。

## 模块表

| 模块 | 职责 | 云端依赖 |
|---|---|---|
| 拆轨 / 分离 | ffmpeg 拆音视频、demucs 分人声 | 全本地 |
| 识别（cloud） | 百炼 message / omni / filetrans 三通道 | 需 DASHSCOPE_API_KEY |
| 说话人分离 | fsmn-vad 切段 + cam++ 聚类 / pyannote | 全本地（torch+funasr） |
| 后处理 | 繁转简、专名纠错、ITN、字幕 | 全本地 |
| 排版 | article-format 规范 docx | 全本地 |
| 纪要提炼 | meeting-notes-expert 模板 + 校验器 | 会话内 AI |
| 运行清单 | `manifest.json`：每素材参数/耗时/状态/产物 | 全本地 |
| 说话人泳道图 | `tools/speaker_timeline.py` | 全本地 |
| 跨会议检索 | `tools/meeting_index.py`（SQLite FTS5） | 全本地 |
| MCP 工具服务 | `tools/mcp_server.py`（job 语义） | 全本地 |

## 常驻转写进程（v2.6）

改造前每个素材起一个 `qwen_asr.py` 子进程，进程内重新加载主引擎；批量 N 个素材即 N 次加载。
v2.6 起主管线拉起一个常驻子进程（`qwen_asr.py --worker`），用 stdin/stdout 传 JSON 任务，
模型在进程内按 `(引擎, device)` 缓存，第一份素材加载一次之后复用。

实测：本地 Qwen3-ASR 批量两份素材，首次加载 2 秒，第二份加载耗时 0 秒且返回同一模型对象。

三条边界，改动时不要破坏：
- worker 的 stdout 是协议通道，日志必须走 stderr；混入日志会解析失败。
- 单任务的 `SystemExit` 必须转成该项失败，不能中断整批（一份坏素材不该让其余作废）。
- worker 不可用（启动失败、进程退出、协议不符）时，`call_asr` 回退到逐文件子进程，
  最坏情形与改造前等价。`--verbose` 强制走逐文件，因为要实时看子进程输出。

## 机器可读产物

| 产物 | 生成方式 | 用途 |
|---|---|---|
| `manifest.json` | 每次批量运行自动写 | 报表、索引、MCP 查询；只列实际存在的产物 |
| `纪要*.json` | `validate_minutes.py --export` | 待办表、决策列表、各节文本，供任务同步与检索 |
| `*.rttm` | `--rttm` | 标准说话人日志，外部复核工具直读 |
| `时间轴.html` | `tools/speaker_timeline.py` | 说话人泳道图 |

## MCP 工具面

`tools/mcp_server.py` 暴露 8 个工具：`transcribe_start/status/result/cancel/list`、
`search_meetings`、`index_meetings`、`render_timeline`。

job 状态落 `~/.av2m/jobs/<job_id>/job.json`（可用 `AV2M_JOBS` 覆盖），进程重启后仍可查。
幂等键 = 输入内容哈希 + 参数哈希，同一份文件同一份参数重复提交直接复用。

参数只接受白名单（`_ALLOWED_OPTIONS`）：拼错的键直接拒，不静默忽略——
静默忽略比报错更危险，agent 会以为生效了。

## 跨会议检索

`tools/meeting_index.py`，索引落 `~/.av2m/meetings.db`（可用 `AV2M_INDEX` 覆盖）。

分词器用 **trigram** 而不是 unicode61：中文没有词边界，unicode61 会把整句当一个大 token，
「销售数据」这类子串查询永远命中不了；trigram 按三字滑窗建索引，任意长度 ≥3 的中文子串都能匹配。
两字及以下的词 trigram 覆盖不到，退到 LIKE 子串扫描（会议量级可接受）。

先用 FTS5 而不是向量库的理由：会议检索的典型查询是专名、人名、项目名，关键词命中率高、
结果可解释、零额外依赖。向量检索等有真实查询日志再评估。

## 调研导航

- 云端引擎接口细节：`skills/funasr-transcribe/scripts/qwen_asr.py` 头部注释（含三通道实测记录）
- 说话人分离算法：同文件 `run_vad` / cam++ 聚类段
- 纪要模板与校验规则：`skills/meeting-notes-expert/SKILL.md` 与 `references/minutes-schema.json`

## 参考资源

- 阿里云百炼语音识别文档（message 走 WebSocket SDK，filetrans 走异步任务轮询）
- README「1b 云端优先」章节：本机最小依赖清单与 ASR_ENGINE 用法

## 本机约定

- 本机只走云端识别：环境变量 `ASR_ENGINE=cloud` + `DASHSCOPE_API_KEY`。
- 依赖装在项目内 `.venv`（Python 3.12，uv 管理），不入库。
- 仓库代码默认值保持离线 `qwen`，不改公开默认行为。
- 说话人分离本机走 campp：`--diarize-engine auto` 探测到未装 pyannote.audio 时自动回落并提示；装了 pyannote 则会议模式自动用 pyannote。
- 本机未装 demucs：含背景音的素材需先 `pip install demucs`，纯人声素材加 `--no-separate` 绕开。
- 本地推理走 GPU：`.venv` 装 torch 2.14.1+cu130，RTX 4060 8GB，`--device` 未显式指定时自动选 `cuda:0`。官方 CUDA 索引没有 2.15 版本，升级 torch 前先查 `download.pytorch.org/whl/cu130/torch/` 的实际可用版本。
- AED 引擎已可用：FireRedASR2S 代码仓在 `~/.local/fire-red-asr2s`，模型在 `~/.cache/modelscope/models/xukaituo--FireRedASR2-AED`。其字级时间戳走 `torchaudio.functional.forced_align`，需要 CUDA 版 torchaudio（当前 2.11.0+cu130）；CPU 版 torchaudio 会让该算子报错并被裸 `except:` 吞掉，时间戳静默退化为 None。
- Qwen3-ASR 主模型已下载（`.cache/modelscope/models/Qwen--Qwen3-ASR-1.7B-hf`，3.9GB），demucs 4.1.0 已装。

## 8GB 显存实测边界（2026-11-12）

各组件单独加载与推理峰值，均为 RTX 4060 8GB 实测：

| 组合 | 峰值显存 | 说明 |
|---|---|---|
| demucs 分离（30 秒 / 5 分钟） | 1.6 GB | 固定窗口分块，显存不随时长增长 |
| AED + ct-punc + fsmn-vad + cam++ | 3.27 GB | AED 路线全组件驻留 |
| Qwen3-ASR 加载 / 推理 | 3.80 / 3.87 GB | bf16 权重 |
| Qwen3-ASR + emotion2vec（8.5 秒素材） | 4.56 GB | |
| 完整管线 Qwen + Demucs + campp（5 分钟，不开情绪） | 4.90 GB | 安全 |
| 完整管线 Qwen + Demucs + campp + 情绪（5 分钟） | 7.72 GB | 接近上限，仅剩 0.28GB |
| 完整管线 AED + campp + 情绪（5 分钟） | 7.71 GB | 同上，峰值由情绪模型主导 |
| pyannote 分离（30 秒 / 5 分钟真实语音） | 1.59 GB | 固定窗口处理，显存不随时长增长 |
| 完整管线 pyannote + Qwen + Demucs（30 秒） | 6.15 GB | |

**唯一的硬边界是 emotion2vec**：它整段推理不分段，显存随时长线性上涨。10 分钟音频单独跑情绪就 OOM（要分配 3.66GB）。长素材开 `--emotion` 会失败并跳过情绪标注，转写本身不受影响（已改为输出明确的显存不足提示）。8GB 卡上 `--emotion` 的安全区间约在 5 分钟以内。

**除情绪外的其余能力都能吃下**：识别（Qwen/AED/cloud 三引擎）、说话人分离（campp）、人声分离（demucs）、字幕（srt/vtt）、RTTM、发言概览、ITN、专名纠错、声纹库全部可用。

**仍未装**：无。`pyannote.audio` 4.0.7 与 `pypinyin` 0.55.0 已补齐，上游全部本地能力可用。

## torchcodec 冲突的规避方式

装 `pyannote.audio` 会带入 `torchcodec`，而 torchcodec 与静态 FFmpeg 不兼容。README 原方案是装完卸掉 torchcodec，但实测发现**不卸也能跑**，前提是代码绕开它的解码路径：

- `run_pyannote` 用 soundfile 预载波形，本来就不碰 torchcodec。
- Qwen3-ASR 的 `transcribe_batch` 原先传文件路径给 `processor.apply_transcription_request`，processor 内部会用 torchcodec 解码，触发 `libtorchcodec_core*.dll` 加载失败。已改为先用 soundfile 读成 numpy 波形（多声道混单声道、必要时重采样到 16k）再传入，绕开解码路径。
- 实测改后 pyannote 链路完整跑通，产物与 campp 一致。

若仍遇到 torchcodec 相关报错，可执行 `pip uninstall torchcodec` 彻底移除（管线不依赖它）。

## 上游同步口径（2026-11-12 起）

- 同步远端时只取代码与文档，平台专属文件不入库。
- `article_skill()` 查找顺序：`ARTICLE_SKILL_DIR` → `~/.zcode/skills` → `~/.agents/skills` → 仓库 `skills/` → `~/.claude/skills`。
- `memory/agent-log.md` 为本机档案，每次合并保留本侧内容，吸收上游教训与质量基线条款，不整篇替换。

## 质量基线（随 v2.5 引入）

- `tests/test_quality_baseline.py`：缺素材时自动调 `scripts/make_fixtures.py` 生成到 `.tmp/fixtures/`
- `benchmark_quality.py --selftest`：只验 CER/编辑距离与 cpWER，DER 需 pyannote.metrics
- 门槛 DER ≤ 0.25、CER ≤ 0.30；改识别相关参数后重跑对比

