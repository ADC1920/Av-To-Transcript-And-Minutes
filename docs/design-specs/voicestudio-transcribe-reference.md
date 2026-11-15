# VoiceStudio 转写环节参考分析与可执行方案

## 1 背景与结论摘要

调研对象为 debpalash/VoiceStudio（本地 AI 语音工作台，Electron + Python 后端，2026-11 当前 trending 项目）。其转写体系包含 WhisperX（默认）、Faster-Whisper、Parakeet TDT、FunASR、sherpa-onnx 等十余个引擎，以及 pyannote 与 native Sortformer 双说话人分离路径。核心转写逻辑集中在 `backend/services/segmentation.py`、`backend/services/asr_backend.py`、`backend/engines/whisper_cpp/` 等文件。

**结论**：VoiceStudio 的识别引擎选择（英文为主）与本项目（中文会议纪要）不重叠，直接替换引擎没有收益；真正值得借鉴的是它在「说话人与转写段对齐」「长任务容错」「校对辅助」三个环节的机制设计。经与本项目逐点对照，产出 5 项可执行优化（2 项高优先级、2 项中、1 项可选）与 4 项明确不引入的结论。

**许可证约束（前置红线）**：VoiceStudio 为 AGPL-3.0，本项目为 MIT。AGPL 代码不可复制、不可作为依赖引入（传染性），本方案所有条目均为借鉴机制后自行实现，不搬运任何上游代码。

## 2 VoiceStudio 转写体系概览

### 2.1 引擎目录（docs/feature-catalog.md）

转写：WhisperX（默认）、Faster-Whisper、MLX Whisper、PyTorch Whisper、Parakeet TDT / v3、Moonshine、FunASR、sherpa-onnx（实时听写）、OpenAI-compatible 自配服务器。说话人分离：pyannote 与 native Sortformer（GGUF，audio.cpp，限 120 秒）。字幕：词级时间戳驱动，卡拉OK ASS 硬字幕输出。

### 2.2 与本项目定位差异

VoiceStudio 的转写服务配音链路（分段目标时长 4.5 秒、按场景切换镜头重切），本项目服务于会议纪要（发言块最长 60 秒、按说话人组织）。同为「ASR + diarization + 词级时间戳」骨架，但优化目标不同：它追求配音节奏，本项目追求归属正确与稿件可读。参考时只取机制，不取参数。

## 3 值得引入的优化（按优先级）

### 3.1 P0：说话人感知重切分——已裁决不实施（前提不成立）

#### 3.1.1 参考来源

`backend/services/segmentation.py` 的 speaker-aware re-split 机制（其 issue #486、#3419）：先按句法与时长分组，diarization 之后对「一个段横跨多个说话人回合」的段，用词级时间戳找到说话人切换点重新切开。

#### 3.1.2 裁决：不实施（2026-11-15 实现前审计推翻原现状判断）

初版方案基于二手代码梳理推断「本项目发言块可能横跨两人」。实现前读透两条路径的合并链路后确认该前提不成立，证据三条：

1. **本项目是 diarization 先于分组**，与 VoiceStudio「Segmentation runs BEFORE diarization」（其源码注释原文）顺序相反。`merge_by_speaker()`（qwen_asr.py）的合并条件为「相邻且同说话人且间隔 ≤ gap」，pyannote 路径的碎段合并同样有 `merged_lab[-1] == lab` 同人条件——两条路径的发言块在本项目架构上不可能跨人。
2. **交叠区无字级证据可用**：pyannote 对重叠语音输出两条并行的回合（各自成段各自转写、各自归属），与 VoiceStudio resplit 的「多数重叠说话人」归属结果等价；字级归属在交叠区无法分辨谁说了哪个字（pyannote 只说两个 turn 同时成立）。
3. campp 路径的段内换人是 VAD 段级盲区，声纹证据只有段级，字级复核同样无证据可用；若未来要补，需要对超长段做子段声纹突变检测（新机制，非 VoiceStudio 方案），暂不做（默认会议模式走 pyannote，回落路径收益有限）。

结论：放弃 P0。原方案的验收标准（单人素材字节级不变）在放弃后自动满足——不改就是不变。

### 3.2 P1：转写分批断点续跑——中途失败不再从头重转

#### 3.2.1 参考来源

`backend/services/incremental.py` 的 segment_fingerprint：每段生成输入做确定性哈希，成功后存储哈希；重跑时只重做哈希变化的段。VoiceStudio 用它做配音的增量重渲，机制可平移到转写分批。

#### 3.2.2 本项目现状与差距

本项目缓存粒度是整素材一个 JSON（`av_to_transcript_and_minutes.py` 的 `asr_cache_name():470`），转写成功才落盘。AED 分批推理已有确定性批计划（`aed_batch_plan():1113`，段数与总时长双上限）与 OOM 拆批（`aed_transcribe_oom_safe():1179`），但 2 小时会议在 90 分钟处 OOM 或断电，全部批次作废重转。

#### 3.2.3 可执行方案

1. 落点：`qwen_asr.py` 的批推理循环（约 2374 行 `infer()` 调用处）。
2. 中间落盘：每批完成即追加写缓存目录下 `{素材指纹}.partial.json`，内容为批计划哈希、批次索引、该批转写结果。批计划哈希由 `aed_batch_plan` 输入（分段与参数）算出，参数或分段变了旧 partial 自然失效。
3. 续跑逻辑：开转写前先读 partial，批计划哈希一致则跳过已完成批次，只转剩余批；全部完成后组装并写正式缓存 JSON，partial 删除。
4. 防误用：沿用现有 `audio_fingerprint():510`（首尾 1MB sha256）校验素材未被替换；partial 写入走临时文件加 `os.replace`，与正式缓存同纪律。
5. 范围控制：只对本地 AED / Qwen 批量路径生效，云端 filetrans 自带断点（任务轮询），不需要。

#### 3.2.4 实施结果（2026-11-15 已落地）

实现为 `qwen_asr.py` 的 `PartialBatches` 类 + `infer()` 批循环改造，与方案的差异两处（均由实测发现）：

1. **partial 文件放素材同目录而非缓存目录**——引擎的 work_dir 是随机临时目录（进程退出即清），放那里续跑拿不到。
2. **素材指纹锚定 to_wav16k 之前的真实输入（src0）**——AED 引擎会先把源文件重编码到临时目录的 16k wav，同一输入每次编码的字节存在差异，挂在它上面续跑永不命中（实测踩到后修正）。批计划键用分段边界序列（VAD/聚类确定性）而非临时 wav 路径。

验证：单测 8 断言（存取/复用/清除/计划变化失效/素材变化失效）+ 真实管线冒烟（8 分钟素材预置批 0 标记结果，管线复用批 0 整批 8 段、段 8 起真实转写、完成后 partial 删除）。默认启用，`--no-resume` 关闭。

### 3.3 P1：低置信度段落标记——把人工校对注意力放到刀刃上

#### 3.3.1 参考来源

`backend/engines/whisper_cpp/output.py`：解析 whisper.cpp 的 `--output-json-full`，把每个 BPE token 的概率合并到词级，低置信度词可用于后续校对提示。VoiceStudio 把概率一路带到字幕与配音 QC（dub_qc）。

#### 3.3.2 本项目现状与差距

置信度采集端已部分落地：2026-11-13 的改动已把 `confidence` 字段加进 `qwen_asr.py` 的 units 与 lines（仅 AED 引擎有值，实测 0.991 到 0.997；Qwen 与云端路径不写该键）。差距在消费端：稿件组稿与 docx 渲染不读该字段，用户看不到；Qwen3-ASR 主力路径与云端路径没有置信度，大多数真实会议素材（Qwen 路径）信号仍是空白。

#### 3.3.3 可执行方案

1. 采集补齐：Qwen3-ASR 路径在 `infer()` 取生成序列平均 log 概率（transformers `output_scores`，实现时确认该版本 API）写进同一 `confidence` 字段；云端路径有则透传，无则置 null 不参与。
2. 消费端：主脚本 `transcribe_and_write_article():988` 组稿时，`confidence` 低于阈值（默认 0.6，`--low-conf-mark` 可调，`--no-conf-mark` 关闭）的段落行尾加「【待校对】」标记；docx 渲染（`render_docx():968`）给该行加灰色底纹。
3. 缓存兼容：纯新增可选字段不需要升 `SCHEMA_VERSION`（2026-11-13 已有该判断先例：升版本会让旧缓存全失效重转，代价与收益不成立）；消费端按字段缺失即不标记处理。
4. 注意：置信度只做提示，不改文本，不得自动删改低置信度内容；AED 实测置信度普遍 0.99 以上，阈值 0.6 需在真实含噪素材上校准。

#### 3.3.4 实施结果（2026-11-15 已落地）

实现与方案一致，另发现并修复一个方案外的缺口：`--no-diarize` 长音频分支组装 payload 时只拷贝 text 与 language，confidence 在 parsed 里存在却被丢弃——已补上（segments 与 units 同步携带）。

验证：Qwen 路径 8 分钟素材 144 段全带 confidence（0.9954-0.9998，generate output_scores 逐步 log softmax 几何平均）；meeting 模式冒烟——默认阈值 0.6 精确命中实测 0.398 的低置信段（「我。」单字短句）且无误报，阈值 0.999 时 5 行全标，旧缓存缺字段不标记（单测 4 断言）。`--low-conf-mark`（默认 0.6）与 `--no-conf-mark` 只改渲染不进缓存键。范围说明：标记只进会议原文（行结构保留）；转写文稿是拼段成文的结构，不适合行级标记，不做。

### 3.4 P2：说话人归属回退链补齐——已审计关闭（无需补齐）

#### 3.4.1 参考来源

`segmentation.py` 的 `_label_from_turns()`（其 issue #3419）：短句完全落在 diarization 回合间隙时，旧实现沿用 diarization 之前的标签（属于另一套编号，可能张冠李戴）；修复后按「多数重叠、中点、最近回合」三级回退。

#### 3.4.2 审计结论（2026-11-15）

该问题在本项目不存在：本项目不把「外部分组」对到 diarization 回合上——pyannote 路径的 segments 直接由 turns 构造（每回合自带标签，段即回合），campp 路径的段与聚类标签一一对应。不存在「段落在回合间隙沿用旧标签」的场景，VoiceStudio #3419 修复的正是「分组先于 diarization」的顺序问题，本项目顺序相反，天然免疫。本项关闭，无需补齐。

### 3.5 P3（可选）：sherpa-onnx CPU 轻量通道

#### 3.5.1 参考来源

VoiceStudio 用 sherpa-onnx zipformer 做实时听写（`backend/services/sherpa_dictation.py`），证明 sherpa-onnx 的 ONNX int8 路径在其多引擎体系里稳定可用。

#### 3.5.2 适用条件与方案

本项目本地主引擎 Qwen3-ASR-1.7B bf16 依赖 GPU，无 GPU 机器几乎跑不动。sherpa-onnx 跑 SenseVoice-Small int8（中文友好，CPU RTF 远小于 1）可作为「无 GPU 机器批量跑」或「快速预览稿」的可选引擎：`--asr-engine sherpa` 新增分支，16k wav 进，带时间戳文本出，说话人分离复用现有 campp 链。依赖新增 `sherpa-onnx`（uv 管理）。此项目仅在确认确有无 GPU 批量场景后再做，工作量约 200 行；无此场景则不做（YAGNI）。

## 4 明确不引入的部分

### 4.1 NLMS 回声消除（aec.py）

其 AEC 服务于「边播边录」场景，需要远端播放信号作参考；离线会议录音没有参考信号，机制不适用。且本项目已有 ZipEnhancer 增强轨道与 demucs 分离。

### 4.2 native Sortformer（GGUF）

依赖 audio.cpp 生态与 GGUF 模型管理，上限 4 人且不支持指定人数、限 120 秒；本项目 pyannote（原生处理重叠）加 campp（声纹修复）已覆盖中文会议场景，收益为负。

### 4.3 whisper.cpp / faster-whisper 引擎替换

whisper 系对中文的准确率弱于现役 Qwen3-ASR 与 FireRedASR2，替换只有编译期依赖收益、没有质量收益。其 `output.py` 里「UTF-8 码点被拆跨 token 时丢弃词级文本、保留段文本防乱码」的防御细节，在本项目自研解析器时值得记住，但不构成引入理由。

### 4.4 配音链路的分段参数

segmentation.py 的 MIN_DUR 1.5 秒、IDEAL_DUR 4.5 秒、MAX_DUR 9 秒与场景切换重切，全部面向配音节奏；会议纪要的块长（60 秒）与组织方式（按说话人）目标不同，参数体系不可搬。

## 5 实施结果与总验收（2026-11-15）

1. P0：裁决不实施（3.1.2 三条证据）；P2：审计关闭（3.4.2）。
2. P1 断点续跑：已落地（3.2.4），默认启用，`--no-resume` 可关。
3. P1 低置信度：已落地（3.3.4），`--low-conf-mark` 默认 0.6，`--no-conf-mark` 可关。
4. P3 sherpa-onnx：未做（无 GPU 批量场景未确认，YAGNI）。
5. 总验收：py_compile 两脚本、引擎 8 组单测 + tools 35 项全 PASS（新增 test_conf_mark 4 项、test_partial_batches 8 项）、质量基线 ALL PASS（DER/CER 门槛不回退）、e2e ALL PASS、断点续跑与置信度渲染各自真实管线冒烟通过。
6. 回滚：三项改动集中在 qwen_asr.py 与 av_to_transcript_and_minutes.py，均为新增类/新增字段/新增参数，可独立 revert；两个默认开启的行为各有显式关闭参数。
7. 改动文件：`skills/funasr-transcribe/scripts/qwen_asr.py`（PartialBatches、infer 批级续跑、Qwen output_scores 置信度、no-diarize 分支 confidence 透传）、`av_to_transcript_and_minutes.py`（meeting_md 标记、两个新参数、opts 透传）、`skills/funasr-transcribe/scripts/test_v25_upgrade_units.py`（两组新单测）。

## 6 调研证据索引

1. 引擎目录：VoiceStudio `docs/feature-catalog.md`（Transcription 与 Speech generation 两节）。
2. 说话人重切分：`backend/services/segmentation.py`（resplit_segments_by_diarization、_word_attribution、_fill_and_smooth、_keeps_one_word_turn、_label_from_turns、deduplicate_chunk_segments）。
3. 增量指纹：`backend/services/incremental.py`（segment_fingerprint、plan_incremental）。
4. 置信度解析：`backend/engines/whisper_cpp/output.py`（parse_transcript、_words）。
5. AEC 与 Sortformer：`backend/services/aec.py`、`backend/services/diarization_native.py`（评估后排除）。
6. 本项目侧行号以 Explore 子代理 2026-11-15 梳理报告为准（av_to_transcript_and_minutes.py、skills/funasr-transcribe/scripts/qwen_asr.py）。
