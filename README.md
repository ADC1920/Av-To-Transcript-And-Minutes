# 音视频 → 转写文稿 / 结构化总结（Av-To-Transcript-And-Minutes）

一套**默认完全离线、本地运行**的「音视频 → 转写文稿 / 结构化总结」全流程工具箱（亦可选接阿里云百炼云端 ASR 引擎）：

```
视频 / 音频（mp4/mkv/mp3/m4a/wav/…）
        │
        ├─ ① 拆轨（视频输入）        无声视频 + 完整音轨（无损，FFmpeg）
        │
        ├─ ② 人声 / 背景音分离        人声.wav + 背景音.wav（Demucs htdemucs，GPU）
        │
        ├─ ③ 噪声门清理              人声_转写用.wav（消除分离模型的静音伪影，保 VAD 可用）
        │
        ├─ ④ 说话人分离 + 转写        Qwen3-ASR-1.7B（本地）/ aed / cloud 云端 × (pyannote / cam++)，带时间戳与说话人
        │
        └─ ⑤ 组稿 + 排版规范 docx    转写文稿（普通）  或  会议原文（带标签，供提炼总结）
                                     正式总结由 AI Agent 按 meeting-notes-expert 模板提炼
```

**一条命令跑完 ①→⑤**：

```bash
python av_to_transcript_and_minutes.py <视频/音频文件或文件夹> [-o 输出目录] [--meeting] [--srt] …
```

> 当前版本：**v2.4** ｜ License: [MIT](LICENSE) ｜ 环境：Windows / Linux / macOS，需 FFmpeg 与 Python 3.10+

---

## ✨ 特性

- **完全离线**：模型全部本地运行，无 API 密钥、无网络依赖（首次运行自动下载模型后）
- **一条命令全自动**：拆轨 → 分离 → 说话人分离 → 转写 → 排版成 docx
- **双产出模式**：`转写文稿-<名>.docx`（普通音视频）／`会议原文-<名>.docx`（会议，带 `[分:秒] 说话人N：` 标签）
- **说话人分离双引擎**：
  - `pyannote`（community-1，会议场景默认）——分割模型原生处理**重叠语音**，实测真实 28 分钟会议从 35 个假说话人收敛到 2 个；支持 `--num-speakers` / `--min-speakers` / `--max-speakers` 下发人数约束（最便宜的精度提升）；重叠区间单独标记进 `overlaps` 字段而不强行归给某一人
  - `campp`（VAD + cam++ 声纹聚类）——轻量，单人/访谈素材够用
  - **长音频分块**：超过 15 分钟自动分块分析（`--pyannote-chunk-s` 可调，0 关闭），块间带 30 秒重叠，跨块用声纹相似度合并说话人标签，内存峰值只与块长相关而非总时长
- **声纹库双引擎通用**：`--speaker-db` 原先只有 campp 可用，现在 pyannote 路径会在已定段边界上补算声纹，两条路线都能跨文件复用说话人身份
- **字幕与纠错**：`--srt` 出字级时间戳字幕；`--replace` 确定性专名纠错（比 `--hotwords` 偏置可靠）；`--replace-file` / `--replace-save` 纠错词典读取与回写（本次纠错条目自动并入词典，下次自动带上）；`--names` 说话人真名映射；`--asr-extra` 透传其余引擎参数
- **字幕对齐后端可选**：`--align-engine auto|fa-zh|native`——`native` 用 Qwen3-ForcedAligner-0.6B，覆盖 11 种语言；`fa-zh` 是中文专用的 38M 模型。默认 auto（有 Qwen 对齐器就用，没有回落 fa-zh）。**中文以外素材必须用 native**，fa-zh 会产出错位时间戳
- **补字幕不重转**：字幕已移出缓存键。先跑转写、事后加 `--srt`，只补一遍对齐（实测 15.3s → 9.0s），不会重跑最贵的识别环节
- **ITN 后端可选**：`--itn-backend auto|wtp|legacy`——装了 WeTextProcessing 就走它（货币/度量/分数表达更全），没装继续用零依赖自写规则
- **质量基准**：`benchmark_quality.py` 跑 DER（说话人分离错误率，pyannote.metrics 标准实现）与 CER（字错误率），支持与历史基线对比，回答"这次改动到底变好还是变坏"
- **断点续跑**：转写（最贵环节）结果按参数指纹缓存——阈值 / 热词 / 人名 / **识别引擎** / 额外 ASR 参数任一不同即互不复用，**切换识别引擎不会误用旧结果**；字幕与对齐后端**不进**缓存键（它们只影响字幕，不影响文本），要字幕时走只补对齐的通道
- **完成判定与补排版**：md 与 docx 齐备才算完成；首次运行缺 `article-format` 时只出 md，补装后重跑自动只补 docx，无需 `--force` 全量重跑
- **批量容错与可观测**：单个文件失败只计一次失败并继续（素材损坏、缺 ffmpeg/demucs 都不中断整批）；无音轨素材单独归类提示；批量打印 `[i/N]` 计数与每个素材用时，收尾给总耗时与均值；`--log` 把运行输出落到 `<输出目录>/run.log`
- **跑前自检与提示**：跑前查显存，可用量低于 4 GB 时提示先停占卡程序或用 `--no-separate`；转写只得到 1 个说话人且用的是默认 campp 时，提示可改用 `--meeting` 或 pyannote 重跑
- **磁盘可控**：`--clean` 完成后删除中间件（三份 wav 与转写缓存 json），只留成稿，长素材不再动辄留下上 GB 的中间 wav
- **平铺输出（v2.4）**：`--flat` 把成稿 docx（及字幕）直接放到素材同目录，删除整个工作子目录（含中间件、md 底稿、`.demucs_model` 标记与拆轨产物）——素材目录最终只有「原始音视频 + 成稿」；重跑时成稿在素材目录即视为已完成，自动跳过
- **模型层可选与增强**：`--asr-engine auto` 按语言路由（中/英/粤走 FireRedASR2-AED，其余走 Qwen3-ASR）；`--vad firered` 用误报更低的 FireRedVAD 切段；`--demucs-model` 可选 htdemucs / htdemucs_ft / mdx_extra 等分离模型（换模型自动重跑）
- **云端可选引擎（v2.3）**：`--asr-engine cloud` 免显存调用阿里云百炼（默认 `qwen-audio-3.1-asr-message`，WebSocket 整文件直出句级+字级时间戳；`--srt` 直接用云端时间戳，无需本地 fa-zh；驱动说话人分离字幕时逐段 4 线程并发）；可选子模型 `omni`（qwen3.8-omni-flash 全模态）与 `filetrans`（异步长音频，仅公网 URL）；`--hotwords` 走即时热词表生效。需 `DASHSCOPE_API_KEY`（环境变量）与 `pip install dashscope`
- **专名质量机制（v2.4）**：管线根目录放 `hotwords.txt`（热词表，每行一个词，# 注释）与 `replace_dict.txt`（纠错词典，每行「错=>对」）即**自动加载、无需任何参数**——热词走识别阶段偏置、词典走识别后确定性替换，两者内容都进转写缓存指纹（改动后旧缓存自动不复用）；模板见 `*.example.txt`。`--replace-save` 把本次纠错条目合并写回词典，专名修正可跨次积累。**`--flat` 平铺模式不会删除这两个文件**（固定放管线根目录）
- **输出规范化**：`--itn` 中文逆文本正则化（三百二十万元 → 320万元、二零二六年十月十五日 → 2026年10月15日），零依赖自写规则
- **双人分声道素材**：`--split-channels` 按声道分轨转写，声道号直接当说话人，省掉声纹聚类、也不会把两人混在一起
- **情绪与声纹**：`--emotion` 出 8 类情绪打分（写入 json 的 `emotions` 字段；**不做**笑声/掌声等音频事件检测）；`--speaker-db` 声纹库跨文件复用说话人身份（首次用 `--speaker-db-save` 建库）
- **单人素材提速（v2.3）**：`--no-diarize` 跳过说话人分离直接整段转写（云端 message 引擎将走整文件一次调用），与 `--meeting` 互斥、缓存独立
- **批量处理**：输入文件夹时递归处理其中所有音视频

---

## 🚀 快速开始

### 1. 环境准备

```bash
# 系统依赖：FFmpeg（须在 PATH 中）
winget install ffmpeg            # Windows
# apt install ffmpeg            # Linux
# brew install ffmpeg           # macOS

# Python 依赖
pip install -r requirements.txt
# GPU 加速：先按 https://pytorch.org 安装对应 CUDA 版本的 torch

# 若报 libtorchcodec 加载失败（pyannote 引入的库与静态 FFmpeg 不兼容）：
pip uninstall torchcodec
```

### 2. 模型准备（首次，自动下载）

| 用途 | 模型 | 来源 |
| --- | --- | --- |
| 转写识别 | Qwen3-ASR-1.7B | ModelScope `Qwen/Qwen3-ASR-1.7B-hf` |
| 说话人分离（会议） | pyannote community-1 | ModelScope `pyannote/speaker-diarization-community-1` |
| 说话人分离（轻量） | fsmn-vad + cam++ | 随 FunASR 自动下载 |
| 人声分离 | htdemucs | 随 Demucs 自动下载 |

> pyannote 官方模型在 HuggingFace 为门控资源（需申请权限）；**ModelScope 有官方镜像且无需门控**，推荐：
> ```bash
> python -c "from modelscope import snapshot_download; snapshot_download('pyannote/speaker-diarization-community-1')"
> ```

### 3. 跑起来

```bash
# 普通音视频 → 转写文稿（自动说话人分离）
python av_to_transcript_and_minutes.py 我的视频.mp4

# 会议录音 → 会议原文（带时间戳与说话人标签，供后续提炼纪要）
python av_to_transcript_and_minutes.py 会议录音.m4a --meeting

# 批量 + 字幕 + 人名映射 + 热词
python av_to_transcript_and_minutes.py ./素材目录 --meeting --srt \
    --names "0=张三,1=李四" --hotwords "产品名,行业术语"
```

### 4. 常用参数

| 参数 | 说明 |
| --- | --- |
| `--meeting` | 产出会议原文（带时间戳+说话人标签）而非普通转写文稿 |
| `--srt` | 额外产出 `.srt` 字幕（字级时间戳）；事后加不会重转，只补对齐 |
| `--align-engine auto\|fa-zh\|native` | 字幕对齐后端：`auto` 有 Qwen 对齐器就用否则 fa-zh；`native`=Qwen3-ForcedAligner（多语言）；`fa-zh`=中文专用 |
| `--diarize-engine auto\|pyannote\|campp` | 说话人分离引擎（`auto`=会议用 pyannote，其余 campp） |
| `--num-speakers N` | 已知说话人数（pyannote）：人数确定时最准，如两人对谈填 2 |
| `--min-speakers N` / `--max-speakers N` | 人数区间（pyannote）：人数不确定时用上限挡住过度切分 |
| `--pyannote-chunk-s N` | pyannote 分块时长上限秒（默认 900，0 关闭）：长素材降内存峰值 |
| `--itn-backend auto\|wtp\|legacy` | ITN 后端：`auto` 有 WeTextProcessing 就用否则自写规则 |
| `--asr-engine qwen\|aed\|auto\|cloud` | 识别引擎：`qwen`（默认，多语言）／`aed`（中英粤更准）／`auto` 按语言路由／`cloud` 阿里云百炼云端引擎；**换引擎自动用新缓存重转** |
| `--no-diarize` | 单人素材跳过说话人分离直接整段转写（更快；云端 message 引擎走整文件一次调用）；与 `--meeting` 互斥 |
| `--replace "错=>对,错2=>对2"` | 识别后确定性替换，专名纠错比 `--hotwords` 可靠（同音异字配 `--asr-extra "--fuzzy"`） |
| `--denoise` | 转写前 ZipEnhancer 降噪（仅真实含 BGM/强噪素材；干净素材不加更好） |
| `--vad fsmn\|firered` | VAD 切段后端（默认 `fsmn`；`firered` 误报更低） |
| `--language zh\|en\|yue\|…` | 强制语言，默认自动检测 |
| `--max-line N` / `--merge-gap N` | 单条字幕最大字数（默认 28）／相邻同说话人合并间隔 ms（默认 800，0 关闭） |
| `--asr-extra "--fuzzy --min-seg 300"` | 其余 `qwen_asr.py` 参数原样透传（同样计入缓存指纹） |
| `--names "0=张三,1=李四"` | 说话人真名映射 |
| `--hotwords "词1,词2"` | 热词偏置，提升专名识别率；未指定时自动加载根目录 `hotwords.txt` |
| `--no-separate` | 跳过人声分离（纯人声音频可直接转写） |
| `--no-asr` | 只做拆轨/分离，不转写 |
| `--verbose` | 实时透传各子进程输出，排查卡顿与失败原因 |
| `--clean` | 完成后删除中间件（三份 wav 与转写缓存 json），只留成稿；对已完成素材也生效 |
| `--flat` | **平铺输出（v2.4）**：成稿直接放素材同目录，删工作子目录与 md 底稿（含中间件、拆轨产物） |
| `--log` | 本次运行输出追加写入 `<输出目录>/run.log` |
| `--replace-file [词典.txt]` | 纠错词典文件（每行 `错=>对`）；未指定时自动加载根目录 `replace_dict.txt`（存在即加载）；裸写=根目录 `replace_dict.txt` |
| `--replace-save [词典.txt]` | 把本次 `--replace` 纠错条目合并写回词典（裸写=根目录 `replace_dict.txt`），下次 `--replace-file` 裸写即自动带上 |
| `--itn` | 中文逆文本正则化：三百二十万元 → 320万元、百分之八十 → 80%、二零二六年十月十五日 → 2026年10月15日 |
| `--asr-engine auto` | 按 `--language` 路由识别引擎（中/英/粤走 AED，其余走 Qwen） |
| `--asr-engine cloud` | 云端识别（默认 message 通道）；换子模型加 `--asr-extra "--cloud-model omni\|filetrans"`；filetrans 需配 `--asr-extra "--cloud-url 公网URL"` |
| `--demucs-model NAME` | 分离模型：`htdemucs`（默认）／`htdemucs_ft`（质量更好、慢约 4 倍）／`mdx_extra` 等；换模型自动重跑分离 |
| `--split-channels` | 双人分声道素材按声道分轨转写，声道号即说话人；自动跳过 Demucs 与声纹聚类 |
| `--emotion` | emotion2vec 输出 8 类情绪打分（写 json 的 `emotions`；不做笑声/掌声等事件检测） |
| `--speaker-db db.json` | 声纹库：跨文件复用说话人身份（仅 campp 引擎）；建库加 `--asr-extra "--speaker-db-save"` |
| `--force` | 重跑已处理过的素材 |
| `-o 输出目录` | 指定输出目录（默认输入旁 `separated_out/`） |

---

## 📦 产物说明

每个输入在输出目录下生成同名子目录：

| 文件 | 说明 |
| --- | --- |
| `无声视频.mp4` | 去掉声音的视频（仅视频输入，画面无损） |
| `完整音轨.m4a` | 从视频无损提取的完整音频（仅视频输入） |
| `人声.wav` / `背景音.wav` | Demucs 分离出的人声与背景音 |
| `人声_转写用.wav` | 经噪声门清理后供转写的版本（可复现中间件） |
| `转写结果*.json` | 结构化转写结果（说话人分段/文本/时间戳），按参数指纹缓存 |
| `转写文稿-<名>.md/.docx` | 普通模式成稿（如有 article-format 技能则套用排版规范） |
| `会议原文-<名>.md/.docx` | 会议模式成稿（`[00:00] 说话人N：文本`） |
| `会议原文-<名>.srt` | 字幕（`--srt`，含说话人标记） |

**结构化总结**由 AI Agent 读取「会议原文」后按 [meeting-notes-expert](skills/meeting-notes-expert/SKILL.md) 模板提炼成七节式总结（基本信息 / 结论 / 关键决策或要点 / 行动事项 / 待定与开放问题 / 上期回顾 / 内容详情）——这一步是理解性工作，交给会话内的 AI 完成。会议场景产出 `会议纪要-<名>.docx`，讲座、访谈、播客等其他场景产出 `总结-<名>.docx`。

---

## 🧩 项目结构

```
Av-To-Transcript-And-Minutes/
├── av_to_transcript_and_minutes.py  # 全自动链路主脚本（①→⑤）
├── test_pipeline_e2e.py             # 端到端回归（自动合成素材，17 项断言）
├── hotwords.example.txt             # 热词表示例（复制为 hotwords.txt 自动加载）
├── replace_dict.example.txt         # 纠错词典示例（复制为 replace_dict.txt 自动加载）
├── requirements.txt
├── benchmark_quality.py             # 质量基准：DER（分离错误率）+ CER（字错误率）
├── skills/
│   ├── funasr-transcribe/
│   │   ├── SKILL.md                 # 转写技能（Agent 调用规范）
│   │   └── scripts/
│   │       ├── qwen_asr.py          # 转写引擎：Qwen3-ASR + pyannote/cam++ + 字幕/纠错
│   │       ├── itn_zh.py            # 中文逆文本正则化（WeTextProcessing 可选后端）
│   │       ├── firered_mem_patch.py # AED 引擎分块注意力与显存补丁（v2.1）
│   │       ├── transcribe.py        # 简化入口
│   │       ├── diarize.py           # 说话人分离（FunASR 路线）
│   │       ├── test_qwen_asr_units.py        # 引擎单元测试
│   │       ├── test_split_and_cluster.py     # 静音点切分与声纹质心
│   │       ├── test_pyannote_chunking.py     # 分块规划与重叠区间 API
│   │       └── test_firered_mem_patch.py     # AED 显存补丁单测
│   └── meeting-notes-expert/
│       └── SKILL.md                 # 总结整理技能（七节模板，会议/通用双场景）
└── LICENSE
```

---

## ✅ 验证

```bash
# 端到端回归（自动合成 TTS 测试素材，断言全链路产物）
python test_pipeline_e2e.py

# 引擎单元测试（切句/时间戳/碎段合并/超长切分/小簇吸收等纯逻辑）
python skills/funasr-transcribe/scripts/test_qwen_asr_units.py

# 超长段静音点切分与声纹质心（零依赖）
python skills/funasr-transcribe/scripts/test_split_and_cluster.py

# pyannote 分块规划与重叠区间 API
python skills/funasr-transcribe/scripts/test_pyannote_chunking.py

# 质量基准自检（CER/编辑距离；DER 需 pyannote.metrics）
python benchmark_quality.py --selftest
```

### 质量基准（DER / CER）

合成素材只能验证"管道不炸"，验不出"转得准不准"。改任何一项（切分策略、说话人数、
声纹库、对齐后端）之前，先攒几份带人工标注的真实素材做基线，否则无法判断改动是变好还是变坏。

标注放在素材同目录，命名 `<素材名>.ref.json`：

```json
{
  "speakers": [
    {"start_ms": 0,    "end_ms": 5200, "spk": "张三"},
    {"start_ms": 5200, "end_ms": 9000, "spk": "李四"}
  ],
  "text": "这里是完整正确文本，用于算 CER（可省略）"
}
```

```bash
# 评测单份（自动跑管线再比对）
python benchmark_quality.py 会议录音.m4a --report 基线.json

# 改完代码后对比，看是改善还是退步
python benchmark_quality.py 会议录音.m4a --report 新.json --compare 基线.json
```

DER 用 pyannote.metrics 的标准实现（0.25 秒 collar 容差），CER 自写（中文按字、英文按词，
标点与全半角不计入）。`--no-run` 可只比对已有产物，跳过重跑。

2026-10-28 对标公开方案批次实测：
- 补字幕通道：首跑 15.3s（含分离+识别），事后加 `--srt` 只补对齐 9.0s，缓存文件数不变（字幕未进键），第三次运行正确跳过
- pyannote 新参数：`--num-speakers` 实跑通过，重叠区间写入 `overlaps` 字段 1 处。**分块映射当时只在 3 秒块上验过"能跑通"，没验说话人身份是否保住**——2026-11-02 用 31 秒双人素材复测发现 chunk=20s 时同一人被拆成两个标签、chunk=15s 时两人被合为一个，详见下方修复记录
- 声纹库在 pyannote 路径建库成功（此前被硬性跳过）
- 单测新增三层：`test_split_and_cluster.py`（含抓到的采样索引/毫秒混用缺陷）、`test_pyannote_chunking.py`（锁住 `get_overlap()` 返回 Timeline 可迭代、无 `itersegments` 的实测 API）、`benchmark_quality.py --selftest`
- 全部 7 组单测 + e2e 19 项断言 ALL PASS

2026-11-02 分块丢说话人修复与实跑验证（pyannote 模型首次在本机落地）：
- 素材：31.4 秒双人交替会议音频（Windows TTS + rubberband 降调模拟第二人），带人工标注 `.ref.json`
- 整段不分块：G0→A、G1→B，DER **13.61%**（漏检 0s / 误报 0s / 混淆 0.12s）。人物归属零错误，误差全在边界
- 分块后修复前：chunk=20s 多出 G2（同一人拆成两个标签），chunk=15s 时 G1 混了 A 和 B（两人合并）
- 修复后：chunk=900s / 30s / 20s 均正确分出两人并映射到 A/B；chunk=15s、10s 仍会失败，因为短块内 pyannote 自身聚不出多人（强制 `num_speakers=2` 也无效），已加 30 秒下限提示
- 完整管线实跑：`--meeting --diarize-engine pyannote --num-speakers 2 --names` 15.1 秒产出会议原文，时间戳、人名映射、docx 排版全部正确
- e2e 19 项断言 ALL PASS；9 组单测 ALL PASS

2026-10-01 健壮性改动后实测：`test_pipeline_e2e.py` 17 项断言 **ALL PASS**（自动合成 TTS 视频走完整链路，37 秒）。

2026-10-01 可用性完善后复测（同一套断言仍全绿，41 秒），另做专项验证：`--clean` 后目录只剩成稿、
`--log` 产出 26 行运行日志、无音轨视频归为「无音轨」而非抛 ffmpeg 原始输出、`--replace-file` 词典替换生效、
批量输出 `[i/N]` 与总耗时均值。

2026-10-08 v2.4 回流批次复测：`--flat` 平铺输出（成稿落素材目录、工作子目录全清、幂等跳过）、
根目录 `hotwords.txt`/`replace_dict.txt` 零参数自动加载、词典内容进缓存指纹（改词典自动重转）、
Demucs 子进程 `HF_HUB_OFFLINE=1`（离线环境省约 30 秒 HuggingFace 重试等待）；
单测新增 cloud 引擎离线用例组（§14），CI 工作流纳入 py_compile + 全量离线单测。

实测性能参考（RTX 5070 Ti 16GB，28 分钟中文会议）：

| 环节 | 耗时 |
| --- | --- |
| 人声分离（Demucs） | ~1 分钟 |
| 说话人分离（pyannote） | ~40 秒 |
| 转写（Qwen3-ASR） | ~87 秒 |
| **全流程** | **约 4 分钟** |

---

## 🔧 技术要点与已知限制

**关键设计**

- **噪声门（必需）**：Demucs 在静音段输出低电平伪影，会让下游 VAD 失效（整段连成一块、说话人分离报废）。链路在分离与转写之间插入 `agate` 噪声门恢复段间真静音。
- **三层声纹防护**（cam++ 引擎）：碎段合并（<400ms）→ 超长段切分（>60s，保批量转写吞吐）→ 小簇吸收（<5s 的碎簇并入最相似大簇）。
- **pyannote 优先**：实测真实会议抢话交叠场景，pyannote 分离质量显著优于声纹聚类路线。
- **切点吸附静音**：超长段切分不再纯等分——在每个理想切点前后搜 50ms 窗的最低能量位置，把切点移到声门闭合处。实测等分点常落在词中间，接缝处出现残字或重复；吸附后跨句截断明显减少。纯函数无音频时退化为等分（保持可测）。
- **分块降内存**：pyannote 整段波形按时长线性占内存（16k 单声道 float32 约 230MB/小时，再复制一份翻倍）。超 15 分钟自动分块，块间留 30 秒重叠，用 pyannote 自带的 WeSpeaker 声纹把各块局部标签并到全局标签——同一人跨块会各自得到独立标签，不映射就会凭空多出说话人。声纹按段独立判定，与块边界位置无关；时间重合投票只作声纹不可用时的兜底，它在块边界正好切在换人处时会投错边。块长低于 30 秒会提示：实测 10 秒块内 pyannote 会把两人合并成单标签，且强制人数约束也救不回来。
- **重叠不强行归属**：`get_overlap()` 的区间单独写进 `overlaps` 字段。抢话段交给两人中任意一个都是错的，标记出来比二选一诚实。
- **torchcodec 规避**：pyannote 引入的 torchcodec 与静态 FFmpeg 不兼容；链路用 soundfile 预载波形绕过，建议 `pip uninstall torchcodec`。
- **AED 显存三层防护**（v2.1）：FireRedASR2-AED 引擎经 `firered_mem_patch.py` 分块注意力（softmax 按行独立，数学等价）+ 时长感知分批（`AED_BATCH_BUDGET_S`）+ OOM 拆批/静音点二分兜底，再配 bf16（`use_half=True`）与 decoder 三角 mask 缓存；实测 4×107s 批量峰值显存 11.8GB → 3.5GB、推理提速 3 倍（不改官方源码，monkey-patch 可还原）。

**已知限制**

- 会议**抢话极严重**时说话人分离仍可能过切（实测 35 簇 → 2 簇已大幅改善，但不保证 100% 准确）；重叠区间只做标记，不拆分同一时段的双人文本
- 说话人编号在不同次运行间可能互换（可用 `--names` 固定映射；`--speaker-db` 可跨文件复用身份）
- FireRedASR2-AED 为可选备选引擎，需另行下载模型与代码（v2.1 起长音频已优化，见上）
- Qwen3-ForcedAligner 需单独下载（约 1.2GB）；未下载时字幕回落到中文专用的 fa-zh，非中文素材会产出错位时间戳
- WeTextProcessing 依赖 pynini，Windows 下需 MSVC 编译；装不上时 ITN 用自写规则，覆盖窄一些但不影响可用性
- 输出 docx 的排版规范依赖 `article-format` 技能（未安装时产出标准 docx，无规范排版）
- 质量基准需要人工标注的真实素材；没有基线时，各项优化的效果只能靠主观判断

---

## 📄 License

[MIT](LICENSE)
