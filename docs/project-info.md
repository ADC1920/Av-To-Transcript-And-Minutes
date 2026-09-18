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

