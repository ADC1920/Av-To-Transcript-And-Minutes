# Av-To-Transcript-And-Minutes

音视频到转写文稿、会议原文与结构化纪要的一条命令工具链：拆轨、人声分离、说话人分离、识别、字幕、规范排版全部串成一条管线。

默认完全离线，模型全部本地运行，无 API 密钥、无网络调用（首次下载模型除外）。也可以只把识别环节交给云端 ASR，本地保留说话人分离。

```
视频 / 音频（mp4 / mkv / mp3 / m4a / wav / …）
      │
      ├─ ① 拆轨            无声视频 + 完整音轨（无损 copy，FFmpeg）
      ├─ ② 人声分离        人声.wav + 背景音.wav（Demucs htdemucs，GPU）
      ├─ ③ 噪声门清理      人声_转写用.wav（消除分离伪影，保 VAD 可用）
      ├─ ④ 说话人分离+识别  Qwen3-ASR / FireRedASR2-AED / 云端 × pyannote / cam++
      └─ ⑤ 组稿与排版      转写文稿（普通）或会议原文（带时间戳与说话人）→ 规范 docx
```

结构化纪要不由管线生成。管线产出带 `[分:秒] 说话人N：` 标签的会议原文，再由会话内 AI 按 [meeting-notes-expert](skills/meeting-notes-expert/SKILL.md) 模板提炼成七节式纪要。理解性工作交给 AI，确定性工作留给脚本。

```bash
python av_to_transcript_and_minutes.py <视频/音频文件或文件夹> [-o 输出目录] [--meeting] [--srt] …
```

当前版本 v2.6 ｜ MIT ｜ Windows / Linux / macOS ｜ Python 3.10+ ｜ 需 FFmpeg 在 PATH

## 核心能力

**识别引擎四选一**

| 引擎 | 说明 |
| --- | --- |
| `qwen` | Qwen3-ASR-1.7B，默认，多语言 |
| `aed` | FireRedASR2-AED，中英粤更准，带显存优化补丁 |
| `auto` | 按 `--language` 路由，中英粤走 AED，其余走 Qwen；`--dialect` 声明方言时改走 Qwen |
| `cloud` | 阿里云百炼，免显存；默认 message 通道，可选 omni 与 filetrans 子模型 |

换引擎自动重转，不复用旧缓存。

**说话人分离双引擎**

`pyannote` 用 community-1 分割模型，原生处理重叠语音，会议场景默认；`campp` 走 VAD + cam++ 声纹聚类，轻量，单人访谈够用。`--diarize-engine auto` 在会议模式探测 pyannote，未安装时回落 campp 并提示。

- 人数约束：`--num-speakers` 最准，`--min-speakers` / `--max-speakers` 兜底
- 长素材分块：超过 15 分钟自动分块（`--pyannote-chunk-s`，0 关闭），块间 30 秒重叠，跨块用声纹合并标签，内存峰值只与块长相关
- 重叠语音不强行归属：`get_overlap()` 区间单列进 `overlaps` 字段
- 声纹库 `--speaker-db` 跨文件复用身份，两条分离路线都支持
- 双人分声道素材加 `--split-channels`，声道号直接当说话人

**字幕与说话人日志**

`--srt` 出 SRT，`--vtt` 出 WebVTT（两者共用一次对齐，可同时开），`--rttm` 出标准 RTTM（pyannote.metrics / meeteval / dscore 直接读）。

字幕对齐后端 `--align-engine auto|fa-zh|native`：`fa-zh` 是中文专用 38M 模型，当前实际生效；`native`（Qwen3-ForcedAligner-0.6B）代码路径就绪，但需要上游 transformers 支持 0.6B 宽度，当前版本不支持，`auto` 回落 `fa-zh`。非中文素材的字幕时间戳仍会偏。

字幕可读性三项兜底：超长且无标点的识别结果按字数硬切（切点优先退到软标点或空格）；`--min-dur` 默认 700ms 补足过短展示时间；`--max-cps` 默认 12 字每秒限制阅读速度，且不越过下一条字幕起点。

字幕参数不进口缓存键。先跑转写、事后加 `--srt` 只补一遍对齐，不重跑识别。

**专名质量**

根目录放 `hotwords.txt`（热词，每行一个）与 `replace_dict.txt`（纠错词典，每行 `错=>对`）即自动加载，无需参数。热词走识别阶段偏置，词典走识别后确定性替换，内容都进缓存指纹，改动后旧缓存自动不复用。

`--replace` 做临时确定性替换，`--replace-save` 把本次条目合并写回词典，专名修正可跨次积累。模板见 `*.example.txt`。

`--itn` 中文逆文本正则化（三百二十万元转成 320万元），`--itn-backend auto|wtp|legacy` 选后端，装了 WeTextProcessing 就走它，否则用零依赖自写规则。

**批量、续跑与可观测**

- 输入文件夹时递归处理所有音视频；单个失败只计一次并继续，收尾退出码非零
- 转写结果按参数指纹缓存，阈值、热词、人名、识别引擎、额外 ASR 参数、词典内容、响度归一化状态任一不同即互不复用
- 批量时拉起常驻转写子进程，主引擎只加载一次；worker 异常自动回退逐文件子进程
- `--watch DIR` 目录监视，发现新素材（大小稳定后）自动跑完整管线
- `--log` 落 `run.log`，`manifest.json` 记录每个素材的参数、耗时、状态与实际产物
- `--clean` 删中间件只留成稿，`--flat` 把成稿平铺到素材目录

**其他**

`--emotion` 出 8 类情绪打分（不做事音频事件检测）；`--denoise` 转写前 ZipEnhancer 降噪，仅对真实含 BGM 或强噪的素材有益；`--no-diarize` 单人素材跳过分离；响度自适应仅在峰值低于 -6 dBFS 且语音段 RMS 低于 -30 dBFS 时做 EBU R128 归一化，正常录音不动。

## 快速开始

### 环境

```bash
# 系统依赖：FFmpeg 须在 PATH
winget install ffmpeg          # Windows
# apt install ffmpeg          # Linux
# brew install ffmpeg         # macOS

# Python 依赖（推荐 uv，按 uv.lock 装出可复现版本）
uv sync

# 不想装 uv 时
pip install -r requirements.txt
```

GPU 加速先按 https://pytorch.org 装对应 CUDA 版本的 torch。依赖需走 CUDA 索引，PyPI 上的同名包是 CPU 版，装上去会静默退化为 CPU，转写慢十倍且 AED 时间戳失效。

### 模型

首次运行自动下载，也可以手动拉取：

| 用途 | 模型 | 来源 |
| --- | --- | --- |
| 识别 | Qwen3-ASR-1.7B | ModelScope `Qwen/Qwen3-ASR-1.7B-hf` |
| 说话人分离（会议） | pyannote community-1 | ModelScope `pyannote/speaker-diarization-community-1` |
| 说话人分离（轻量） | fsmn-vad + cam++ | 随 FunASR 下载 |
| 人声分离 | htdemucs | 随 Demucs 下载 |

pyannote 官方模型在 HuggingFace 是门控资源，ModelScope 有官方镜像且无需申请：

```bash
python -c "from modelscope import snapshot_download; snapshot_download('pyannote/speaker-diarization-community-1')"
```

### 跑起来

```bash
# 普通音视频 → 转写文稿
python av_to_transcript_and_minutes.py 我的视频.mp4

# 会议录音 → 会议原文（时间戳 + 说话人标签）
python av_to_transcript_and_minutes.py 会议录音.m4a --meeting

# 批量 + 字幕 + 人名 + 热词
python av_to_transcript_and_minutes.py ./素材目录 --meeting --srt \
    --names "0=张三,1=李四" --hotwords "产品名,行业术语"
```

### 无 GPU 或不想跑本地识别

识别交给云端，本地只留 VAD 与声纹聚类：

```bash
uv sync --extra cloud
setx DASHSCOPE_API_KEY "sk-xxx"       # Linux/macOS 用 export
setx ASR_ENGINE "cloud"               # 之后不带 --asr-engine 也默认走云端
python av_to_transcript_and_minutes.py 会议录音.m4a --meeting
```

`ASR_ENGINE` 只改本机默认值，仓库默认仍是离线的 `qwen`。`filetrans` 子模型只接受公网 URL，本地文件用 `scripts/oss_upload_bridge.py` 压缩上传并生成签名 URL，凭据只从环境变量读取。

## 常用参数

| 参数 | 说明 |
| --- | --- |
| `--meeting` | 产出会议原文而非普通转写文稿 |
| `--no-diarize` | 单人素材跳过说话人分离，与 `--meeting` 互斥 |
| `-o` / `--output` | 输出目录，默认输入旁 `separated_out/` |
| `--asr-engine qwen\|aed\|auto\|cloud` | 识别引擎 |
| `--diarize-engine auto\|pyannote\|campp` | 说话人分离引擎 |
| `--num-speakers` / `--min-speakers` / `--max-speakers` | 人数约束（pyannote） |
| `--pyannote-chunk-s` / `--pyannote-overlap-s` | 分块时长与块间重叠，默认 900 / 30 秒 |
| `--srt` / `--vtt` / `--rttm` | 字幕与说话人日志（共用一次对齐） |
| `--align-engine auto\|fa-zh\|native` | 字幕对齐后端 |
| `--min-dur` / `--max-cps` / `--max-line` / `--merge-gap` | 字幕最短时长、阅读速度、单条字数、合并间隔 |
| `--names "0=张三,1=李四"` | 说话人真名映射 |
| `--hotwords` | 热词偏置，未指定时自动加载根目录 `hotwords.txt` |
| `--replace "错=>对"` | 确定性专名纠错 |
| `--replace-file` / `--replace-save` | 纠错词典读取与回写，裸写走根目录 `replace_dict.txt` |
| `--itn` / `--itn-backend` | 中文逆文本正则化与后端选择 |
| `--speaker-db` | 声纹库，裸写或 `auto` 用输出目录 `speaker_db.json` |
| `--split-channels` | 双人分声道素材按声道分轨 |
| `--emotion` | 8 类情绪打分 |
| `--denoise` | ZipEnhancer 降噪 |
| `--vad auto\|fsmn\|firered` | VAD 后端，`auto` 按用途选 |
| `--language` / `--dialect` | 强制语言 / 声明方言 |
| `--demucs-model` | 分离模型，换模型自动重跑 |
| `--no-separate` | 跳过人声分离 |
| `--no-asr` | 只拆轨与分离 |
| `--watch DIR` | 目录监视 |
| `--clean` / `--flat` | 清中间件 / 平铺成稿 |
| `--log` / `--verbose` | 落运行日志 / 实时透传子进程输出 |
| `--force` | 重跑已处理素材 |

完整参数见 `python av_to_transcript_and_minutes.py --help`。

## 产物

每个输入在输出目录下生成同名子目录：

| 文件 | 说明 |
| --- | --- |
| `无声视频.mp4` / `完整音轨.m4a` | 拆轨产物（仅视频输入） |
| `人声.wav` / `背景音.wav` | Demucs 分离结果 |
| `人声_转写用.wav` | 噪声门清理后供转写的版本 |
| `转写结果*.json` | 结构化结果（分段、文本、时间戳、置信度、情绪），按参数指纹缓存 |
| `转写文稿-<名>.md/.docx` | 普通模式成稿 |
| `会议原文-<名>.md/.docx` | 会议模式成稿，顶部含发言概览 |
| `会议原文-<名>.srt/.vtt/.rttm` | 字幕与说话人日志 |
| `manifest.json` | 本次批量的运行记录：顶层参数、总耗时、汇总计数，每个素材的状态、耗时与产物清单，只列实际存在的文件 |

会议原文顶部的发言概览给出每人段数、累计时长、占比、首末开口时间与重叠语音统计。提炼纪要时「谁占了六成发言、谁只说了两句」会直接改变权重与判断。

纪要文档可再导出机器可读 JSON（待办表、决策列表、各节文本）：

```bash
python skills/meeting-notes-expert/scripts/validate_minutes.py 会议纪要-x.docx --export 会议纪要-x.json
```

## 工具层

| 工具 | 用途 |
| --- | --- |
| `tools/speaker_timeline.py` | 转写结果 / 会议原文 / RTTM 渲染成说话人泳道图 HTML 片段 |
| `tools/meeting_index.py` | 跨会议检索，SQLite FTS5，`add` / `search` / `stats` |
| `tools/mcp_server.py` | MCP 工具服务（stdio），job 语义 |

```bash
# 泳道图
python tools/speaker_timeline.py out/会议原文-x.json -o 时间轴.html

# 跨会议检索（索引落 ~/.av2m/meetings.db，AV2M_INDEX 可覆盖）
python tools/meeting_index.py add out/
python tools/meeting_index.py search 胚布 白配检测 --speaker 张三

# MCP 服务（uv sync --extra mcp）
python tools/mcp_server.py
```

跨会议检索用 trigram 而不是 unicode61：中文没有词边界，unicode61 会把整句当成一个大 token，子串查询永远命中不了。trigram 按三字滑窗建索引，三字及以上任意子串可匹配，两字退到 LIKE 扫描。

MCP 暴露 8 个工具：`transcribe_start/status/result/cancel/list`、`search_meetings`、`index_meetings`、`render_timeline`。`transcribe_start` 立即返回 `job_id` 不阻塞，状态落 `~/.av2m/jobs/`（`AV2M_JOBS` 可覆盖），进程重启后仍可查。幂等键是输入内容哈希加参数哈希，同一文件同一参数重复提交直接复用。参数只接受白名单，拼错的键会被拒，不会静默忽略。

## 项目结构

```
Av-To-Transcript-And-Minutes/
├── av_to_transcript_and_minutes.py   主脚本，①→⑤ 全流程编排
├── benchmark_quality.py              质量基准：DER / CER / cpWER
├── test_pipeline_e2e.py              端到端回归（合成素材）
├── hotwords.example.txt              热词表模板
├── replace_dict.example.txt          纠错词典模板
├── pyproject.toml / uv.lock          依赖真相源与锁定版本
├── requirements.txt                  兼容入口
├── docs/
│   ├── project-info.md               架构、模块表、本机环境与显存边界
│   ├── landscape-2026-11.md          同类项目与模型格局（第一轮）
│   ├── landscape-2026-11-r2.md       第二轮复核与论断对账
│   └── optimization-roadmap-2026-11.md
├── scripts/
│   ├── make_fixtures.py              合成带标注的回归素材
│   └── oss_upload_bridge.py          filetrans 的公网 URL 桥接
├── tools/
│   ├── speaker_timeline.py
│   ├── meeting_index.py
│   └── mcp_server.py
├── tests/
│   ├── test_quality_baseline.py
│   └── test_tools_units.py
├── skills/
│   ├── funasr-transcribe/            转写技能与引擎
│   │   ├── SKILL.md
│   │   ├── references/advanced.md
│   │   └── scripts/
│   │       ├── qwen_asr.py           识别、分离、字幕、纠错
│   │       ├── itn_zh.py             中文逆文本正则化
│   │       ├── firered_mem_patch.py  AED 显存补丁
│   │       ├── diarize.py
│   │       └── test_*.py
│   └── meeting-notes-expert/         纪要模板、校验器与 golden 用例
└── .github/workflows/tests.yml
```

## 验证与质量基线

```bash
# 端到端回归（自动合成 TTS 素材，断言全链路产物）
python test_pipeline_e2e.py

# 质量基线（DER / CER / 重叠检测，素材缺失自动生成）
python tests/test_quality_baseline.py

# 引擎与工具单测
python skills/funasr-transcribe/scripts/test_qwen_asr_units.py
python skills/funasr-transcribe/scripts/test_split_and_cluster.py
python skills/funasr-transcribe/scripts/test_v25_upgrade_units.py
python skills/funasr-transcribe/scripts/test_pyannote_chunking.py
python skills/funasr-transcribe/scripts/test_itn_zh.py
python skills/funasr-transcribe/scripts/test_cloud_units.py
python skills/funasr-transcribe/scripts/test_cache_fingerprint.py
python tests/test_tools_units.py

# 基准自检与纪要校验自检
python benchmark_quality.py --selftest
python skills/meeting-notes-expert/scripts/validate_minutes.py --selftest
```

CI 分两个 job：离线 job 只装 numpy，跑语法检查与全 mock 单测，覆盖 Python 3.10 与 3.12；纪要 job 装 python-docx 与 jsonschema，跑结构校验自检与 golden 用例回归。需要模型与显卡的 e2e 与质量基线在本地跑。

### 质量基准

合成素材只能验证管线不炸，验不出转得准不准。改切分策略、说话人数、声纹库或对齐后端之前，先攒几份带标注素材做基线。

素材由 `scripts/make_fixtures.py` 现场合成，音频不入库。标注放素材同目录，命名 `<素材名>.ref.json`：

```json
{
  "speakers": [
    {"start_ms": 0, "end_ms": 5200, "spk": "张三", "text": "这一段他说的原话"},
    {"start_ms": 5200, "end_ms": 9000, "spk": "李四", "text": "这一段他说的原话"}
  ],
  "text": "完整正确文本，用于算 CER（可省略）"
}
```

```bash
# 评测单份
python benchmark_quality.py 会议录音.m4a --report 基线.json

# 改完代码后对比
python benchmark_quality.py 会议录音.m4a --report 新.json --compare 基线.json
```

DER 走 pyannote.metrics 标准实现（0.25 秒 collar）。CER 中文按字、英文按词，标点与全半角不计。v2.5 起同时报三个数：容差 DER、无容差 DER、cpWER。前者可与论文数字对齐，中者量边界精度，后者把每人的话拼串比字，同时惩罚归属错与识别错。cpWER 需要参考标注带逐段 `text`，缺逐段文本时不报。

当前基线（合成双人素材）：DER 9.86%、CER 0、重叠检测 1 处 5.4 秒。质量门槛：DER ≤ 0.25、CER ≤ 0.30。

### 性能参考

RTX 5070 Ti 16GB，28 分钟中文会议：

| 环节 | 耗时 |
| --- | --- |
| 人声分离（Demucs） | 约 1 分钟 |
| 说话人分离（pyannote） | 约 40 秒 |
| 识别（Qwen3-ASR） | 约 87 秒 |
| 全流程 | 约 4 分钟 |

8GB 显存的边界（RTX 4060 实测）：识别、分离、字幕、RTTM、ITN、声纹库全部可用。不开情绪时完整管线峰值 4.9 至 6.2GB，安全。唯一硬边界是 `--emotion`，emotion2vec 整段推理显存随时长线性上涨，开情绪后峰值升到 7.7GB 只余 0.28GB，10 分钟音频单独跑就 OOM，安全区间约 5 分钟以内。超限时打 warn 跳过情绪标注，转写不受影响。

## 设计要点

**噪声门是必需环节。** Demucs 在静音段输出低电平伪影，会让下游 VAD 把整段连成一块，说话人分离随之报废。链路在分离与转写之间插入 `agate` 噪声门恢复段间真静音。

**切点吸附静音。** 超长段切分不做等分，在每个理想切点前后 50ms 窗内找最低能量位置，把切点移到声门闭合处。等分点常落在词中间，接缝处会出现残字或重复。

**跨块说话人映射用声纹，不用时间投票。** 同一人跨块会各自得到独立标签，不映射就会凭空多出说话人。声纹按段独立判定，与块边界位置无关；时间重合投票只作声纹不可用时的兜底，块边界正好切在换人处时会投错边。

**分块下限 30 秒。** 实测 10 秒块内 pyannote 会把两人合并成单标签，强制人数约束也救不回来。

**VAD 按用途选，不盲目上新模型。** `--vad auto` 在分离链路用 fsmn，在 `--no-diarize` 长音频分段用 FireRedVAD。依据是实测：同一段 25 秒双人素材，fsmn 切 4 段得 4.53% DER，FireRedVAD 切 9 段得 7.23% DER，碎段让声纹聚类变差；但 FireRedVAD 作为纯分段边界快约 20 倍（8.5s 降到 0.37s），所以只在不需要分离的链路启用。

**AED 显存三层防护。** 分块注意力（softmax 按行独立，数学等价）加时长感知分批，加 OOM 拆批与静音点二分兜底，再配 bf16 与 decoder mask 缓存。实测 4×107s 批量峰值显存 11.8GB 降到 3.5GB，推理提速 3 倍。不改官方源码，monkey-patch 可还原。

**torchcodec 绕行。** pyannote 会带入 torchcodec，它与静态 FFmpeg 不兼容。链路用 soundfile 预载波形、把 numpy 波形直接传给 processor，绕开解码路径。仍遇报错可 `pip uninstall torchcodec`，管线不依赖它。

## 已知限制

- 抢话极严重的会议仍可能过切（实测 35 簇收敛到 2 簇，但不保证 100%）；重叠区间只做标记，不拆分同一时段的双人文本
- 说话人编号在不同次运行间可能互换，用 `--names` 固定映射，`--speaker-db` 跨文件复用身份
- 非中文素材的字幕时间戳会偏，`native` 对齐器要等上游 transformers 支持 0.6B 宽度
- FireRedASR2-AED 是可选引擎，需另下模型与代码
- WeTextProcessing 依赖 pynini，Windows 下需 MSVC 编译，装不上时 ITN 用自写规则
- 规范 docx 排版依赖 `article-format` 技能，未安装时产出标准 docx
- 质量基准需要人工标注素材，没有基线时优化效果只能靠主观判断
- `--clean` 会删缓存与中间 wav，之后换参数重转要重新走分离

## License

[MIT](LICENSE)
