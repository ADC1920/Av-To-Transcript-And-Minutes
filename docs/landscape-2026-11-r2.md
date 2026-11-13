# 2026-11 第二轮调研：同类项目与模型格局复核

> 目的：在 v2.6 落地 P0/P1 七项之后，再看一遍外部有什么新东西值得吸收。
> 方法：两路独立调研（开源项目全景 + 模型格局）+ 对每条关键论断回代码核实。
> 收录标准：结论必须能指到代码行或一手来源（官方模型卡 / README / release notes）。
> 与前一轮的关系：`landscape-2026-11.md` 与 `optimization-roadmap-2026-11.md` 的结论**仍然有效**，
> 本文只记增量、纠正与新的否决项。

## 一、结论先行

1. **模型层不用换主力**。两份独立调研都指向同一结论：中文第一梯队仍是 Qwen3-ASR-1.7B 与 FireRedASR2-AED。
   2026 下半年新出的中文强模型要么更大（8B+）、要么不带时间戳、要么闭源。
2. **有一处文档与行为不符，属于隐私承诺问题**：pyannote 遥测默认开启，会上报 `https://otel.pyannote.ai/v1/traces`，
   而本项目 README 首行写「完全离线」。这是本轮**唯一确认为缺陷**的项，修复成本一行。
3. **有一个零代码的收益（但本轮实测发现代码有缺陷）**：`--align-engine native` 的代码路径看似完整
   （`aligner_available()` / `load_aligner()` / `align_batch_qwen()`），实际有三层缺陷，
   最后一层（权重键名 + 隐藏维度与 transformers 实现不匹配）需引入 `qwen-asr` 包才能解决。详见第三节 P1。
4. **「置信度已有但没接出去」成立**：AED 返回的 `confidence` 在二分重试合并时用到（`merge_aed_results`），
   但**没有写进 `lines`**，下游无法据此判断哪段需要复核。
5. **「AED 60 秒上限未处理」不成立**：`AUTO_SEGMENT_S = 60` 与 `split_long_segments()` 已实现自动分段，
   调研所指的问题在本项目里已不存在。此类外部结论必须回代码验证后再采信。

## 二、核实结果：调研论断对账

对两份调研提出的关键论断逐条回代码核对，结论分三类。

| 论断 | 核实结果 | 证据 |
| --- | --- | --- |
| pyannote 默认开启遥测 | **成立**（且默认恰为 true） | `pyannote.audio.telemetry.metrics.CONFIG["metrics_enabled"]` 实测为 `True`；全仓无 `PYANNOTE_METRICS_ENABLED` 设置 |
| 无响度归一化 | **成立** | 全仓 grep `loudnorm` / `EBU` / `R128` 零命中 |
| `confidence` 未进产物 | **成立** | `qwen_asr.py:287` 收集、`:1141` 在二分合并时用，但 `lines` 构造（`:2407`）字段只有 `spk/spk_name/start_ms/end_ms/text/language` |
| `--align-engine native` 代码已完整只差权重 | **成立** | `aligner_available()` `:732`、`load_aligner()` `:737`、`align_batch_qwen()` `:750`；`ALIGNER_MS` 目录在本机不存在 |
| 无目录监视 | **成立** | 全仓 grep `watchdog` / `--watch` 零命中 |
| 无 HTTP API | **成立** | 全仓 grep `FastAPI` / `uvicorn` / `/v1/audio` 零命中 |
| 索引无向量 | **成立** | `tools/meeting_index.py` 无 `embedding` / `vec` 相关表 |
| 方言无法显式声明 | **成立** | `--asr-engine auto` 路由键只有 `zh/en/yue`（`qwen_asr.py:1915`），无方言开关 |
| **AED 60 秒上限未处理** | **不成立** | `AUTO_SEGMENT_S = 60`（`:71`）+ `split_long_segments()`（`:1037`）已实现；`:2149` 处按时长自动分段 |
| **本项目「完全离线」** | **部分不成立** | 模型均本地运行属实，但 pyannote 遥测默认外发 |

## 三、值得吸收的增量（按投入产出比）

### P0：改一行，消除文档与行为的矛盾

**关闭 pyannote 遥测**

README 首行承诺「完全离线：模型全部本地运行，无 API 密钥、无网络依赖」。实测 pyannote 遥测默认开启，
上报内容含文件时长、`num_speakers` / `min_speakers` / `max_speakers`、pipeline 类名，目标端点
`https://otel.pyannote.ai/v1/traces`。

修法：在设置 `PYANNOTE_DIR` 等环境变量的同处加一行 `env.setdefault("PYANNOTE_METRICS_ENABLED", "0")`，
`qwen_asr.py` 顶部也补一处（它可能被独立调用）。

验收：`python -c "import os; os.environ['PYANNOTE_METRICS_ENABLED']='0'; from pyannote.audio.telemetry import metrics; print(metrics.CONFIG)"` 且断网跑一次转写无外发尝试。

### P1：零代码收益

**下载 Qwen3-ForcedAligner-0.6B**

代码路径已完整，`--align-engine auto` 已写好「有就用、没有回落 fa-zh」。缺的只是 1.2GB 权重。

依据（官方模型卡，AAS 毫秒越低越好）：中文 33.1ms，对比 NFA 109.8ms、Monotonic-Aligner 161.1ms；
300 秒长音频仍 36.5ms，而 WhisperX 在此崩到 2708.4ms。11 语言覆盖，正好补上「非中文素材字幕错位」。

**2026-11-13 实测更新：此路当前走不通，代码路径并非「已完整」**

下载完成（1.84GB，11 个文件，`aligner_available()` 返回 True）后实跑暴露三层问题：

| 层 | 症状 | 根因 |
| --- | --- | --- |
| 1 | `padded_feature_length` 必须是 100 的倍数，实得 853 | processor 的 `pad_to_multiple_of` 为 None，需自行按 `n_window*2` 补零 |
| 2 | `Input type (float) and bias type (BFloat16) should be the same` | 特征提取输出 float32，模型 bf16，对齐路径漏了 `.to(model.dtype)` |
| 3 | **权重键名与维度双重不匹配** | checkpoint 键为 `thinker.*` 前缀，且 hidden size 是 **1024**；transformers 5.16.1 的 `Qwen3ASRForConditionalGeneration` 按 config 建出 **2048** 维，`lm_head` 期望 151936 而权重是 5000 |

前两层已修（补零 + dtype 转换，见 `align_batch_qwen`），第三层**无法靠改调用绕过**：0.6B 对齐器的宽度就是 1024，
而 transformers 侧的 Qwen3-ASR 实现假定 1.7B 的 2048。`Qwen3ASRForTokenClassification` 需要 `score` 头，
该 checkpoint 只有 `lm_head`（5000×1024，5000 个时间桶 × 80ms ≈ 400 秒）。

结论：官方推荐的路径是 `qwen-asr` 包（PyPI 0.0.6，`forced_aligner=` 参数挂进 ASR 模型）。
要么引入该包，要么等 transformers 支持 0.6B 对齐器的宽度。当前状态是 **`auto` 会回落 fa-zh，不报错**，
所以下载了无害，只是没生效。

**2026-11-13 追加实测：`qwen-asr` 包不可引入**

查了包内容与依赖，三个否决理由：

1. **硬钉 `transformers==4.57.6`**（PyPI `requires_dist` 原文）。实测该版本 wheel 内**完全没有
   `qwen3_asr` 模块**，装它等于把项目主 ASR 路径的直接依赖降一个大版本并失去该模块。
2. **自带 backend 只是想绕开这个限制**（`qwen_asr/core/transformers_backend/modeling_qwen3_asr.py`），
   但它用 `@check_model_inputs()` 这种旧签名；transformers 5.16.1 的实现要求直接传函数，
   隔离测试报 `TypeError: check_model_inputs() missing 1 required positional argument: 'func'`。
   即两套 transformers 实现彼此不兼容，不存在"装了就两边都能跑"的组合。
3. **注册会劫持主路径**：它用 `AutoConfig.register("qwen3_asr", ...)` / `AutoModel.register(...)` /
   `AutoProcessor.register(...)` 覆盖全局注册。装进同一环境后，项目主 ASR 的
   `AutoProcessor.from_pretrained` 会被导向它的实现，风险外溢到当前正常工作的链路。

另外它硬依赖 `nagisa`（日文分词）与 `soynlp`（韩文），对一个中文优先的项目属于无谓负担。

**结论：P1 在当前依赖组合下不可达**。要真正用上需满足其一：transformers 上游支持 0.6B 宽度的
Qwen3-ASR 对齐器；或 `qwen-asr` 放宽 transformers 版本约束并与 5.x 兼容。
在此之前 `auto` 回落 fa-zh 是正确行为，中文素材不受影响，非中文素材字幕仍走均分兜底。

### P2：把已有信号接出去

**`lines` 增加 `confidence` 字段，并支持低置信段复核**

信号已在手但被丢弃。加上字段后可以做两件事：交付时标出可疑段；对低置信段换引擎重跑取高值。

这是「不说话人时间轴也能提升成稿可用性」的最低成本路径，且为后续人工校对提供排序依据。

验收：产物 json 的 `lines` 每条含 `confidence`；构造一段噪声素材，低置信段能被正确标出。

### P3：条件启用响度归一化

全仓无 `loudnorm`。远场/小音量是中文会议的高频场景，但**无条件启用会损坏已干净的素材**。

正确形态是条件触发：先测整体 RMS / 峰值，低于阈值才走 `loudnorm=I=-16:TP=-1.5:LRA=11`，且参数进缓存键。
不要做成默认全开。

验收：安静素材（人为衰减 20dB）归一化后 CER 下降；正常素材结果不变。

### P4：目录监视

同类项目（Scriberr、Buzz）都有「丢进文件夹自动跑」。本项目的幂等缓存判定已完整，
缺的只是触发器。用 `watchdog` 监听，文件大小稳定后调现有 `process_one()`。

### P5：方言显式声明

`--asr-engine auto` 只知道 `zh/en/yue`。方言素材会全落进 `zh` 走 AED，而 AED 在方言集上退化明显。

加 `--dialect` 参数做两件事：帮助用户明确预期；在文档里写清哪些方言 AED 覆盖。
不引入新模型。

## 四、明确不采纳（新增）

| 候选 | 理由 |
| --- | --- |
| 换 FireRedASR2-LLM（8B） | 相对 AED 仅提升 0.16pp（2.89 vs 3.05），代价是 8 倍参数、16GB 卡须量化、换引擎 |
| Fun-ASR-Nano-2512 | FireRed 复跑口径 Mandarin-4 4.55 / Dialect-19 15.07，明显落后在用引擎；且 HF 权重缺 CTC 张量，时间戳是固定 0.06s 步进的假值 |
| GLM-ASR-Nano-2512 | 中文自评数字只在 PNG 里不可提取（未核实）；无时间戳、无说话人 |
| MOSS-Transcribe-Diarize | AISHELL-4 联合 CER 14.84、cpCER 15.83，省掉分离的收益不显著；显存随时长线性涨，60 分钟以上需 32GB 级 |
| VibeVoice-ASR（7B） | AISHELL-4 WER 21.40，转写质量明显弱于现役引擎 |
| DiariZen | 分数确实优于 community-1，但权重 **CC BY-NC 4.0 禁商用**，且无 Windows 官方支持 |
| 3D-Speaker 换掉 pyannote | AISHELL-4 10.30 优于 11.7，但 AliMeeting 基本打平、AMI-SDM 与 DIHARD3 明显更差，净收益不到 1.5pp 且只在一个数据集成立 |
| Parakeet / Canary 全系 | 无中文 |
| Qwen3-ASR-0.6B 当默认 | 换的是吞吐不是精度，单任务管线用不上 |
| 111 个 MCP 工具 | 工具面过宽会失控，本项目 8 个 job 语义工具是更合理的水位 |
| LLM 语义断句替换规则断句 | 不可复现，且与既有「正则 + 词典 + 可回归基准」的取舍冲突；可作为可选层，不宜替换 |

## 五、待验证（条件触发再做）

| 项 | 触发条件 | 说明 |
| --- | --- | --- |
| Nemotron 3 Diarization | 出现「DER 不够用」的真实反馈 | NeMo-Speech.cpp 提供 Windows 原生构建，推翻了「Windows 装 NeMo 风险高」的旧结论（那是指 Python 版 NeMo）。但中文只有 AISHELL-4 上 2pp 优势（9.69 vs 11.7），AliMeeting 几乎打平，且标签口径不同会使现有 DER 基线不可比。**换之前必须做小样本对标** |
| 端到端 ASR+分离模型 | 显存升到 32GB 以上 | MOSS / VibeVoice 都在这一档才有意义 |
| 语音事件检测 | 用户明确要笑声/掌声标注 | CED-Base（86M，Apache-2.0）是成本合理的选择，但只有片段级标签，需自行滑窗；SenseVoice 官方自认事件分类弱于专用 AED |
| 语义检索 | FTS5 命中率不够用的真实反馈 | 现在 trigram + LIKE 对中文短查询已够用，向量检索缺真实查询日志支撑判断 |
| `torch.compile` | 有加速需求且 Windows 上 Triton 稳定 | Qwen 官方支持，但需实测；做成开关默认关 |

## 六、需要纠正的前轮记录

前轮 `landscape-2026-11.md` 第五节第 4 条写「换 NeMo / Sortformer 做分离：Windows 装 NeMo 风险高」。

该结论对 **Python 版 NeMo** 成立（官方支持矩阵明确不支持 Windows），但 **NeMo-Speech.cpp** 是独立 C++ 运行时，
提供 `scripts\windows\build.ps1` 与 `install.ps1`，已有第三方 Windows 11 实测。因此这条否决理由**只对旧形态成立**，
新形态应改为「待小样本对标」，而非直接否决。是否真的更好仍需实测，见第五节。
