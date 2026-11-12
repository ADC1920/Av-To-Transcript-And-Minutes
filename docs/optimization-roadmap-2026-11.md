# 2026-11 调研：优化升级与新增能力路线

> 目的：回答「这个项目在 ZCode 上还能补什么」，分别从产品经理与后端工程师两个视角给出可执行的优先级。
> 方法：本地代码逐段核对 + GitHub 同类项目 + 中文商业产品 + 2026 年语音模型论文。
> 收录标准：结论必须能指到代码行、仓库或官方文档；拿不到证据的写「未核实」，不写成建议。

## 一、结论先行

1. **转写链路本身已到第一梯队，短板不在模型，在"交付形态"与"可被程序消费"**。Otter、Fireflies、Granola、飞书妙记、通义听悟把转写做成商品，差异点已移到行动项闭环与跨会议记忆。本项目仍停在「产出 docx」，下游没有任何机器可读产物。
2. **最贵的浪费是模型重复加载，不是识别本身**。批量 N 个素材会起 N 次 `qwen_asr.py` 子进程（`av_to_transcript_and_minutes.py:1359` 串行循环，`call_asr` 内 `run_child`），每次子进程都重新 `load_model`（`skills/funasr-transcribe/scripts/qwen_asr.py:1921`），Qwen3-ASR 单次加载占 3.80 GB 显存。这一项修掉，批量耗时直接少掉 N-1 次加载。
3. **ZCode 上最大的机会是把它变成 MCP 工具，而不是继续做 CLI + 读 SKILL.md 拼命令**。FunASR 官方已提供 MCP server，agentctl 的 job 式工具（start/status/result/cancel/resume）与 talkthrough-mcp 的幂等 job 设计都是现成参照。

## 二、现状盘点（代码核对结论）

### 已有的真实优势

| 能力 | 证据 | 为什么稀缺 |
|---|---|---|
| 中文 ITN 零依赖自写规则 | `skills/funasr-transcribe/scripts/itn_zh.py` | Meetily、Buzz、Scriberr 都不做中文数字规范化 |
| 规范 docx 排版 | `render_docx` 走 article-format，`av_to_transcript_and_minutes.py:759` | 同类开源止步于 md/txt |
| RTTM 标准说话人日志 | `write_rttm`，`av_to_transcript_and_minutes.py:679` | 外部复核与论文对齐需要，成本只有几行 |
| 无容差 DER + cpWER | `benchmark_quality.py` | 同类多为单一 DER，分不清"更准"与"边界更粗" |
| 字幕阅读速度规则 | `--min-dur` / `--max-cps` | 商业产品也很少把 12 字/秒做成可调参数 |
| 声纹库跨文件复用 | `--speaker-db`，campp 与 pyannote 双路径 | Meetily 的说话人识别是 Pro 付费项 |
| 缓存键含引擎、热词、词典哈希 | `asr_cache_name`，`av_to_transcript_and_minutes.py:400` | 多数项目只按文件名判断已处理 |
| VAD 按用途分流 | `resolve_vad_arg`，本机实测否掉 FireRedVAD 当默认 | 有实测依据的取舍，不是跟榜单 |

### 已确认的工程缺口

| 缺口 | 位置 | 影响 |
|---|---|---|
| 批量串行，素材之间无并发 | `av_to_transcript_and_minutes.py:1359` | GPU 空闲等待 ffmpeg/demucs/IO 的时间全浪费 |
| 每个素材重新加载全部模型 | `qwen_asr.py:1921`；demucs 每素材一次 `run_child` | 批量 N 个素材 N 次加载，Qwen 每次 3.8 GB |
| 无机器可读的运行清单 | 只有 stdout 与 `run.log` | 下游无法判断"哪份成稿是哪个参数跑出来的" |
| 纪要只有 md/docx | `minutes-schema.json` 的 `action_items` 只用于校验，不产出 | 行动项无法同步到任何任务系统 |
| 依赖不可复现 | 只有 `requirements.txt`，无 `pyproject.toml` / `uv.lock` | 上限只有大版本区间，环境漂移无法定位 |
| 无 job 状态机 | 完成判定靠 md/docx 文件存在与缓存键 | 无法查询进度、取消、断点恢复的显式语义 |
| 云端原生分离能力闲置 | `diarization_enabled` 只在 filetrans 用（`qwen_asr.py:524`），message 通道逐段上传后本地聚类 | 长会议多花本地 VAD 与 campp 时间，跨段说话人一致性弱于端到端 |
| CI 不验真实质量 | `.github/workflows/tests.yml` 的 `quality-baseline` 在 Linux 只验入口可导入 | 真实门槛只在本机，回归靠人记 |

## 三、产品层对标

商业产品的能力分层在 2026 年已经很清楚（Techgenyz、MeetWave 两篇独立分析口径一致）：

1. 转写与摘要：商品化，不构成差异。
2. 行动项提取：有责任人、期限、优先级，能推送到外部系统。
3. 跨会议智能：同一客户、同一项目的历史脉络，可检索可问答。
4. 治理：同意管理、留存策略、权限隔离、可删除。

本项目在层 1 有明显优势（中文、离线、docx、字幕规则），层 2 只有「纪要里有待办表格」这一半，层 3 与层 4 完全空白。

对照表（只列与本项目相关的行）：

| 能力 | Otter / Fireflies / Granola | 飞书妙记 / 通义听悟 | Meetily（3.2 万星） | 本项目 |
|---|---|---|---|---|
| 实时字幕 | 有 | 有 | 有 | 无 |
| 说话人标签 | 有 | 有，妙记可标真名 | Pro 版才有 | 有，含声纹库与人数约束 |
| 行动项带责任人 | 自动分配 | 有 | 有 | 纪要模板有，无结构产物 |
| 跨会议检索 | 有 | 听悟无跨笔记搜索 | 无 | 无 |
| 历史重转写 | 有 | 有 | 有，可换模型语言 | 有，缓存键区分 |
| 模板自定义 | Granola 核心能力 | 有限 | 有 | 模板写在 SKILL.md，用户改不了 |
| 字幕导出 | 有 | 有 | 有 | 有 SRT/VTT，含阅读速度规则 |
| 全离线 | 无 | 无 | 有 | 有 |
| 中文 ITN | 无 | 有 | 无 | 有 |
| 规范公文排版 | 无 | 无 | docx 导出 | 有 |
| 机器可读纪要 | API 有 | API 有 | 无 | 无 |

结论：不要向「实时」与「跨会议知识库」同时开战。前者要重构主干（项目自己的 `docs/landscape-2026-11.md` 已判定不重写），后者才是当前投入产出比最高的新产品面，且它的前置条件只是把已有 JSON 组织起来。

## 四、优先级建议

排序依据是「收益 ÷ 改动面」，并显式标注风险与回退方式。

### P0：先做，收益确定，改动面小

**P0-1 模型常驻批处理**

现状是每个素材一个子进程，进程内重新加载 Qwen、demucs、pyannote、campp。改法两条，取成本低的一条：

- 短期：给 `qwen_asr.py` 加多文件入参，一次进程内循环，模型只加载一次；主管线把同一批素材的转写合成一次调用。
- 长期：独立成常驻 HTTP 服务，管线通过请求调用，顺带解决 MCP 化。

验收：批量 10 个素材，统计 `[1] 引擎已加载` 出现次数从 10 降到 1，总耗时下降可量化。回退方式：保留逐文件子进程开关。

风险：低。缓存键机制已完善（`asr_cache_name` 含引擎、热词、词典哈希、源指纹），多文件模式不会破坏断点续跑。

**P0-2 运行清单 manifest.json**

每个素材落一条结构化记录：输入路径与指纹、生效参数、各阶段耗时、产物路径与校验和、状态、失败原因。放在输出目录，与 `run.log` 并列。

收益是复利型的：后续的 MCP 查询、可视化、知识库索引、审计全部依赖它。现在这些信息散落在 stdout 里，程序读不到。

验收：批量跑完后 `manifest.json` 可用 `jq` 查出每个素材的状态与耗时；失败素材带原始错误尾部。改动面：一个写盘函数加在批量循环收尾。

**P0-3 依赖可复现**

补 `pyproject.toml` 与 `uv.lock`，把 `requirements.txt` 降级为兼容入口。这与全局规则「Python 包管理一律 uv」一致，也解决 `torch` 与 `torchaudio` 版本号不同步这类已经踩过的坑（`docs/project-info.md` 记录 AED 时间戳依赖 CUDA 版 torchaudio 2.11.0+cu130）。

### P1：产品能力补强

**P1-1 纪要 JSON 产物**

`minutes-schema.json` 里 `action_items` 已经定义好六个字段（id/task/owner/due/priority/note），但只在 `validate_minutes.py` 校验时解析，不落盘。改成出稿时同时写 `纪要.json`。

这一步把「纪要」从给人看的文档变成给系统用的数据，是跨会议能力的前置条件。下游可以接任意任务系统，也可以直接进索引。

**P1-2 ZCode MCP 工具化**

把管线暴露成 MCP server，工具面按 job 语义设计（参照 agentctl 与 talkthrough-mcp 两个已上线实现）：

| 工具 | 作用 |
|---|---|
| `transcribe_start(path, options)` | 提交任务，立即返回 job_id |
| `transcribe_status(job_id)` | 队列/运行/完成/失败，带已完成秒数 |
| `transcribe_result(job_id, offset, limit)` | 分页取转写段，支持 partial |
| `transcribe_cancel(job_id)` | 取消并保留已落盘分块 |
| `search_meetings(query, speaker?, since?)` | 跨会议检索，返回会议、时间、发言人与时间戳 |

关键约束（从 talkthrough-mcp 抄来的两条硬规则）：按内容哈希做幂等，重复调用同一文件秒回；`diarize=true` 对已处理任务只补说话人，不重转。

这一步是本轮最有平台价值的建议。当前形态是 agent 读 SKILL.md 再手拼命令行，参数一多就漂；MCP 化之后，参数校验、状态查询、分页结果都收敛到工具契约里。

**P1-3 说话人泳道图**

`--rttm` 已产出标准说话人日志，画一张时间轴泳道图只差渲染层。项目自己的观察清单里已列此项。落成会话内可视化，或输出独立 HTML 便于随纪要分发。

**P1-4 跨会议索引**

用 SQLite FTS5 起步，不引入向量库。索引对象是 P1-1 的结构化产物加原文，按会议 id 组织，支持按说话人、时间范围过滤。

先用 FTS5 而不是向量的理由：会议检索的典型查询是「上次谁说过的某个专名」，关键词命中率高、可解释、零依赖；向量检索留到有真实查询日志再评估。

### P2：观察，条件触发

| 项 | 触发条件 | 备注 |
|---|---|---|
| 云端原生说话人分离 | 核实 message 通道是否支持 `speaker_diarization_enabled` | filetrans 通道已在传 `diarization_enabled`；官方文档明确 filetrans 与 `qwen-audio-3.1-asr-flash` 支持，message 走 WebSocket SDK 未核实 |
| 实时增量层 | 用户明确提出边录边转 | 项目已判定不重写主干，只能叠加，且只做 draft 不混入正式记录 |
| 声纹被动词门 | 有多场重复参会人的真实素材 | Teams 的做法是设置里 opt-in，之后从普通发言自动建声纹（MC1197146）；合规压力大于技术难度 |
| 纪要模板自定义 | 用户提出按不同会议类型出不同模板 | 现在模板写死在 `meeting-notes-expert/SKILL.md` |
| GPU 显存感知调度 | 批量出现 OOM 或并发后显存争抢 | 现在是跑前 `warn_low_vram` 提示，没有队列 |
| 音视频事件检测 | 用户要笑声、掌声、咳嗽标注 | `emotion2vec` 只做八类情绪，明确不做事件检测，已在 README 写清 |

## 五、明确不建议做

沿用并补充 `docs/landscape-2026-11.md` 的否决清单：

1. **不重写为 ONNX 或 C++**：收益在边缘分发，本项目是单机 GPU 管线。
2. **不为流式重构主干**：离线批处理的标点、ITN、说话人质量更高。
3. **不用 LLM 替代 ITN 与纠错正则**：不可复现，逐次漂移，已有基准可回归才是正解。
4. **不换 NeMo 或 Sortformer 做分离**：Windows 安装风险高，公开分数优势不足以抵消集成成本。这条有新的反面证据：2026-11 的独立评测里 pyannote Community-1 在 AliMeeting 上是 20.3% DER，DiariZen Large v2 是 10.74%，但集成代价仍是 Windows 上的 NeMo 全家桶。
5. **不做双引擎融合**：成本翻倍，噪声域可能反而拉高 WER。
6. **不追实时端到端新模型**：VibeVoice-ASR-Streaming（7B）、Confucius4-R2T2、Nemotron 3 Diarization 都是 2026 年的强结果，但它们解决的是「边说边出」与「实时分离」，与当前离线高品质交付的目标不同轨。等 P1 全部落地、确有实时需求时再评估。
7. **不把 `qwen3-asr-0.6B` 当默认**：它换来的是吞吐（128 并发下 2000 秒语音 1 秒转完），不是精度。本机是单任务 GPU 管线，用不上。

## 六、ZCode 平台侧的三个形态机会

按落地顺序：

1. **技能 → MCP 工具**（对应 P1-2）。技能解决的是「agent 知道怎么做」，MCP 解决的是「agent 能可靠地做」。当前每条命令十几个参数，靠 SKILL.md 自然语言约束，参数漂移是必然的。
2. **会话内可视化**（对应 P1-3）。发言占比、说话人泳道、转写质量（DER/CER 对比）都适合做成对话内视图，比让人读 RTTM 文本有效。
3. **插件常驻面板**。ZCode 的 MCP App 支持 `ui.surfaces` 常驻面板与 `widgetState` 记忆。转写任务队列、进度、结果预览适合做成面板；但它依赖 P0-2 的 manifest 与 P1-2 的 job 状态，前置没做完不必急。

## 七、验证方式

每条建议落地时的验收口径：

| 项 | 可重跑命令 | 判定 |
|---|---|---|
| P0-1 | 批量 10 素材，`grep -c "引擎已加载" run.log` | 从 10 降到 1，总耗时下降 |
| P0-2 | `jq '.items[] | select(.status=="failed")' manifest.json` | 失败项带错误尾部，成功项带全部产物路径 |
| P0-3 | `uv sync --frozen` 在干净环境 | 装出的 torch 与 torchaudio 版本可复现 |
| P1-1 | `python -c "import json; json.load(open('纪要.json'))"` + schema 校验 | 通过 `minutes-schema.json` |
| P1-2 | MCP 客户端 `tools/list` 后调 `transcribe_start` 再轮询 `transcribe_status` | 状态从 queued 走到 completed，重复调用同一文件秒回 |
| P1-4 | 对 3 场会议建索引后查专名 | 命中会议、说话人、时间戳，结果可点击定位 |

## 八、未核实项

以下结论依赖外部信息，本轮没有本机实测，落地前需先验证：

1. `qwen-audio-3.1-asr-message` 的 WebSocket SDK 是否支持 `speaker_diarization_enabled`。官方文档只明确 `qwen-audio-3.1-asr-flash`（HTTP）与 filetrans 支持。
2. 批量 10 素材时模型加载占实际耗时的比例。代码路径已确认是每素材一次，但缺少计时数据。
3. P0-1 的多文件模式在 8 GB 显存卡上的实际收益。模型只加载一次会常驻，显存峰值可能高于逐文件模式（逐文件模式释放更彻底）。
