---
name: funasr-transcribe
description: "本地音视频转文字（离线、无需 API 密钥）：录音/音频/视频转文字、会议访谈与会议录像转写、说话人分离（谁在何时说了什么）、字幕 SRT/VTT 生成（字级时间戳）、专名纠错、无标点文本恢复标点、录音情绪分析。主力 Qwen3-ASR-1.7B + FunASR VAD/声纹，备选 FireRedASR2-AED 与阿里云百炼云端引擎。Use whenever the user mentions transcribing a recording, speech-to-text, audio-to-text, video-to-text, meeting or interview transcription, subtitles/SRT/VTT from audio or video, speaker diarization, punctuation restoration, proper-noun correction, or emotion analysis of audio — even if they just drop an audio or video file and say 转一下 or 整理成文字."
---

# FunASR / Qwen3-ASR 本地语音转写

> **本副本归属 ZCode 侧仓库**（`skills/funasr-transcribe/`，随仓库分发；ZCode 会话经技能钩子加载本副本）。
> 脚本真源在本仓库根 `av_to_transcript_and_minutes.py` 与 `skills/funasr-transcribe/scripts/`（2026-11-02 自 `Daily Use/publish/av-to-transcript-and-minutes` 整体移入项目根）。
>
> **本机（2026-11-02 起）只走云端识别**：识别环节统一用阿里云百炼 `--engine cloud`，不加载本地 ASR 大模型。本机默认值由环境变量 `ASR_ENGINE=cloud` 提供（管线 `--asr-engine` 默认读它），密钥走 `DASHSCOPE_API_KEY`。**本地仍保留说话人分离**（fsmn-vad 切段 + cam++ 声纹聚类，依赖 torch/funasr/sklearn），会议与访谈原文必须靠它出说话人标签。依赖装在项目内 `.venv`（Python 3.12，uv 管理）。
> 注意：本机未装 pyannote / demucs；`--meeting` 走 campp 分离，人声分离环节需另装 demucs 或加 `--no-separate`。

把录音、视频转成文字的本地工具箱，完全离线、无需 API 密钥。依赖装在项目内 `.venv`，实际版本以 `.venv/Scripts/python.exe -c "import torch,funasr,transformers;print(torch.__version__,funasr.__version__,transformers.__version__)"` 为准（2026-11-12 实测 torch 2.14.1+cu130、funasr 1.4.19、transformers 5.22.1）。模型缓存在 `~/.cache/modelscope/models/`。本地模型推理走 GPU（RTX 4060 8GB，torch cu130 版，`--device` 未显式指定时自动选 `cuda:0`）。

## 快速决策（按需求选脚本）

| 用户需求 | 用法 |
|---------|------|
| **视频/音频一键出文稿**（拆轨 + 人声分离 + 说话人分离转写 + 规范 docx） | `python <技能目录>/scripts/av_to_transcript_and_minutes.py <视频或音频>`（见下节） |
| 视频/会议要带时间戳与说话人标签的原文 | 上条加 `--meeting`（出会议原文底稿，正式总结再按 meeting-notes-expert 提炼；会议场景为会议纪要） |
| **日常转写（首选）**：单人或不需要区分说话人 | `python <技能目录>/scripts/qwen_asr.py <音频文件>` |
| 会议/访谈，要区分"谁说了什么" | `python .../qwen_asr.py <音频文件> --diarize` |
| 要字幕文件（SRT，可带说话人标签） | 加 `--srt`（fa-zh 强制对齐，字级时间戳） |
| 专有名词/人名总听错 | 加 `--replace "错词=>对词"`（确定性替换） |
| 录音嘈杂/带背景音乐 | 加 `--denoise`（ZipEnhancer 前处理，见约束 5） |
| **免显存云端引擎（付费,准确率更高）** | `python .../qwen_asr.py <音频> --engine cloud` （默认 qwen-audio-3.1-asr-message,整文件直出时间戳;需 DASHSCOPE_API_KEY;详见下方 cloud 引擎节） |
| 只要 FunASR 原生通道（对照/低显存兜底） | `python <技能目录>/scripts/transcribe.py <音频文件>` |
| 给无标点的转写文本补标点 | 见 references/advanced.md 的 ct-punc 节 |
| 分析录音/某段音频的情绪 | 见 references/advanced.md 的 emotion2vec 节 |

`<技能目录>` = 本 SKILL.md 所在目录。脚本直接用 `python` 跑，无需安装任何东西。

## 主力脚本 qwen_asr.py（双引擎，Qwen3-ASR 默认 / FireRedASR2-AED 可选）

识别引擎二选一，VAD/声纹/字幕/纠错各引擎通用：

| 引擎 | 参数 | 强项 | 弱项 |
|------|------|------|------|
| Qwen3-ASR-1.7B（默认） | `--engine qwen` | 多语言（含**日文**）、自带标点、自带语种识别 | 中文偶有同音误听 |
| FireRedASR2-AED | `--engine aed` | 中/英/粤准确率更高、原生字级时间戳+置信度 | **不支持日文**、输出无标点（脚本自动用 ct-punc 补）、只吃 wav（脚本自动转） |

实测对照（2026-09-14，同素材）：zh「开放时间」AED 与 Qwen 均对；en AED 拼对
"chieftain" 而 Qwen 误听为 "chief then"；粤语 AED 对、Qwen 对；**日文 Qwen 正确、
AED 输出中文乱码**；噪声音频 AED 明显更稳。速度（稳态）AED 约 2.7ms/秒音频（如 43s 音频
≈0.2s），Qwen 约 10-17× 实时；AED 首调有 5-8s 预热。

**AED 字级时间戳依赖 CUDA 版 torchaudio**（2026-11-12 本机验证）：AED 的 `return_timestamp=True`
走 FireRedASR2S 的 `get_token_timestamp_torchaudio`，内部调 `torchaudio.functional.forced_align`。
若 torchaudio 是 CPU 版而模型在 GPU 上，该算子会抛 `NotImplementedError`（CUDA backend 无此算子），
而调用处是裸 `except:`——异常被吞掉、`timestamp` 变成 `None`，字级时间戳静默退化成段级估计。
本机已装 torch 2.14.1+cu130 + torchaudio 2.11.0+cu130，两者版本号不同（官方 cu130 索引的
torchaudio 最高只到 2.11.0），实测 ABI 兼容、`forced_align` 在 CUDA 上正常返回。换 torch 版本时
必须同步确认这一点，验证方法：`torchaudio.functional.forced_align` 传 CUDA 张量不报错。
另注意 bf16 输入会报错（`firered_mem_patch` 已把 CTC 输出 cast fp32，勿删那段）。

```bash
python qwen_asr.py 会议录音.m4a                    # 整段转写（自动语言检测；>60s 自动分段提速）
python qwen_asr.py 会议录音.m4a --srt              # 出 .srt 字幕 + 结构化 JSON
python qwen_asr.py 采访.m4a --diarize --srt        # 说话人分离 + 带说话人标签的字幕
python qwen_asr.py 采访.m4a --diarize --names "0=张三,1=李四"   # 说话人改名（先听一段确认谁是谁）
python qwen_asr.py 录音.m4a --replace "小蜜=>小米"  # 专名纠错（可加 --fuzzy 走拼音模糊）
python qwen_asr.py 录音.m4a --engine aed            # 换 FireRedASR2-AED（中英粤更准；日文禁用）
python qwen_asr.py 嘈杂.m4a --denoise              # 含 BGM/强噪前处理
python qwen_asr.py 录音.m4a --vad firered           # 强制 FireRedVAD 切段（默认 auto：分离链路 fsmn、纯分段 firered）
python qwen_asr.py 录音.m4a --rttm                 # 额外出 .rttm 说话人日志（pyannote.metrics/meeteval 可直接读）
python qwen_asr.py 录音.m4a --srt --max-cps 9      # 字幕按 9 字/秒补足展示时长（默认 12）
python qwen_asr.py 长录音.m4a --hotwords "十三希诺,ZCode"   # 热词偏置（prompt 层，弱于 --replace）
```

输出：stdout 全文；音频同目录落 `.json`（结构化：segments / sentences）；`--srt` 时额外落 `.srt`。
其余参数：`--language zh` 强制语言、`--threshold 0.7` 聚类阈值、`--merge-gap 800` 同人碎段合并间隔（ms）、
`--max-line 28` 单条字幕最大字数、`--device cpu`、`--no-t2s` 关繁转简、`--replace-file` 用词典文件。
输入为 m4a/AAC 等 libsndfile 读不了的容器时，入口自动用 ffmpeg 转 16k mono wav 再进链路（stderr 提示 `[0]`；mp3/wav/flac 原生可读不受影响）。

### cloud 引擎（--engine cloud,阿里云百炼 API,2026-10-07 接入并 E2E 验证）

| 子模型 | --cloud-model | 通道形态 | 适用 |
|--------|---------------|----------|------|
| qwen-audio-3.1-asr-message | `message`（默认） | dashscope SDK WebSocket;非 diarize 时整文件一次调用 | 日常转写首选:整文件直出句级+**字级**时间戳,免本地 VAD/对齐;diarize 时逐段并发(4 线程)+本地声纹聚类,字幕复用云端字级时间戳跳过 fa-zh |
| qwen3.8-omni-flash | `omni` | OpenAI 兼容;B64 直传,逐段 | 全模态模型兼职转写(无时间戳);默认关思考模式输出纯文本 |
| qwen-audio-3.1-asr-filetrans | `filetrans` | 异步整文件,**仅公网 URL**（--cloud-url） | 超长音频一键转写,云端说话人分离+字级时间戳;位置参数仅作输出锚点 |

```bash
python qwen_asr.py 录音.m4a --engine cloud               # message 默认:整文件一次调用(免 VAD/对齐,快)
python qwen_asr.py 录音.m4a --engine cloud --diarize --srt  # diarize 时走逐段+本地声纹聚类,字幕按 --max-line 细分
python qwen_asr.py 录音.m4a --engine cloud --cloud-model omni   # omni 全模态通道(无时间戳)
python qwen_asr.py 锚点.wav --engine cloud --cloud-model filetrans \
       --cloud-url "https://.../meeting.mp3" --diarize --srt
python av_to_transcript_and_minutes.py 录像.mp4 --asr-engine cloud --no-diarize  # 单人素材:跳过说话人分离(走整文件,最快)
python av_to_transcript_and_minutes.py 录像.mp4 --asr-engine cloud  # 管线走云端(默认带说话人分离)
```

- 需要 `DASHSCOPE_API_KEY`（环境变量或注册表 HKCU\Environment,2026-10-07 已写注册表）;message 通道
  另需 `pip install dashscope`（omni/filetrans 通道不需要）。cloud 下 campp 说话人分离、--replace 纠错、
  --itn、--srt 全部照常复用;message/filetrans 的时间戳由云端提供(无需 fa-zh),omni 无时间戳。
- message 通道整文件单次调用无需本地 VAD,但设了 --diarize 时改走逐段+本地声纹聚类;
  --hotwords 全通道支持:message 走即时热词表（随请求传入 {词:4},免预创建;上限 50 条）,omni 走 prompt,filetrans 走参数。
- 接口格式经真实探针验证:message 走 dashscope.audio.asr.Recognition（WebSocket,本地 wav 直传）;
  omni 走 OpenAI 兼容 chat/completions（data:;base64,逐段）;filetrans 只收公网 URL（本地文件
  无 OSS 时不要用 filetrans,用默认 message）;三款均按量计费、每模型有 10 小时免费额度。
- 管线 `--no-diarize`:单人素材跳过说话人分离直接整段转写（更快;云端 message 将走整文件一次调用），与 --meeting 互斥，缓存独立（"不分人"键）。
- speaker_id 从 0 的整数,与本地 campp 编号一致,--names 语义相同。
- **本机云端优先用法**：`ASR_ENGINE=cloud` 已设为本机默认，直接跑管线即走云端，无需每次带 `--asr-engine cloud`。`--device` 默认自动选（CUDA 可用则 `cuda:0`，否则 `cpu`），本机装的是 cu130 版 torch，分离环节走 GPU，32 秒双人素材走 VAD + campp 约 13 秒完成（含模型加载）。
- **VAD 断句必须显式传阈值**：funasr 1.4.19 起 fsmn-vad 在未传 `max_end_silence_time` 时启用动态静音阈值，长语音下阈值逐步放宽，1.5 秒静音间隙切不开、整段被判成一段，下游「段数<2 跳过聚类」静默退化成单说话人。`qwen_asr.py` 已固定传 800ms 关闭该逻辑（实测 32 秒素材由 1 段恢复为 5 段）。

### transcribe.py — FunASR 原生通道（备选）

```bash
python transcribe.py 会议录音.m4a --json out.json
python transcribe.py 录音.wav --device cpu        # 无 N 卡
```

比 Qwen3-ASR 快、显存小，中文准确率略低；作为对照基线与轻量兜底保留。

### diarize.py — 旧版分离脚本（保留，逻辑同 qwen_asr.py --diarize）

```bash
python diarize.py 访谈录音.m4a --threshold 0.7
```

## 重要约束（实测踩坑，勿绕过）

1. **说话人分离走 qwen_asr.py `--diarize` 的手动分环实现**：不要用 AutoModel 的 `spk_model="cam++"` pipeline 参数（该路径在 funasr 1.x 上聚类失效，不同人全被标成同一个 spk=0）。
2. **说话人编号 ≠ 真实身份**：0/1/2 按出现顺序分配，交付时提醒用户听一段确认，再用 `--names` 固定。
3. **聚类阈值调节**：两人声音相近被并成一人 → 调低（0.65~0.7）；同一个人被拆成多人 → 调高（0.8）。脚本在簇间相似度贴近阈值时会提示。
4. **`--replace` 优于 `--hotwords`**：热词只是 prompt 偏置（ASR 对 prompt 服从度低，实测连繁简都改不动）；`--replace` 是识别后确定性替换，必中。同音异字用 `--fuzzy`（需 pypinyin + rapidfuzz；本机 rapidfuzz 已装、pypinyin 未装，用模糊匹配前先补装）。
5. **`--denoise` 是条件性收益**：实测合成白噪声场景**无改善甚至更差**（「开放时间」→「派班时间」）；仅在真实含 BGM/强噪素材上试，干净素材不加任何前处理最好。
6. GPU 上模型输出的 tensor 转 numpy 必须先 `.cpu()`；Windows 原生 Python 不认 Git Bash 的 `/tmp` 路径，临时文件用 `tempfile.gettempdir()`。
7. 字幕时间为字级（fa-zh 强制对齐），比 VAD 段级边界准；单条过长用 `--max-line` 调小。
8. **`--vad firered` 有两道坎**（2026-10-01 修）：① fireredvad 断言输入必须 16kHz，而链路给进来的是 44.1kHz 立体声，必须先重采样；② 还**必须落成 PCM_16 文件**再喂——它的 fbank 按 int16 数值范围取特征，直接传 float32(-1..1) 会让特征幅度小三万倍左右，概率趋近 0 且**不报错**，表现为静默判成「全非语音」（实测 probs max 0.0031 对比正确路径 0.9998）。改完实测切出 2 个语音段。
9. **ITN 用自写规则**：WeTextProcessing 依赖 pynini（Windows 只有源码包，需 MSVC Build Tools 编译，本机装不上），fun_text_processing 不在 PyPI，故由 `itn_zh.py` 自写零依赖规则；只转高置信模式，约数与非数量用法（一起 / 十分 / 三天打鱼 / 三五个）保持原样。单测：`python scripts/test_itn_zh.py`。
10. **声道分轨必须跳过 Demucs**：Demucs 按立体声混音做人声分离，会打破左右声道独立性，而分轨的全部价值就在于两路各自干净——`--split-channels` 已自动跳过分离与声纹聚类。
11. **同一目录内有多份待转写音频时，缓存键必须带 tag**：`asr_cache_name` 原先只按配置指纹命名，两个声道文件会命中同一份缓存、转出完全相同的文本（实测踩过）；现已加 `tag`（分轨用 `ch0`/`ch1`）。
12. **「素材是否本来就干净」无法用能量差判断**（对照实验证伪，勿再尝试）：纯人声 TTS 的人声与背景音差 15.1 dB，人声+440Hz 背景乐差 12.7 dB，只差 2.4 dB，判据区分不出有无背景。需要跳过分离时手动加 `--no-separate`。
13. **emotion2vec 只做情绪识别，不做音频事件检测**：它输出 8 类情绪（开心/难过/厌恶/中立/生气/惊讶/害怕/兴奋）的整段打分；**笑声、掌声这类事件它识别不了**，需要另装事件检测模型（如 AudioSet 系分类器），别把它当事件标注用。
14. **声纹库只在 campp 引擎下可用**：pyannote 路径不产出声纹向量，加了 `--speaker-db` 会跳过并提示；裸写或 `--speaker-db auto`=用音频同目录（链路场景为输出目录）speaker_db.json，有则复用无则建库、命中自动更新；显式路径建库仍用 `--speaker-db-save`；命中阈值默认 0.75（同人被认成新人就调低）。

15. **cloud 引擎的密钥与边界**：DASHSCOPE_API_KEY 走环境变量,回退注册表 HKCU\Environment（load_dashscope_key）,
    绝不写进代码/配置文件；message 通道需 dashscope SDK（缺失时友好报错）,其余通道零新依赖；
    filetrans 模式位置参数只作输出锚点（json/srt 落其旁）,真实音频源必须 --cloud-url 公网可达；
    cloud 引擎不加载本地大模型（load_model/load_aed_model 跳过）,显存占用≈0,但 campp/--replace 等本地组件按需照常加载。

## 环境自检

用户报告"转写失败/环境不存在"时先跑：

```bash
python -c "import funasr, torch; print(funasr.__version__, torch.cuda.is_available())"
```

- 正常输出 `1.4.x True` → 环境完好，排查音频文件本身（格式/路径）。
- ImportError 或 CUDA 不可用 → 按 references/advanced.md 的「环境安装/修复」节重装（含 RTX 50 系必须 cu128+ 的说明）。

## 详细参考

模型清单与下载 ID、fa-zh 强制对齐 / 专名纠错 / FireRedVAD / ZipEnhancer 的接口与实测数据、ct-punc / emotion2vec 调用代码、环境安装与修复步骤、完整踩坑记录 → 读 **references/advanced.md**（按需加载，不预读）。

## 全自动链路 av_to_transcript_and_minutes.py（视频/音频 → 文稿 → docx）

一条命令跑完：FFmpeg 拆轨（无损 copy）→ Demucs 人声/背景音分离 → agate 噪声门 → qwen_asr 恒走 `--diarize` 转写 → 组稿 → article-format 规范 docx。视频与音频通用，逐环节幂等可续跑。

```bash
python av_to_transcript_and_minutes.py 会议录像.mp4              # 默认出「转写文稿」docx（每 4 句一段）
python av_to_transcript_and_minutes.py 会议录像.mp4 --meeting    # 出「会议原文」docx（带 [分:秒] 时间戳与说话人标签）
python av_to_transcript_and_minutes.py 录音.m4a --no-separate    # 纯人声素材：跳过 Demucs 直接转写（更快）
python av_to_transcript_and_minutes.py 素材目录 -o 输出目录        # 批量；默认落 <输入>/separated_out
python av_to_transcript_and_minutes.py 录像.mp4 --names "0=张三,1=李四" --srt
python av_to_transcript_and_minutes.py 录像.mp4 --asr-engine aed  # 中/英/粤更准（日文禁用；换引擎自动重转）
python av_to_transcript_and_minutes.py 录像.mp4 --asr-engine auto --language zh  # 按语言路由：中/英/粤走 aed
python av_to_transcript_and_minutes.py 录像.mp4 --itn            # 逆文本正则化：三百二十万元 -> 320万元
python av_to_transcript_and_minutes.py 录像.mp4 --demucs-model htdemucs_ft   # 分离质量优先（官方微调版，慢约 4 倍）
python av_to_transcript_and_minutes.py 双人录音.wav --split-channels        # 双人分声道录制：声道号直接当说话人
python av_to_transcript_and_minutes.py 录音.wav --emotion                   # 8 类情绪分析（写入 json 的 emotions）
python av_to_transcript_and_minutes.py 会议.wav --speaker-db           # 声纹库 auto：输出目录 speaker_db.json 有则复用无则建库（也可给显式路径）
python av_to_transcript_and_minutes.py 录像.mp4 --replace "小蜜=>小米,开饭时间=>开放时间"   # 专名确定性纠错
python av_to_transcript_and_minutes.py 噪声录像.mp4 --denoise --vad firered              # 强噪前处理 + 更低误报 VAD
python av_to_transcript_and_minutes.py 录像.mp4 --asr-extra "--fuzzy --min-seg 300"      # 其余 qwen_asr 参数透传
python av_to_transcript_and_minutes.py 录像.mp4 --replace-file 词典.txt                    # 纠错词典文件（每行 错=>对）
python av_to_transcript_and_minutes.py 素材目录 --clean --log                             # 只留成稿 + 运行日志落盘
python av_to_transcript_and_minutes.py 录像.mp4 --verbose        # 实时看各子进程输出，排查卡顿/失败原因
python av_to_transcript_and_minutes.py 素材目录 --flat          # 平铺输出：成稿直接放素材同目录，过程件全清
python av_to_transcript_and_minutes.py 录像.mp4 --flat --log    # 平铺成稿 + 运行日志落盘
python av_to_transcript_and_minutes.py 会议.wav --meeting --rttm   # 会议原文 + RTTM 说话人日志（可喂 pyannote.metrics/meeteval）
python av_to_transcript_and_minutes.py 录像.mp4 --max-cps 9 --min-dur 800   # 字幕更慢速易读（默认 12 字/秒、700ms）
```

行为要点：

- `--no-asr` 只拆轨与分离；`--force` 重跑；转写结果按配置分文件缓存（阈值 / 热词 / 人名 / 字幕 /
  **识别引擎** / **额外 ASR 参数**任一不同则互不复用），最贵环节支持断点续跑。
- 完成判定为 md 与 docx 齐备：只有 md 而缺 docx（首跑时未装 article-format）时只补排版，
  不重跑分离与转写，不再需要 `--force` 全量重跑。
- `--clean` 完成后删除中间件（三份 wav 与转写缓存 json），只留成稿；对已完成的素材加 `--clean`
  也会顺手清掉残留中间件。注意缓存被删后换参数重转会重新走一遍分离，这是换磁盘空间的有意取舍。
- `--flat` 平铺输出（v2.4）：成稿 docx（及 `--srt` 的字幕）移到素材同目录，随后删除整个工作子目录（含 md 底稿、`.demucs_model` 标记、中间件与拆轨产物），默认 separated_out 目录清空后一并移除；已完成素材重跑时自动跳过（成稿在素材目录即视为完成）。
- `--log` 把本次运行输出追加写入 `<输出目录>/run.log`（终端与文件双写），批量跑完可回溯。
- 跑前查显存：可用量低于约 4.5 GB 时提示先停其它占卡程序或用 `--no-separate`（实测链路峰值增量约 4.4 GB，阈值 4600 MB 覆盖峰值不漏报）。
- 转写前粗估信噪比，低于 15 dB 时提示可试 `--denoise`（只提示不自动开——白噪声场景实测无收益，只在真实含 BGM/强噪素材上值得试）。
- 分离前对原始素材粗估信噪比（分位法），≥25 dB（校准：纯人声 25.9）时提示可加 `--no-separate` 跳过 Demucs 提速（只提示不自动跳）。
- `--split-channels`：立体声双人分声道素材（电话、双麦）按声道分轨转写，声道号直接当说话人编号，输出带标签的会议原文；会自动跳过 Demucs 与声纹聚类，非立体声回退普通模式。
- `--demucs-model` 可选 htdemucs（默认）／htdemucs_ft（官方微调版，慢约 4 倍）／mdx_extra 等；换模型会按新模型重跑分离，模型名记在产物目录的 `.demucs_model` 标记里。
- 素材无音轨（或音频损坏）归类为「无音轨」单独计数，不再把 ffmpeg 原始输出整段抛出。
- `--speaker-db` 裸写（auto）：库放输出目录 `speaker_db.json`，批内跨素材共享，命中显示库中姓名并更新声纹；库更新后重转同一素材需 `--force`（库路径恒定，不触发缓存重转）。
- `--replace-save`：把本次 `--replace`/`--replace-file` 条目合并写回词典（裸写=**管线根目录** `replace_dict.txt`，键同新值覆盖、注释保留）；下次 `--replace-file` 裸写自动带上，专名修正可积累。**v2.4 起词典与热词表固定放管线根目录**——平铺模式（`--flat`）会删整个工作子目录，放输出目录会被一并删掉、积累失效。
- 转写后只得到 1 个说话人、且用的是默认 campp 时，提示可改用 `--meeting` 或 pyannote 重跑。
- 批量打印 `[i/N]` 计数与每个素材用时，收尾给总耗时与均值。
- 批量时单个文件失败只计一次失败并继续下一个（素材损坏、缺 ffmpeg/demucs 都不会中断整批），
  失败信息打印子进程错误尾部 20 行而非单行。
- 各级子进程输出统一按 UTF-8 收发（子进程 `PYTHONIOENCODING=utf-8` + 父进程 `encoding="utf-8",
  errors="replace"`，stdout 与 stderr 都锁）；中文 Windows 下不会因 cp936 解码丢日志或显示乱码。
- 产物同落 `<输出>/<文件名>/`：`人声.wav`、`背景音.wav`、`人声_转写用.wav`、`转写结果*.json`、`转写文稿-<名>.md/.docx` 或 `会议原文-<名>.md/.docx`（另加 `.srt`／`.vtt` 字幕、`--rttm` 出 `.rttm`）。
- **v2.5 `--vad auto` 按用途选后端**：分离链路用 fsmn，`--no-diarize` 长音频分段用 FireRedVAD（装了才换）。依据是本地实测——25 秒双人素材上 FireRedVAD 切 9 段得到 7.23% DER，fsmn 切 4 段得到 4.53%：段被切碎后声纹聚类粒度变差。纯分段链路只拿 VAD 当识别批边界，同素材 8.5s → 0.37s。要强制指定就显式传 `--vad fsmn|firered`。
- **v2.5 会议原文顶部带「发言概览」**：每人段数、累计时长、占比、首末开口时间，以及重叠语音处数与总时长；单人素材不出（没有对比意义）。
- **v2.5 字幕可读性**：整段无标点的长句按字数硬切（切点优先退到软标点/空格），`--min-dur`（默认 700ms）与 `--max-cps`（默认 12 字/秒）补足过短展示时间且不越过下条起点。
- Demucs 在静音段留低电平伪影会让 VAD 切不出段，脚本固定用 `agate=threshold=-40dB` 还原真静音，勿删这一环。
- article-format 定位顺序：`ARTICLE_SKILL_DIR` → `~/.zcode/skills` → `~/.agents/skills` → `~/.claude/skills` → 仓库内 `skills/`；找不到时只出 md 并在 stderr 提示，不报错。
- 依赖 ffmpeg / ffprobe / demucs（均在 PATH）与 qwen_asr.py（同目录自动定位，可用 `VOICE_ASR_SCRIPT` 覆盖）。
- 短素材用默认 campp 聚类可能分不出说话人（VAD 段不足 2 段时按单一说话人处理）；要区分说话人的会议素材加 `--meeting`（自动走 pyannote）。

## 专名质量机制（v2.4）

管线通过「热词表 + 纠错词典」双机制维护专名准确率，两个文件都放**管线根目录固定位置**，**自动加载、无需任何参数**，且**平铺模式（--flat）不会删除它们**：

- `hotwords.txt`：热词表，每行一个词（# 注释），识别阶段偏置（云端即时热词上限 50 条）。**热词是偏置不是保证**——长主句命中率高、短插入句可能漏。
- `replace_dict.txt`：纠错词典，每行「错=>对」，识别后**确定性全文替换**（比热词可靠）。收词原则：确认为错写的专名；常用词组合慎收（全文替换会误伤）。
- **维护方式**：纪要核稿确认的新专名写进热词表（提升识别）、确认的错写写进词典（事后兜底）。两个文件改动后**新转写自动生效**（内容进缓存指纹：词典哈希 + 热词哈希），已交付素材不追溯（需 `--force` 重跑）。
- `--replace-save` 可把本次 `--replace` 条目合并回写词典。模板见仓库根 `*.example.txt`。

## 脚本真源说明

本工作区为 ZCode 侧副本：真源在本仓库根的 `av_to_transcript_and_minutes.py` 与
`skills/funasr-transcribe/scripts/`（2026-11-02 主仓库已从 `Daily Use/publish/av-to-transcript-and-minutes` 整体移入项目根）。
技能目录内脚本与仓库根脚本同批维护；ZCode 会话经技能钩子加载本副本，不再回写 Daily Use 工作目录。
单元自测：`python skills/funasr-transcribe/scripts/test_qwen_asr_units.py`、`.../test_itn_zh.py`；
链路回归：`python test_pipeline_e2e.py`（自动合成 TTS 视频跑全链路，17 项断言，期望 ALL PASS）。

**上游（2026-10-02）**：本副本自当时技能副本整体同步，含音频与视频全链路、P1–P7 链路优化
与模型层三批改进（`--itn`、`--engine auto`、`--vad firered` 修复、`--split-channels`、
`--emotion`、`--speaker-db`、`--demucs-model`、SNR 估计、`--clean`、`--log` 等）。
这批优化已通过 PR #2 与 PR #3 squash 合并进上游 `A2194008525/av-to-transcript-and-minutes`
（`216c092`，2026-10-01）。上游更新时按「上游版本 + 本仓库路径适配」的口径合并，勿整文件覆盖。
**2026-10-08 v2.4 回流**：`--flat` 平铺输出与根目录热词/词典零参数自动加载（含词典哈希进缓存指纹）、Demucs 子进程 `HF_HUB_OFFLINE=1` 已同步。
