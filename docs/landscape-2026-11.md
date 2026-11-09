# 2026-11 调研：同类项目与本地语音模型格局

> 目的：给本项目的技术选型找外部参照，并回答"哪些地方我们落后、哪些已经领先"。
> 收录标准：只留做过本地实测或有一手来源（仓库 README / 官方 leaderboard）的结论；
> 拿不到真实数字的一律标"未核实"，不转述二手印象。

## 一、结论先行

1. 本项目的**链路完整性**在开源里少见：拆轨 → 人声分离 → 说话人分离 → 识别 → 字幕 → ITN → docx，
   同批项目没有一个把中文 ITN 到 docx 这段也做进来（Meetily、Buzz、Scriberr 都止步于转写+LLM 纪要）。
2. 落后项集中在**交付形态**与**评测口径**：没有实时/流式、没有说话人时间轴可视化、
   评测只有 DER/CER 两个数。这三项已在本轮部分补齐（RTTM、发言概览、无容差 DER、cpWER）。
3. 识别模型层面**不需要换主力**：Qwen3-ASR-1.7B 仍是中文第一梯队，本地实测与第三方横评一致；
   换用"更新"的模型收益小于工程收益，风险大于收益。
4. VAD 上"新模型更好"的结论**不成立**，已用本地实测否掉（见第四节）。

## 二、同类项目第一梯队

| 项目 | 覆盖段 | 值得我们看的地方 |
| --- | --- | --- |
| modelscope/FunASR | 识别 + 分离 | `vad_model` / `spk_model` 一条链返回带说话人的 VAD 段；`merge_vad` 合并碎片、`max_single_segment_time` 限单段时长 |
| pyannote/pyannote-audio | 分离 + 评测 | 已在用；pipeline 可组合（分割 + embedding + 聚类），progress hook 可挂进度 |
| m-bain/whisperX | 识别 + 对齐 + 分离 | VAD 既抑制幻觉又当 batch 边界；2026 的 Context-Aware Batching 在批内保留前文，改善标点与专名 |
| k2-fsa/sherpa-onnx | 全链路 + 流式 | 单二进制内含 VAD/ASR/分离，流式与非流式同一套 API（本项目不做，理由见第五节） |
| MahmoudAshraf97/whisper-diarization | 分离 + 识别 | faster-whisper 批推理 + NeMo MSDD；`--suppress_numerals` 把数字转读法提升对齐精度 |
| QwenLM/Qwen3-ASR | 识别 + 对齐 | 对齐器以参数挂进 batch 推理，一次返回词/字级时间戳；单次 ≤5 分钟窗口 |
| juanmc2005/diart | 在线分离 | 增量聚类替代全量重聚，长音频跨块 ID 抖动更小 |
| ufal/whisper_streaming | 流式识别 | LocalAgreement 增量确认 + 缓冲裁剪策略 |
| Zackriya-Solutions/meetily | 记录 + UI + 纪要 | 本地转写 + Ollama/OpenAI 兼容端点出纪要；"导入已有录音补增强" |
| rishikanthc/Scriberr | 记录 + 队列 | 目录监视自动入队、任务队列带失败重试 |

补充：`fgnt/meeteval`（cpWER / tcpWER 标准实现）、`BUTSpeechFIT/DiariZen`（无 collar DER 与跨数据集超参）、
`jianfch/stable-ts`（VAD 抑制掩码 + 按标点重组）、`Rikorose/DeepFilterNet`（48kHz 实时降噪）。

## 三、本地语音模型格局（2026-11）

识别（中文会议为准）：

| 模型 | 定位 | 本项目态度 |
| --- | --- | --- |
| Qwen3-ASR-1.7B | 多语言主力，自带标点与语种识别 | 保持默认 |
| FireRedASR2-AED | 中/英/粤与方言更强，原生字级时间戳+置信度 | 保持备选（`--asr-engine aed`） |
| Fun-ASR-Nano-2512（0.8B） | 远场高噪优化，原生 CTC 字级时间戳 | 备选，未接入（需 ModelScope 权重与 CTC 张量） |
| SenseVoiceSmall | 极速档，30 秒上限 | 不采纳（精度落后） |
| Whisper large-v3 / Parakeet / Canary | 中文弱或无中文 | 不采纳 |
| MOSS-Transcribe-Diarize（0.9B） | 端到端转写+时间戳+说话人，长上下文不分块 | 观察，接入成本高（新框架） |
| GLM-ASR-Nano-2512（1.5B） | 中英粤，低音量鲁棒 | 观察，许可未核实 |

分离：pyannote community-1（现用）在公开横评里不是最高分，但**可离线、可本地、有既有集成**；
Nemotron 3 Diarization、DiariZen WavLM 分数更好，代价是 NeMo 全家桶与本机 Windows 安装风险。

对齐：Qwen3-ForcedAligner-0.6B（11 语言）优于 WhisperX wav2vec2 与 stable-ts 方案；本机尚未下载，
`--align-engine auto` 现在回落 fa-zh（中文专用，够用但不能用于非中文）。

## 四、本地实测：否掉"换 FireRedVAD 当默认"

调研建议把 VAD 换成 FireRedVAD（公开报告 F1 97.57 vs fsmn-vad 90.91）。本地实测不支持这个结论：

| 指标（同一段 25 秒双人合成素材） | fsmn-vad | FireRedVAD |
| --- | --- | --- |
| VAD 切出的语音段 | 4 段 | 9 段 |
| 语音总时长 | 25.7s | 23.1s |
| VAD 耗时（含模型加载） | 8.49s | 0.37s |
| cam++ 分离 DER | **4.53%** | 7.23% |

解释：分离链路里 VAD 段就是声纹聚类的单元。FireRedVAD 边界更贴真实停顿，但段被切碎后
每段声纹更不稳，聚类粒度变差，DER 反而上升 2.7 个百分点。它真正的优势在**纯分段**场景
（`--no-diarize` 长音频只用 VAD 定识别批边界），那里是 20 倍速度差。

因此 v2.5 的 `--vad auto` 是**按用途选后端**，不是"有新模型就换"：
分离链路 fsmn、纯分段链路 FireRedVAD（装了才换），显式传值时不改写用户选择。

## 五、明确不采纳

1. **全面转 ONNX / C++（sherpa-onnx 路线）**：收益在边缘设备与单二进制分发，本项目是单机 GPU 管线，
   重写成本高、收益低。
2. **为流式重构主干**：离线批处理的标点、ITN、说话人质量都更高。要"边录边转"应当叠加增量层，
   不重写主干。本轮不做。
3. **用 LLM 替代 ITN / 纠错正则**：不可复现、逐次漂移；正则 + 词典 + 可回归基准更适合这个位置。
4. **换 NeMo / Sortformer 做分离**：Windows 装 NeMo 风险高，公开分数优势不足以抵消集成成本。
5. **双引擎结果融合（ROVER / LLM 纠错）**：成本翻倍，且在噪声域可能反而拉高 WER；
   当前"热词偏置 + 确定性纠错词典"是更便宜也更可控的路径。留观察。

## 六、本轮已落地（v2.5）

| 项 | 依据 |
| --- | --- |
| `--vad auto` 按用途选后端 | 第四节实测 |
| 字幕硬切兜底 + `--min-dur` / `--max-cps` 阅读速度 | 丢标点的长句原本会出一条几十字的字幕；语速快时 20 字只占 1.2 秒 |
| `--rttm` 说话人日志 | 外部复核与论文对齐需要标准格式，成本只是几行文本 |
| 会议原文「发言概览」 | 纪要提炼者需要知道谁占多少发言、谁只说了两句 |
| 无容差 DER + cpWER | 单看容差 DER 分不清"更准"和"边界更粗"；cpWER 才贴近"稿子能不能直接用" |

## 七、后续观察清单

- Qwen3-ForcedAligner 下载后，非中文素材的字幕质量
- MOSS-Transcribe-Diarize 在长会议上的实际 cpCER（端到端省掉分离环节的可能性）
- Fun-ASR-Nano 在远场/高噪真实会议上的表现（合成素材测不出来）
- 说话人时间轴可视化（HTML 泳道图）——当前只有 RTTM 与文字概览
