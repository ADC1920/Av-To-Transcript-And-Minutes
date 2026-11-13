# 本地转写引擎（双引擎：Qwen3-ASR-1.7B 默认 / FireRedASR2-AED 可选）
# 混搭方案：识别引擎（Qwen3-ASR 或 FireRedASR2-AED）+ FunASR fsmn-vad/CAM++ 说话人分离
#           + 字级时间戳/SRT + 专名确定性纠错 + FireRedVAD 可选 VAD + ZipEnhancer 可选降噪
# 与技能 funasr-transcribe 的关系：识别引擎从 Fun-ASR-Nano 升级为 Qwen3-ASR-1.7B；
# 分环逻辑（VAD 切段 -> 逐段声纹 -> 余弦层次聚类）与技能 diarize.py 完全一致。
#
# 对接真相表（证据 = 本机源码/实测）:
#   构造输入  processor.apply_transcription_request(audio, language, prompt, return_tensors="pt")
#             -> processing_qwen3_asr.py:494 (chat template + assistant prefill "language <NAME><asr_text>")
#   生成      Qwen3ASRForConditionalGeneration.generate(**inputs) -> modeling_qwen3_asr.py 头部 Example
#   解析      processor.decode(ids, return_format="parsed") -> {"language","transcription"}
#   音频输入  本地路径/URL/numpy，或其列表（批量）；路径模式由 processor 内部读取重采样
#   模型仓库  Qwen/Qwen3-ASR-1.7B-hf（ModelScope, -hf 后缀 = transformers 原生格式）, 4.08GB
#   备选引擎  FireRedASR2-AED（ModelScope `xukaituo/FireRedASR2-AED`, 4.73GB; 代码克隆到 ~/.local/fire-red-asr2s）
#             FireRedAsr2.from_pretrained("aed", model_dir, cfg).transcribe(uttids, wav_paths)
#             -> [{'uttid','text','confidence','timestamp':[('字',start_s,end_s),...]}]  (字级时间戳原生)
#             输入限制: kaldiio.load_mat 只认 16k 单声道 wav（mp3 会报 read_ascii_mat 错）→ 脚本内自动转
#             实测: 中/英/粤/噪声音频优于 Qwen3-ASR；日文不支持（输出中文乱码）；
#                   输出无标点 → 可选 ct-punc 补标点（--no-punc 关闭）；首调约 7-8s 预热,之后 2.7ms/秒音频
#   强制对齐  funasr AutoModel("fa-zh") = iic/speech_timestamp_prediction-v1-16k-offline (38M)
#             调用 input=(wav, text) / ([wavs],[texts])，data_type=("sound","text")
#             输出 {"text": "字 字 字"(空格分隔), "timestamp": [[start_ms,end_ms],...]}
#             实测: 标点自动跳过（13 汉字+标点输入 -> 13 token/13 时间戳）；66s 单次对齐 OK
#   专名纠错  funasr.utils.postprocess_hotwords.build_postprocess_hotword_matcher
#             确定性 dict{错:对} 替换；--fuzzy 走拼音模糊（需 pypinyin+rapidfuzz）
#   VAD 选择  fsmn-vad（默认，随 funasr）/ fireredvad（pip 包 + xukaituo/FireRedVAD 缓存）
#   显存优化  ① firered_mem_patch.apply(): 分块注意力 monkey-patch（不改 clone 源码，softmax
#             按行独立故与整块计算严格等价；编码器 ac/bd 就地累加+及时 del）
#             ② AED 批量改时长感知（aed_batch_plan: 段数≤--batch-size 且批内总时长
#             ≤AED_BATCH_BUDGET_S），替代固定 8 段一批——60s 段×8 是注意力显存峰值主因
#             ③ FireRed transcribe 内部吞掉一切异常（asr.py:118-133），OOM 表现=整批
#             空文本而非抛错 → aed_transcribe_oom_safe 以空结果为重试信号：批拆单 →
#             单段静音点二分递归（split_wav_at_quiet），文本拼接+时间戳平移合并
#             ④ use_half=True 走 bf16（Blackwell 张量核，权重/激活减半；配套在 firered_mem_patch：
#             decoder 三角 mask 按 (size,device) 缓存、forced_align 前 enc_outputs cast fp32）
#
# 路径安全边界: 输入拒绝 ".." 且 realpath 规范化；输出 JSON/SRT 锁定音频同目录；
#               临时分段 wav 锁定系统临时目录；落盘前均显式校验目录边界。
#
# 用法:
#   python qwen_asr.py 会议录音.m4a                     # 整段转写（自动语言检测）
#   python qwen_asr.py 会议录音.m4a --srt               # 额外输出 .srt 字幕（字级时间戳）
#   python qwen_asr.py 会议录音.m4a --replace "开饭时间=>开放时间"   # 专名确定性纠错
#   python qwen_asr.py 采访.m4a --diarize --names "0=张三,1=李四"   # 说话人分离 + 重命名
#   python qwen_asr.py 访谈.m4a --diarize --threshold 0.7           # 聚类阈值调节
#   python qwen_asr.py 录音.m4a --engine aed            # 换 FireRedASR2-AED（中文更准；日文不支持）
#   python qwen_asr.py 录音.m4a --vad firered            # 换 FireRedVAD 切段
#   python qwen_asr.py 嘈杂.m4a --denoise                # ZipEnhancer 前处理（含 BGM/强噪时试）
#   python qwen_asr.py 录音.m4a --engine cloud           # 云端默认 qwen-audio-3.1-asr-message（整文件直出句级+字级时间戳,免显存）
#   python qwen_asr.py 录音.m4a --engine cloud --cloud-model omni   # 云端 qwen3.8-omni-flash（全模态转写,纯文本）
#   python qwen_asr.py --engine cloud --cloud-model filetrans --cloud-url <公网音频URL> --diarize
#                                                        # 云端异步转写（仅公网 URL;自带说话人分离+字级时间戳）
import argparse, atexit, json, os, re, shutil, sys, tempfile, time
from pathlib import Path
import numpy as np

# 显存碎片优化(前瞻):expandable_segments 可减少长音频批量推理下 PyTorch 缓存分配器的
# 碎片虚高;2026-10-11 实测 Windows 暂不支持(告警 "not supported on this platform" 后
# 回落默认分配器,无副作用,每进程一条告警),保留此设置待 PyTorch 支持后自动生效。
# 须在 torch 初始化前设置,用户已显式设置时不覆盖
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

REPO_ID = "Qwen/Qwen3-ASR-1.7B-hf"
MS_CACHE = Path(os.path.expanduser("~")) / ".cache" / "modelscope" / "models" / "Qwen--Qwen3-ASR-1.7B-hf" / "snapshots" / "master"
AED_MODEL_DIR = Path.home() / ".cache" / "modelscope" / "models" / "xukaituo--FireRedASR2-AED"
AED_CODE_DIR = Path.home() / ".local" / "fire-red-asr2s"
FIRERED_VAD_DIR = Path(os.path.expanduser("~")) / ".cache" / "fireredvad" / "FireRedVAD" / "VAD"
# pyannote 说话人分离（社区版 community-1，ModelScope 镜像含 segmentation/embedding/plda 全套）
PYANNOTE_DIR = Path.home() / ".cache" / "modelscope" / "models" / \
    "pyannote--speaker-diarization-community-1" / "snapshots" / "master"
AUTO_SEGMENT_S = 60  # 超过此时长自动走 VAD 分段批量转写(实测比整段自回归快约 20%,且每段语言独立检测)
VAD_MAX_END_SILENCE_MS = 800  # VAD 断句静音阈值(funasr 配置默认值);显式传值以关闭 1.4.19 的动态静音
AED_BATCH_BUDGET_S = 240  # AED 单次前向的批内总音频时长预算(bf16 后显存余量足,120→240 换吞吐)
AED_MIN_SPLIT_S = 10.0    # OOM 兜底重试时单段最短时长,短于此不再二分
AED_MAX_SPLIT_DEPTH = 2   # OOM 兜底二分深度上限(2 → 最小拆到原段 1/4)
# pyannote 长音频分块：整段波形驻留内存按时长线性增长（16k 单声道 float32 约 230MB/小时，
# 再经 torch.from_numpy 复制一份翻倍）。分块后峰值只与块长相关，与总时长无关。
# 块间留重叠：同一说话人跨块会各自得到不同标签，靠重叠区的声纹相似度找回对应关系。
PYANNOTE_CHUNK_S = 900     # 单块上限(秒)，短于此不分块
PYANNOTE_OVERLAP_S = 30    # 块间重叠(秒)，供跨块说话人标签映射
PYANNOTE_EMB_THRESHOLD = 0.5   # 跨块声纹同人判定阈值（实测同人 0.72-0.77 / 异人 0.11-0.17）
PYANNOTE_MIN_CHUNK_S = 30  # 块长下限(秒)：实测 10s 块内 pyannote 把两人合并成单标签，
                           # 且强制 num_speakers=2 也救不回来（上下文太短，聚类无从展开）

# 转写结果 JSON 的 schema 版本。lines 结构一旦改动就升这个号；读取方（主脚本缓存复用、
# benchmark_quality.py）按版本决定能否消费，版本不认识就按未命中处理重转，
# 避免旧缓存字段对不上时静默丢信息。改字段请同步 SCHEMA_VERSION 与 _schema_ok。
SCHEMA_VERSION = 1
# Qwen3 强制对齐模型（--align-engine native 时用；与 ASR 主模型分开，需另行下载约 1.2GB）
ALIGNER_MS = Path(os.path.expanduser("~")) / ".cache" / "modelscope" / "models" / \
    "Qwen--Qwen3-ForcedAligner-0.6B" / "snapshots" / "master"
PUNCT = set("，。！？；：、,.!?;:…—～~「」『』“”‘’（）()《》〈〉【】[]\"'· \t\n\r")
HARD_STOP = "。！？；!?;"
SOFT_STOP = "，,、：:"

# ---- cloud 引擎（阿里云百炼,格式经 2026-10-07 真实探针验证）----
# message: dashscope SDK WebSocket,本地 wav 直传,整文件直出句级+字级时间戳(diarize 时逐段)
# omni: OpenAI 兼容接口,data:;base64 音频 + 转写 prompt(无时间戳);filetrans: 异步任务,仅公网 URL,
# 提交后轮询 tasks,结果 JSON 在 transcription_url(24h 有效);三款均经 2026-10-07 真实探针验证
CLOUD_BASE = "https://dashscope.aliyuncs.com"
CLOUD_MODELS = {"message": "qwen-audio-3.1-asr-flash-message",
                "omni": "qwen3.8-omni-flash",
                "filetrans": "qwen-audio-3.1-asr-flash-filetrans"}


def resolve_model_path():
    return str(MS_CACHE) if MS_CACHE.is_dir() else REPO_ID


# 进程级模型缓存：批量清单模式（--batch-manifest）在同一进程内处理多个素材，
# 主引擎按 device 常驻，第 2 个素材起不再重载几 GB 权重。单文件调用时缓存生命周期
# 就是进程本身，行为与改动前等价；返回值里的加载耗时按 0 报，便于区分「命中缓存」。
_MODEL_CACHE = {}


def _cache_get(key):
    return _MODEL_CACHE.get(key)


def load_model(device):
    """引擎 1(默认): Qwen3-ASR-1.7B,transformers 原生直载"""
    key = ("qwen", str(device))
    hit = _cache_get(key)
    if hit is not None:
        return hit[0], hit[1], 0.0
    import torch
    from transformers import AutoProcessor, Qwen3ASRForConditionalGeneration
    t0 = time.time()
    processor = AutoProcessor.from_pretrained(resolve_model_path())
    model = Qwen3ASRForConditionalGeneration.from_pretrained(
        resolve_model_path(), dtype=torch.bfloat16)
    model.to(device)
    model.eval()
    _MODEL_CACHE[key] = (model, processor)
    return model, processor, time.time() - t0


def load_aed_model(device):
    """引擎 2: FireRedASR2-AED（中文/英文/粤语实测优于 Qwen3-ASR；不支持日文；输出无标点）"""
    import sys
    key = ("aed", str(device))
    hit = _cache_get(key)
    if hit is not None:
        return hit[0], None, 0.0
    if not (AED_MODEL_DIR / "model.pth.tar").is_file():
        sys.exit(f"FireRedASR2-AED 模型不存在: {AED_MODEL_DIR}\n"
                 f"下载: python -c \"from modelscope import snapshot_download; "
                 f"snapshot_download('xukaituo/FireRedASR2-AED', local_dir=r'{AED_MODEL_DIR}')\"")
    if not (AED_CODE_DIR / "fireredasr2s").is_dir():
        sys.exit(f"FireRedASR2S 代码目录不存在: {AED_CODE_DIR}\n"
                 f"克隆: git clone https://github.com/FireRedTeam/FireRedASR2S.git \"{AED_CODE_DIR}\"")
    if str(AED_CODE_DIR) not in sys.path:
        sys.path.insert(0, str(AED_CODE_DIR))
    from fireredasr2s.fireredasr2 import FireRedAsr2, FireRedAsr2Config
    import firered_mem_patch
    t0 = time.time()
    cfg = FireRedAsr2Config(
        use_gpu=str(device).startswith("cuda"), use_half=True, beam_size=3, nbest=1,
        decode_max_len=0, softmax_smoothing=1.25, aed_length_penalty=0.6, eos_penalty=1.0,
        return_timestamp=True,
    )
    model = FireRedAsr2.from_pretrained("aed", str(AED_MODEL_DIR), cfg)
    print(f"[info] {firered_mem_patch.apply()}", file=sys.stderr)
    _MODEL_CACHE[key] = (model, None)
    return model, None, time.time() - t0


def load_punc(device):
    """ct-punc 标点恢复(AED 引擎输出无标点,用它补;加载失败返回 None 不阻断)"""
    try:
        from funasr import AutoModel
        return AutoModel(model="ct-punc", device=device, disable_update=True)
    except Exception as e:
        print(f"[warn] ct-punc 加载失败,跳过补标点: {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
        return None


def gc_cuda():
    """显存缓存回收:小模型阶段(VAD/声纹/对齐/补标点)结束后调用,削与主引擎转写峰值的叠加;无 CUDA 时 no-op"""
    import torch
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def load_speaker_db(path) -> dict:
    """读声纹库 {"version":1,"speakers":{"张三":[...]}} → {name: 归一化 ndarray}"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        out = {}
        for name, vec in (raw.get("speakers") or {}).items():
            arr = np.asarray(vec, dtype=np.float32).flatten()
            n = float(np.linalg.norm(arr)) if arr.size else 0.0
            if n > 0:
                out[name] = arr / n          # 存进来就归一化，比对时只算点积
        return out
    except Exception:
        return {}


def save_speaker_db(path, db: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1,
               "speakers": {k: [round(float(x), 6) for x in np.asarray(v).flatten()]
                            for k, v in db.items()}}
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def match_speaker_db(centroid, db: dict, threshold: float):
    """库中比对：返回 (最佳名字, 相似度)；低于阈值时名字为 None 但仍回报最高相似度"""
    if centroid is None or not db:
        return None, -1.0
    c = np.asarray(centroid, dtype=np.float32).flatten()
    n = float(np.linalg.norm(c))
    if n <= 0:
        return None, -1.0
    c = c / n
    best, best_sim = None, -1.0
    for name, vec in db.items():
        if vec.shape != c.shape:
            continue
        sim = float(np.dot(c, vec))
        if sim > best_sim:
            best, best_sim = name, sim
    return (best if best_sim >= threshold else None), best_sim


def analyze_emotion(audio_path, device):
    """emotion2vec+large 整段情绪分析，返回分数最高的前三标签；失败返回 None 不阻断。

    能力边界（勿夸大）：它做的是 8 类情绪识别（开心/难过/厌恶/中立/生气/惊讶/害怕/兴奋），
    **不是音频事件检测**——笑声、掌声这类事件它识别不了，那需要另装事件检测模型。

    显存随时间增长：整段音频一次性进模型，没有 VAD 分段。实测 RTX 4060 8GB 上，
    8.5 秒素材峰值 0.77GB，5 分钟到 2.8GB，10 分钟直接 OOM（要分配 3.66GB 时无可用）。
    所以长素材开 --emotion 会静默丢掉情绪标签，只能靠 stderr 的 warn 看出来。
    """
    try:
        from funasr import AutoModel
        emo = AutoModel(model="iic/emotion2vec_plus_large", device=device, disable_update=True)
        r = emo.generate(input=str(audio_path), cache={}, granularity="utterance")
        pairs = sorted(zip(r[0]["labels"], r[0]["scores"]), key=lambda x: -x[1])[:3]
        return [{"label": str(l), "score": round(float(s), 4)} for l, s in pairs]
    except Exception as e:
        msg = f"{type(e).__name__}: {str(e)[:120]}"
        if "out of memory" in str(e).lower():
            msg += ("（显存不足：emotion2vec 整段推理不分段，长素材峰值随时长上涨。"
                    "可对长素材去掉 --emotion，或裁短后单独跑情绪）")
        print(f"[warn] emotion2vec 分析失败，跳过情绪标注: {msg}", file=sys.stderr)
        return None


def transcribe_batch(model, processor, audio_paths, language=None, hotwords=None, max_new_tokens=2048,
                     t2s=None, matcher=None, punc=None, engine="qwen", aed_ctx=None, itn=None):
    """批量转写,返回 [{"language","transcription"}, ...]
    Qwen 引擎: 支持 language/hotwords 偏置、自带标点与语言识别
    AED 引擎: 忽略 language/hotwords（模型无此接口）；输出无标点,经 ct-punc 补；时间戳由 AED 原生提供；
              aed_ctx 提供时走 OOM 兜底（空结果自动拆小重试）"""
    if engine == "aed":
        uttids = [f"u{i}" for i in range(len(audio_paths))]
        if aed_ctx is not None:
            results = aed_transcribe_oom_safe(model, audio_paths, aed_ctx)
        else:
            results = model.transcribe(uttids, list(audio_paths))
        # AED 内部异常时会返回空列表或短列表（feat_extractor/beam search 失败均如此），
        # 必须补齐到与输入等长（缺失项记空文本），否则下游 zip 截断会漏赋字段
        if len(results) < len(audio_paths):
            print(f"[warn] AED 返回 {len(results)} 条 / 输入 {len(audio_paths)} 条，"
                  f"缺失段按空文本补齐", file=sys.stderr)
            results = list(results) + [{"uttid": u, "text": ""}
                                       for u in uttids[len(results):]]
        texts = [(r.get("text") or "").strip() for r in results]
        # ct-punc 批量调用(逐段调用实测 111 段多花 17s;批量后降到亚秒级)
        if punc is not None and any(texts):
            try:
                punc_out = punc.generate(input=[t if t else "。" for t in texts])
                texts = [(p.get("text") or t).strip() for p, t in zip(punc_out, texts)]
            except Exception:
                pass
        parsed = []
        for r, txt in zip(results, texts):
            if t2s is not None:
                txt = t2s(txt)
            if itn is not None:
                txt = itn(txt)          # 繁简之后做 ITN：规则表按简体编写
            parsed.append({"language": None, "transcription": apply_replace(txt, matcher),
                           "timestamp": r.get("timestamp"), "confidence": r.get("confidence")})
        return parsed
    import torch
    import soundfile as sf
    # 传 numpy 波形而非文件路径：路径形式会让 processor 内部用 torchcodec 解码，
    # 而 torchcodec 与静态 FFmpeg 不兼容（装 pyannote.audio 时会一并引入），
    # 报 libtorchcodec_core*.dll 加载失败。numpy 形式直接进特征提取，绕开这条依赖。
    audio_inputs = []
    for p in audio_paths:
        data, sr = sf.read(str(p), dtype="float32", always_2d=False)
        if data.ndim > 1:
            data = data.mean(axis=1)          # 多声道混成单声道，与 processor 内部行为一致
        if sr != 16000:
            import librosa
            data = librosa.resample(data, orig_sr=sr, target_sr=16000)
        audio_inputs.append(data)
    inputs = processor.apply_transcription_request(
        audio=audio_inputs, language=language, prompt=hotwords, return_tensors="pt")
    inputs = {k: (v.to(model.device) if torch.is_tensor(v) else v) for k, v in inputs.items()}
    # 音频特征 extractor 输出 float32,与 bf16 模型权重对齐(实测 conv 层要求同型)
    if "input_features" in inputs and torch.is_tensor(inputs["input_features"]):
        inputs["input_features"] = inputs["input_features"].to(model.dtype)
    with torch.inference_mode():
        gen = model.generate(**inputs, do_sample=False, max_new_tokens=max_new_tokens)
    out_ids = gen[:, inputs["input_ids"].shape[1]:]
    parsed = processor.decode(out_ids, return_format="parsed")
    for item in parsed:
        if t2s is not None and item.get("language") in ("Chinese", "Cantonese"):
            item["transcription"] = t2s(item["transcription"])
        # ITN 只对中文有意义（规则表是中文数字），免去对英文段做无谓正则
        if itn is not None and item.get("language") in ("Chinese", "Cantonese", None):
            item["transcription"] = itn(item["transcription"])
        item["transcription"] = apply_replace(item["transcription"], matcher)
    return parsed


def build_t2s(disabled):
    """繁->简转换器:默认启用,仅对中文/粤语输出生效(日文等语言汉字不误转)"""
    if disabled:
        return None
    from opencc import OpenCC
    return OpenCC("t2s").convert


def build_matcher(replace_map, fuzzy):
    """专名纠错 matcher:确定性替换(dict) + 可选拼音模糊;返回可 apply_text 的对象或 None"""
    if not replace_map:
        return None
    from funasr.utils.postprocess_hotwords import build_postprocess_hotword_matcher
    return build_postprocess_hotword_matcher(
        postprocess_hotwords=replace_map, enable_fuzzy=bool(fuzzy))


def apply_replace(text, matcher):
    if matcher is None:
        return text
    return matcher.apply_text(text)[0]


# ---------------- cloud 引擎（百炼 API） ----------------

def load_dashscope_key():
    """DASHSCOPE_API_KEY:环境变量优先,回退注册表 HKCU\\Environment(会话中途写入注册表也能取到)"""
    k = os.environ.get("DASHSCOPE_API_KEY")
    if k:
        return k
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as reg:
            k, _ = winreg.QueryValueEx(reg, "DASHSCOPE_API_KEY")
        return k
    except (OSError, ImportError):
        sys.exit("cloud 引擎需要 DASHSCOPE_API_KEY(未在环境变量与 HKCU\\Environment 中找到)")


def cloud_post(url, headers, body, timeout=300, retries=2, log=print):
    """POST,429/5xx/网络异常按 3s/7.5s 退避重试;成功返回 json,其余 sys.exit"""
    import requests
    delay = 3
    for i in range(retries + 1):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504) and i < retries:
                log(f"[warn] 云端返回 {r.status_code},{delay}s 后重试({i + 1}/{retries})")
                time.sleep(delay)
                delay *= 2.5
                continue
            sys.exit(f"云端接口失败 HTTP {r.status_code}: {r.text[:300]}")
        except requests.RequestException as e:
            if i < retries:
                log(f"[warn] 网络异常 {type(e).__name__},{delay}s 后重试")
                time.sleep(delay)
                delay *= 2.5
                continue
            sys.exit(f"云端接口网络失败: {type(e).__name__}: {e}")


def _cloud_hotword_vocab(hotwords, log=None):
    """顿号/逗号分隔的热词串 -> 即时词表 {词: 4}(文档推荐权重);随请求传入,免预创建与生命周期管理"""
    words = [w.strip() for w in (hotwords or "").replace("，", ",").replace("、", ",").split(",") if w.strip()]
    if not words:
        return None
    if len(words) > 50:
        if log:
            log(f"[提示] 热词 {len(words)} 条,仅取前 50 条")
        words = words[:50]
    return {w: 4 for w in words}


def _cloud_message_seg(path, ctx):
    """message 通道单段/单文件:dashscope SDK(WebSocket),本地 wav 直传;
    返回 (全文, 字级时间戳[[start_ms, end_ms], ...] 相对本段起点)"""
    try:
        import dashscope
        from dashscope.audio.asr import Recognition
    except ImportError:
        sys.exit("message 通道需要 dashscope SDK：pip install dashscope（omni/filetrans 通道无需）")
    dashscope.api_key = ctx["key"]
    kw = {}
    vocab = _cloud_hotword_vocab(ctx.get("hotwords"))
    if vocab:
        kw["vocabulary"] = vocab
    rec = Recognition(model=CLOUD_MODELS["message"], format="wav", sample_rate=16000,
                      callback=None, **kw)
    res = rec.call(str(path))
    if getattr(res, "status_code", None) != 200:
        sys.exit(f"message 通道失败: {getattr(res, 'message', res)}")
    sents = res.get_sentence() or []
    text = "".join((s.get("text") or "") for s in sents).strip()
    ts = []   # 词级时间戳 -> 字符级(词内均分),供 subtitle_units 复用
    for s in sents:
        for w in (s.get("words") or []):
            chars = [c for c in (w.get("text") or "") if c not in PUNCT]
            if not chars:
                continue
            wb, we = int(w.get("begin_time") or 0), int(w.get("end_time") or 0)
            nn = len(chars)
            ts.extend([[wb + (we - wb) * i // nn, wb + (we - wb) * (i + 1) // nn] for i in range(nn)])
    return text, ts

def _cloud_omni_seg(path, ctx):
    """omni 通道单段:OpenAI 兼容,音频 data:;base64 直传,要求只输出转写文本"""
    import base64
    b64 = base64.b64encode(Path(path).read_bytes()).decode()
    content = [{"type": "input_audio", "input_audio": {"data": "data:;base64," + b64, "format": "wav"}}]
    prompt = "请将这段音频完整转写为文字，只输出转写结果，不要任何附加说明。"
    if ctx.get("hotwords"):
        prompt = "可能出现的术语(仅供参考,不要输出此列表):" + ctx["hotwords"] + "。" + prompt
    content.append({"type": "text", "text": prompt})
    body = {"model": CLOUD_MODELS["omni"], "messages": [{"role": "user", "content": content}],
            "modalities": ["text"], "reasoning_effort": "none"}
    j = cloud_post(CLOUD_BASE + "/compatible-mode/v1/chat/completions",
                   {"Authorization": f"Bearer {ctx['key']}", "Content-Type": "application/json"},
                   body, log=ctx["log"])
    return ((j.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()


def cloud_transcribe_batch(audio_paths, ctx, t2s=None, matcher=None, itn=None):
    """cloud 同步引擎逐段转写,返回与 transcribe_batch 同构的 parsed
    ctx: {key,model,log,hotwords};model 为 message/omni 之一(message 走 SDK WebSocket,
    omni 走 OpenAI 兼容);云端自带标点与规范化,繁转简与专名纠错仍走本地后处理。
    4 线程并发上传(云端 RPM 600 远大于此),ex.map 按段序保序;文本后处理回主线程做
    (t2s/itn/matcher 不保证线程安全);message 段回传字级时间戳,存入 cloud_words 供字幕复用"""
    from concurrent.futures import ThreadPoolExecutor

    def one(p):
        if ctx["model"] == "message":
            return _cloud_message_seg(p, ctx)
        return _cloud_omni_seg(p, ctx), None

    with ThreadPoolExecutor(max_workers=4) as ex:
        raw = list(ex.map(one, audio_paths))
    out = []
    for txt, cts in raw:
        if t2s is not None:
            txt = t2s(txt)
        if itn is not None:
            txt = itn(txt)
        item = {"language": None, "transcription": apply_replace(txt, matcher)}
        if cts:
            item["cloud_words"] = cts
        out.append(item)
    return out

def run_cloud_message(args, out_json, out_srt, matcher, t2s, itn_fn, log, t0, audio_path):
    """message 通道整文件旁路(非 diarize):SDK 一次调用直出句级文本+时间戳,
    无需本地 VAD 与 fa-zh 对齐;json(segments) 与 srt 与本地管线同构"""
    try:
        import dashscope
        from dashscope.audio.asr import Recognition
    except ImportError:
        sys.exit("message 通道需要 dashscope SDK：pip install dashscope（omni/filetrans 通道无需）")
    dashscope.api_key = load_dashscope_key()
    kw = {}
    vocab = _cloud_hotword_vocab(args.hotwords, log)
    if vocab:
        kw["vocabulary"] = vocab
        log(f"[0] 即时热词 {len(vocab)} 条已启用(随请求传入,免预创建)")
    rec = Recognition(model=CLOUD_MODELS["message"], format="wav", sample_rate=16000,
                      callback=None, **kw)
    res = rec.call(str(audio_path))
    if getattr(res, "status_code", None) != 200:
        sys.exit(f"message 通道失败: {getattr(res, 'message', res)}")
    sents = res.get_sentence() or []
    units, aligns = [], []
    for s in sents:
        # 字级 words -> 字符级 ts(词内均分),供 subtitle_units 按 --max-line 细分字幕
        ts = []
        for w in (s.get("words") or []):
            chars = [c for c in (w.get("text") or "") if c not in PUNCT]
            if not chars:
                continue
            b, e = int(w.get("begin_time") or 0), int(w.get("end_time") or 0)
            nn = len(chars)
            ts.extend([[b + (e - b) * i // nn, b + (e - b) * (i + 1) // nn] for i in range(nn)])
        aligns.append({"tokens": [], "ts": ts})
        txt = (s.get("text") or "").strip()
        if t2s is not None:
            txt = t2s(txt)
        if itn_fn is not None:
            txt = itn_fn(txt)
        units.append({"start_ms": int(s.get("begin_time") or 0), "end_ms": int(s.get("end_time") or 0),
                      "text": apply_replace(txt, matcher), "spk": None, "spk_name": None})
    # 字级 ts 与变换后文本去标点字数一致才启用细分(ITN 改长度时自动退回整句一条)
    good = all(len(a["ts"]) == sum(1 for ch in u["text"] if ch not in PUNCT)
               for a, u in zip(aligns, units))
    text = "".join(u["text"] for u in units)
    print(text)
    payload = {"schema_version": SCHEMA_VERSION, "language": None, "text": text,
               "engine": CLOUD_MODELS["message"],
               "audio": str(Path(os.path.realpath(args.audio))),
               "segments": [{"start_ms": u["start_ms"], "end_ms": u["end_ms"], "text": u["text"]} for u in units]}
    if args.srt or getattr(args, "vtt", False):
        subs = subtitle_units(units, aligns if good else None, args.max_line, args.min_dur, args.max_cps)
        payload["sentences"] = subs
        write_subtitles(subs, args, out_srt, use_speaker=False)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[2] 完成 (总耗时 {time.time()-t0:.0f}s), 结构化结果: {out_json}"
        + (f", 字幕: {out_srt}" if args.srt else ""))

def run_cloud_filetrans(args, out_json, out_srt, matcher, t2s, itn_fn, names, log, t0, audio_path):
    """filetrans 旁路:整文件提交异步任务(仅公网 URL),轮询取句级时间戳+可选说话人,
    输出与本地管线同构的 stdout 全文 + json(segments/sentences) + srt"""
    import requests
    if not args.cloud_url:
        sys.exit("--cloud-model filetrans 只接受公网音频 URL(--cloud-url 本地路径无效);"
                 "本地文件请用 --cloud-model 3.1(自动分段逐段上传)")
    key = load_dashscope_key()
    h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
         "X-DashScope-Async": "enable"}
    body = {"model": CLOUD_MODELS["filetrans"], "input": {"file_url": args.cloud_url},
            "parameters": {}}
    if args.language:
        body["parameters"]["language_hints"] = [args.language]
    if args.diarize:
        body["parameters"]["diarization_enabled"] = True
    r = requests.post(CLOUD_BASE + "/api/v1/services/audio/asr/transcription",
                      headers=h, json=body, timeout=60)
    if r.status_code != 200:
        sys.exit(f"filetrans 提交失败 HTTP {r.status_code}: {r.text[:300]}")
    task_id = (r.json().get("output") or {}).get("task_id")
    if not task_id:
        sys.exit(f"filetrans 提交未返回 task_id: {r.text[:300]}")
    log(f"[1] filetrans 任务已提交: {task_id}(轮询中,长音频可能需要数分钟)")
    qh = {"Authorization": f"Bearer {key}"}
    jq, last_ping = None, time.time()
    deadline = time.time() + 7200
    while time.time() < deadline:
        time.sleep(3)
        rq = requests.get(CLOUD_BASE + f"/api/v1/tasks/{task_id}", headers=qh, timeout=30)
        jq = rq.json()
        st = (jq.get("output") or {}).get("task_status")
        if st in ("SUCCEEDED", "FAILED", "CANCELED"):
            break
        if time.time() - last_ping > 30:
            log(f"[1b] 轮询中... 状态 {st or jq.get('code', rq.status_code)}")
            last_ping = time.time()
    st = (jq or {}).get("output", {}).get("task_status")
    if st != "SUCCEEDED":
        sys.exit(f"filetrans 任务未成功(状态 {st}): {json.dumps(jq or {}, ensure_ascii=False)[:300]}")
    tr_url = (((jq["output"]).get("results") or [{}])[0]).get("transcription_url")
    if not tr_url:
        sys.exit("filetrans 结果缺 transcription_url")
    tj = requests.get(tr_url, timeout=60).json()
    tr0 = (tj.get("transcripts") or [{}])[0]
    sents = tr0.get("sentences") or []
    text = (tr0.get("text") or "".join(s.get("text", "") for s in sents)).strip()
    units = []
    for s in sents:
        spk = s.get("speaker_id")
        units.append({"start_ms": int(s.get("begin_time") or 0), "end_ms": int(s.get("end_time") or 0),
                      "text": (s.get("text") or "").strip(), "spk": spk,
                      "spk_name": names.get(spk) if isinstance(spk, int) else None})
    for u in units:
        u["text"] = apply_replace(u["text"], matcher)
        if t2s is not None:
            u["text"] = t2s(u["text"])
        if itn_fn is not None:
            u["text"] = itn_fn(u["text"])
    if matcher or t2s is not None or itn_fn is not None:
        text = "".join(u["text"] for u in units) or text
    print(text)
    payload = {"schema_version": SCHEMA_VERSION, "language": args.language, "text": text,
               "engine": CLOUD_MODELS["filetrans"],
               "audio": str(audio_path), "url": args.cloud_url,
               "segments": [{k: u[k] for k in ("start_ms", "end_ms", "text", "spk")} for u in units]}
    if args.srt or getattr(args, "vtt", False):
        payload["sentences"] = units
        write_subtitles(units, args, out_srt, use_speaker=args.diarize)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[2] 完成 (总耗时 {time.time()-t0:.0f}s), 结构化结果: {out_json}"
        + (f", 字幕: {out_srt}" if args.srt else ""))

# ---------------- VAD / 对齐 / 字幕 ----------------

def vad_backend_available(name):
    """VAD 后端可用性。firered 需权重与包同时在；fsmn 由 funasr 自带（权重缺失时由 funasr 自己报错）。"""
    if name != "firered":
        return True
    if not (FIRERED_VAD_DIR / "model.pth.tar").is_file():
        return False
    try:
        import fireredvad  # noqa: F401  只在探测，真正加载在 run_vad 里
    except Exception:
        return False
    return True


def resolve_vad_backend(choice, for_diarization=False):
    """auto -> 按用途选后端：分离链路用 fsmn，纯分段链路用 FireRedVAD（装了才换）。

    为什么要分用途：VAD 在两处角色不同。
      · cam++ 分离链路：VAD 段就是要聚类的声纹单元，段越碎、声纹越不稳。同一段 25 秒
        双人合成素材实测 fsmn 4 段 4.53% DER，FireRedVAD 9 段 7.23% DER——边界更准
        却因为段被切碎、聚类粒度变差而整体更差。这条链路保持 fsmn。
      · --no-diarize 长音频分段：VAD 只用来定识别批边界，不参与聚类；此时边界越准、
        耗时越短越好，同一素材实测 8.5s → 0.37s（含两侧模型加载），换 FireRedVAD。
    显式传 fsmn/firered 时一律按用户指定，不做用途判断。
    """
    if choice and choice != "auto":
        return choice
    if not vad_backend_available("firered"):
        return "fsmn"
    return "fsmn" if for_diarization else "firered"


def run_vad(audio_path, backend, device):
    """返回 [[start_ms, end_ms], ...]；fsmn 走 funasr，firered 走 fireredvad"""
    if backend == "firered":
        import soundfile as sf
        from fireredvad import FireRedVad, FireRedVadConfig
        if not (FIRERED_VAD_DIR / "model.pth.tar").is_file():
            sys.exit(f"FireRedVAD 模型不存在: {FIRERED_VAD_DIR}（需先 modelscope download --model xukaituo/FireRedVAD）")
        cfg = FireRedVadConfig(use_gpu=str(device).startswith("cuda"))
        vad = FireRedVad.from_pretrained(str(FIRERED_VAD_DIR), cfg)
        # 两道坎，顺序都不能省：
        # ① fireredvad 的特征提取断言输入必须是 16kHz（core/audio_feat.py:30），而链路给进来的
        #    通常是 44.1kHz 立体声（Demucs 产物）；fsmn 路线由 funasr 自行重采样，这条必须自己转。
        # ② 必须落成 PCM_16 文件再喂：它的 fbank 按 int16 数值范围取特征，直接传 float32(-1..1)
        #    元组会让特征幅度小三万倍左右，模型概率趋近 0——不报错，但判成「全非语音」
        #    （实测 probs max 0.0031 对比文件路径 0.9998），属静默失效，比崩溃更难发现。
        data, sr = sf.read(str(audio_path), dtype="float32", always_2d=True)
        mono = data.mean(axis=1)
        if sr != 16000:
            import librosa
            mono = librosa.resample(mono, orig_sr=sr, target_sr=16000)
        vad_wav = Path(tempfile.gettempdir()) / f"firered_vad_{os.getpid()}_{int(time.time() * 1000)}.wav"
        sf.write(str(vad_wav), mono.astype(np.float32), 16000, subtype="PCM_16")
        try:
            result, _ = vad.detect(str(vad_wav))
        finally:
            vad_wav.unlink(missing_ok=True)
        return [[int(round(s * 1000)), int(round(e * 1000))] for s, e in result["timestamps"]]
    from funasr import AutoModel
    # 必须显式传 max_end_silence_time：funasr 1.4.19 起 fsmn-vad 默认启用「动态静音阈值」
    # （model.py:1012 `dynamic_silence = max_end_silence_time is None`），阈值随累计语音时长
    # 逐步放宽，实测 32 秒素材里 1.5 秒静音切不开，整段被判成一个语音段——下游「段数<2 跳过
    # 聚类」于是静默退化成单说话人，会议原文丢失全部说话人标签。显式传值时动态逻辑自动关闭。
    vad = AutoModel(model="fsmn-vad", device=device, disable_update=True,
                    max_end_silence_time=VAD_MAX_END_SILENCE_MS)
    return [[int(s), int(e)] for s, e in vad.generate(input=str(audio_path))[0]["value"]]


def aed_aligns_from(parsed):
    """AED 原生字级时间戳(秒,含字符) -> 与 fa-zh 同构的 aligns 结构(毫秒)
    注意: AED 时间戳对应的是补标点前的字符序列;补标点后文本多了标点,
          char_times/subtitle_units 会按去标点字符数比对,数量一致即按序映射"""
    out = []
    for p in parsed:
        ts = p.get("timestamp") or []
        out.append({"tokens": [t[0] for t in ts],
                    "ts": [[int(round(t[1] * 1000)), int(round(t[2] * 1000))] for t in ts]})
    return out


def aed_ts_usable(parsed):
    """AED 原生时间戳是否可用。

    判据是「时间戳 tokens 拼接后覆盖文本」，不是「个数相等」——AED 会把连续英文
    当成一个 token（实测「tiktok」1 个时间戳对 6 个字符），按字符数比必然失配，
    而失配会触发回落 fa-zh，fa-zh 在长音频上又可能抛
    `ValueError: time_stamp is missing a retained token timestamp`，整段失败。
    """
    for p in parsed:
        ts = p.get("timestamp") or []
        text = p.get("transcription") or ""
        if not ts:
            return False
        if "".join(str(t[0]) for t in ts) != "".join(c for c in text if c not in PUNCT):
            return False
    return True


def align_batch(fa_model, wav_paths, texts):
    """fa-zh 强制对齐;返回 [{"tokens":[...],"ts":[[s,e],...]}, ...],顺序与输入一致

    两处必要的防御，都源自长音频实测：
    ① 空文本不能送进模型。fa-zh 的 sentence_postprocess 要求每个保留 token 都有
       时间戳，空文本一个 token 都没有，必抛 `ValueError: time_stamp is missing a
       retained token timestamp` 并中断整批。AED 返回短列表时缺失段会被补成空文本
       （见 transcribe_batch），90 分钟素材里这种段几乎必然出现。
    ② 有文本的段也可能单条失败（噪音段、AED 幻听长文本与音频严重不匹配时，对齐器
       配不出时间戳同样抛上面那个错）。整批共用一次 generate，一条坏就全批丢；
       故按条调用并吞掉单条失败，坏条退化成空 ts，由 subtitle_units 走整段兜底。
       逐条比批量慢，但只在有字幕需求时走，且正确性优先——整批失败等于整份素材无字幕。"""
    out = []
    for wav, text in zip(wav_paths, texts):
        if not (text or "").strip():
            out.append({"tokens": [], "ts": []})
            continue
        try:
            res = fa_model.generate(input=([wav], [text]), data_type=("sound", "text"))
            item = res[0]
            out.append({"tokens": item["text"].split(), "ts": item["timestamp"]})
        except Exception as e:
            print(f"[warn] 对齐失败，该段改按跨度均分: {type(e).__name__}: "
                  f"{str(e)[:100]}（文本前 20 字：{text[:20]}）", file=sys.stderr)
            out.append({"tokens": [], "ts": []})
    return out


def aligner_available():
    """Qwen3 强制对齐模型是否已下载（--align-engine native/auto 的前置）"""
    return (ALIGNER_MS / "config.json").is_file()


def load_aligner(device):
    """Qwen3-ForcedAligner-0.6B：多语言字级对齐，替代中文专用的 fa-zh。
    与 ASR 主模型是两个独立模型，用 processor + 分类头模型各一份。"""
    import torch
    from transformers import AutoProcessor, Qwen3ASRForTokenClassification
    t0 = time.time()
    processor = AutoProcessor.from_pretrained(str(ALIGNER_MS))
    model = Qwen3ASRForTokenClassification.from_pretrained(
        str(ALIGNER_MS), dtype=torch.bfloat16).to(torch.device(device))
    model.eval()
    return model, processor, time.time() - t0


def align_batch_qwen(model, processor, wav_paths, texts, language=None, batch=8):
    """Qwen3 强制对齐：返回与 align_batch 同构的 [{"tokens","ts"}...]（毫秒）。
    一次前向给整批 logits，decode_forced_alignment 按 word_list 把时间戳配回词。
    处理失败的单条退化为空 ts，由 char_times 走跨度均分兜底，不阻断整批。
    空文本不进批（同 align_batch）：AED 短列表补齐出的空段没有可对齐的词，
    混在批里会让整批失败退化为均分，把有文本段的字级时间戳一起丢掉。
    批失败时逐条重试而非整批放弃：整批共用一次前向，一条超长或含异常字符的段
    就能让同批其余段一起丢掉字级时间戳，代价与收益不成比例。"""
    import torch
    out = [{"tokens": [], "ts": []} for _ in texts]
    lang = language if isinstance(language, (list, type(None))) else [language] * len(wav_paths)
    live = [i for i, t in enumerate(texts) if (t or "").strip()]

    def _run_once(idx):
        chunk_a = [wav_paths[j] for j in idx]
        chunk_t = [texts[j] for j in idx]
        chunk_l = ([lang[j] for j in idx] if isinstance(lang, list)
                   else [None] * len(chunk_a))
        inputs, word_lists = processor.prepare_forced_aligner_inputs(
            audio=chunk_a, transcript=chunk_t, language=chunk_l)
        inputs = {k: (v.to(model.device) if torch.is_tensor(v) else v)
                  for k, v in inputs.items()}
        with torch.inference_mode():
            logits = model(**inputs).logits
        return processor.decode_forced_alignment(
            logits, inputs["input_ids"], word_lists,
            timestamp_token_id=model.config.timestamp_token_id)

    for i in range(0, len(live), batch):
        idx = live[i:i + batch]
        try:
            decoded = _run_once(idx)
        except Exception as e:
            print(f"[warn] Qwen 对齐第 {i // batch + 1} 批失败，改逐条重试: "
                  f"{type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
            decoded = []
            for j in idx:
                try:
                    decoded.append(_run_once([j])[0])
                except Exception as e2:
                    print(f"[warn] 该段对齐失败，改按跨度均分: {type(e2).__name__}: "
                          f"{str(e2)[:100]}（文本前 20 字：{texts[j][:20]}）", file=sys.stderr)
                    decoded.append([])
        for j, items in zip(idx, decoded):
            out[j] = {"tokens": [it["text"] for it in items],
                      "ts": [[int(round(it["start_time"] * 1000)),
                              int(round(it["end_time"] * 1000))] for it in items]}
    return out


def char_times(text, tokens, ts, base_ms, fallback_start, fallback_end):
    """去标点字符序 -> [(start_ms,end_ms)]。

    优先按「token 展开」映射：AED 会把连续英文当成一个 token（「tiktok」1 个时间戳
    对 6 个字符），按 token/字符个数相等判断会误判失配。这里把每个 token 的时间跨度
    按字符数均分给它的字符，时间轴仍然准确。tokens 与文本对不上时才整段均分兜底。
    """
    plain_len = sum(1 for c in text if c not in PUNCT)
    if plain_len <= 0:
        return []
    plain_chars = [c for c in text if c not in PUNCT]
    if tokens and len(ts) == len(tokens) and "".join(str(t) for t in tokens) == "".join(plain_chars):
        out = []
        for tok, (s, e) in zip(tokens, ts):
            n = max(1, len(str(tok)))
            for k in range(n):
                out.append((int(s) + base_ms + (int(e) - int(s)) * k // n,
                            int(s) + base_ms + (int(e) - int(s)) * (k + 1) // n))
        return out
    if len(ts) == plain_len:
        return [(int(ts[k][0]) + base_ms, int(ts[k][1]) + base_ms) for k in range(plain_len)]
    # 兜底:在给定跨度内均分(对齐 token 与文本不匹配时,保时间轴合理)
    span = max(1, fallback_end - fallback_start)
    return [(fallback_start + span * i // plain_len, fallback_start + span * (i + 1) // plain_len)
            for i in range(plain_len)]


def split_sentences(text, max_len):
    """按标点切句,超长句再按软标点细分;返回 [(plain_start, plain_end, sentence)]
    plain 索引 = 去标点后的字符序(与 char_times 的键一致)"""
    # 字符位置 -> plain 索引(标点处为 None)
    plain_of, pi = [], 0
    for ch in text:
        if ch in PUNCT:
            plain_of.append(None)
        else:
            plain_of.append(pi)
            pi += 1
    # 硬标点切句(字符区间)
    spans, start = [], 0
    for i, ch in enumerate(text):
        if ch in HARD_STOP:
            if text[start:i + 1].strip():
                spans.append((start, i + 1))
            start = i + 1
    if start < len(text) and text[start:].strip():
        spans.append((start, len(text)))
    # 超长句按软标点细分
    pieces = []
    for cs, ce in spans:
        if len(text[cs:ce]) <= max_len:
            pieces.append((cs, ce))
            continue
        sub_start = cs
        for i in range(cs, ce):
            if text[i] in SOFT_STOP and (i - sub_start) >= max_len // 2:
                pieces.append((sub_start, i + 1))
                sub_start = i + 1
        if sub_start < ce:
            pieces.append((sub_start, ce))
    # 硬切兜底：上面只按软标点切，整段没有标点时（识别丢标点的长句很常见）切片
    # 会一直超过 max_len，字幕就出一行超长文本。这里按字符数补齐硬切，切点优先
    # 退到最近的空格/软标点，避免把英文单词劈开。
    if max_len >= 8:
        hard = []
        for cs, ce in pieces:
            cur = cs
            while ce - cur > max_len:
                cut = cur + max_len
                floor = max(cur + max_len * 3 // 4, cur + 1)
                for j in range(cut, floor, -1):
                    if text[j - 1] in SOFT_STOP or text[j - 1] == " ":
                        cut = j
                        break
                hard.append((cur, cut))
                cur = cut
            if cur < ce:
                hard.append((cur, ce))
        pieces = hard
    # 转 plain 区间
    res = []
    for cs, ce in pieces:
        pa = next((plain_of[i] for i in range(cs, ce) if plain_of[i] is not None), None)
        pb = next((plain_of[i] for i in reversed(range(cs, ce)) if plain_of[i] is not None), None)
        sent = text[cs:ce].strip()
        if pa is not None and pb is not None and sent:
            res.append((pa, pb + 1, sent))
    return res


def enforce_reading_speed(subs, min_dur_ms=700, max_cps=12.0):
    """按阅读速度补足过短的字幕显示时间，只延长不改文案（原地修改并返回）。

    CPS（每秒字数）是字幕可读性硬指标：中文常规上限 9-12 字/秒。对齐给的是真实
    发声时长，语速快的人 20 字可能只占 1.2 秒，直接出 SRT 就是闪一下。这里把 end
    拉到 max(最小时长, 字数/max_cps)，但不越过下一条字幕起点——越过就会时间轴交叉，
    播放器表现是两条同时挂在屏幕上。max_cps<=0 时只做最小时长兜底。
    """
    for i, s in enumerate(subs):
        start = int(s.get("start_ms", 0))
        n = sum(1 for c in (s.get("text") or "") if c not in PUNCT)
        need = int(min_dur_ms)
        if max_cps > 0 and n:
            need = max(need, int(n / float(max_cps) * 1000))
        end = max(int(s.get("end_ms", 0)), start + need)
        if i + 1 < len(subs):
            end = min(end, int(subs[i + 1].get("start_ms", end)) - 1)
        s["end_ms"] = max(end, start + 100, int(s.get("end_ms", 0)))
    return subs


def subtitle_units(units, aligns, max_len, min_dur=700, max_cps=12.0):
    """把输出单元细分为字幕条;units=[{start_ms,end_ms,text,spk,spk_name}],aligns 可为 None
    有 aligns 时按字级时间戳细分并校正边界,无则整段一条"""
    subs = []
    for idx, u in enumerate(units):
        al = aligns[idx] if aligns else None
        if not al or not al.get("ts"):
            subs.append({"start_ms": u["start_ms"], "end_ms": u["end_ms"], "text": u["text"],
                         "spk": u.get("spk"), "spk_name": u.get("spk_name")})
            continue
        ct = char_times(u["text"], al["tokens"], al["ts"], u["start_ms"], u["start_ms"], u["end_ms"])
        for a, b, sent in split_sentences(u["text"], max_len):
            if not ct:
                seg_s, seg_e = u["start_ms"], u["end_ms"]
            else:
                seg_s = ct[min(a, len(ct) - 1)][0]
                seg_e = ct[min(max(b - 1, 0), len(ct) - 1)][1]
            subs.append({"start_ms": seg_s, "end_ms": seg_e, "text": sent,
                         "spk": u.get("spk"), "spk_name": u.get("spk_name")})
    subs = enforce_reading_speed(subs, min_dur, max_cps)
    # 相邻字幕时间收敛:上一条 end 不晚于下一条 start
    for i in range(len(subs) - 1):
        if subs[i]["end_ms"] > subs[i + 1]["start_ms"]:
            subs[i]["end_ms"] = subs[i + 1]["start_ms"]
        if subs[i]["end_ms"] <= subs[i]["start_ms"]:
            subs[i]["end_ms"] = subs[i]["start_ms"] + 200
    return subs


def fmt_srt_ts(ms):
    ms = max(0, int(ms))
    return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"


def fmt_vtt_ts(ms):
    """WebVTT 时间戳：与 SRT 同布局，但毫秒用点号而非逗号（用逗号浏览器不解析）"""
    return fmt_srt_ts(ms).replace(",", ".")


def _subtitle_cues(subs, use_speaker):
    """把字幕单元整理成 [(text, start_ms, end_ms)]，SRT 与 VTT 共用，避免两套格式走偏"""
    cues = []
    for s in subs:
        label = ""
        if use_speaker and (s.get("spk_name") or s.get("spk") is not None):
            label = f"[{s.get('spk_name') or ('说话人' + str(s['spk']))}] "
        cues.append((f"{label}{s['text']}", s["start_ms"], s["end_ms"]))
    return cues


def write_srt(subs, path, use_speaker):
    blocks = []
    for i, (text, start, end) in enumerate(_subtitle_cues(subs, use_speaker), 1):
        blocks.append(f"{i}\n{fmt_srt_ts(start)} --> {fmt_srt_ts(end)}\n{text}\n")
    path.write_text("\n".join(blocks), encoding="utf-8")


def write_vtt(subs, path, use_speaker):
    """WebVTT：HTML5 <track> 与各家视频平台直接吃，SRT 不行。
    首行必须是 WEBVTT，其后空行再进 cue；cue 可不带序号，带了也不影响兼容性。"""
    lines = ["WEBVTT", ""]
    for i, (text, start, end) in enumerate(_subtitle_cues(subs, use_speaker), 1):
        lines.append(str(i))
        lines.append(f"{fmt_vtt_ts(start)} --> {fmt_vtt_ts(end)}")
        lines.append(text)
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_subtitles(subs, args, out_srt, use_speaker):
    """按 --srt / --vtt 写字幕文件，返回实际写出的路径列表。
    两种格式共用同一份 cue 数据与一次对齐成本，同时开就同时出，不重复跑对齐。"""
    written = []
    if args.srt:
        write_srt(subs, out_srt, use_speaker)
        written.append(out_srt)
    if getattr(args, "vtt", False):
        out_vtt = out_srt.with_suffix(".vtt")
        write_vtt(subs, out_vtt, use_speaker)
        written.append(out_vtt)
    return written


def write_rttm(lines, path, uri=None):
    """RTTM：说话人分离的标准交换格式（dscore / pyannote.metrics / meeteval 都直接读）。

    为什么值得单独出：本项目自己的 DER 走 pyannote.metrics，但外部复核、与论文数字
    对齐、或把分离结果交给别的工具链时，标准格式是唯一不用再写一次转换的通道，
    而它的成本只是几行文本。字段含义：
      SPEAKER <文件标识> 1 <起点秒> <时长秒> <NA> <NA> <说话人> <NA> <NA>
    说话人标签不能含空白（RTTM 以空白分列），人名里的空格换成下划线。
    """
    uri = uri or path.stem
    rows = []
    for l in lines or []:
        if l.get("spk") is None:
            continue
        start = max(0.0, int(l.get("start_ms", 0)) / 1000.0)
        end = max(start, int(l.get("end_ms", 0)) / 1000.0)
        if end - start <= 0:
            continue
        label = l.get("spk_name") or f"spk{l['spk']}"
        label = re.sub(r"\s+", "_", str(label)) or f"spk{l['spk']}"
        rows.append(f"SPEAKER {uri} 1 {start:.3f} {end - start:.3f} <NA> <NA> {label} <NA> <NA>")
    path.write_text("".join(r + "\n" for r in rows), encoding="utf-8")
    return len(rows)


def merge_short_segments(segments, min_ms, gap_ms):
    """过短 VAD 段（<min_ms）并入前一段（间隔 <= gap_ms）；首段过短则并入后段。
    目的：碎段的声纹嵌入不稳定，会被聚类误判成假说话人簇，先并入邻近段再提声纹。"""
    if min_ms <= 0:
        return [(int(s), int(e)) for s, e in segments]
    out = []
    for s, e in segments:
        s, e = int(s), int(e)
        if out and (e - s) < min_ms and s - out[-1][1] <= gap_ms:
            out[-1][1] = e  # 并入前段尾部
        else:
            out.append([s, e])
    if len(out) >= 2 and (out[0][1] - out[0][0]) < min_ms and out[1][0] - out[0][1] <= gap_ms:
        out[1][0] = out[0][0]  # 首段过短 -> 并入次段头部
        out.pop(0)
    return [(a, b) for a, b in out]


def split_long_segments(segments, max_ms, audio=None, sr=16000, search_ms=1500):
    """超长段按 max_ms 切分，切点吸附到附近能量最低处（静音点）。

    目的：单次超长自回归转写效率崩塌（实测 7 分钟段 GPU 满负荷 20 分钟未完成），
    拆成 <=max_ms 的多段走批量转写（实测 33 段全量约 3 分钟）。

    为什么不在理想切点硬切：pyannote 的回合常是连续语流，等分点大概率落在词中间，
    切出来的两段各自带半截词，转写文本在接缝处出现残字或重复。这里在每个理想切点
    前后 search_ms 范围内找 50ms 窗的最低能量位置，把切点移到声门闭合处。
    audio 为 None 时退化为等分（保持纯函数可测）。"""
    if max_ms <= 0:
        return [(int(s), int(e)) for s, e in segments]

    def quiet_point(lo_ms, hi_ms, hard_lo, hard_hi):
        """在 [lo,hi] 毫秒区间找最低能量点，返回切点毫秒；无音频或区间过窄返回 None"""
        if audio is None or hi_ms - lo_ms < 100:
            return None
        lo_ms = max(int(lo_ms), int(hard_lo))
        hi_ms = min(int(hi_ms), int(hard_hi))
        win = max(1, int(0.05 * sr))
        f0, f1 = int(lo_ms * sr / 1000), int(hi_ms * sr / 1000)
        f0, f1 = max(0, f0), min(len(audio), f1)
        n_frames = (f1 - f0) // win
        if n_frames < 2:
            return None
        frames = audio[f0:f0 + n_frames * win].reshape(n_frames, win)
        energy = (frames.astype(np.float64) ** 2).mean(axis=1)
        k = int(np.argmin(energy))
        # 采样索引换算回毫秒——返回索引会被上游当作毫秒用，切点直接飞出音频末尾
        return int(round((f0 + (k + 0.5) * win) * 1000 / sr))

    out = []
    for s, e in segments:
        s, e = int(s), int(e)
        n = max(1, -(-(e - s) // max_ms))  # 向上取整
        step = (e - s) / n
        cuts = [s]
        for k in range(1, n):
            ideal = s + int(round(k * step))
            # 搜索窗不越过相邻理想切点的一半，避免两次吸附挤到一起产生零长段
            half = min(search_ms, step / 2 * 0.9)
            q = quiet_point(ideal - half, ideal + half, s + 100, e - 100)
            cuts.append(q if q is not None else ideal)
        cuts.append(e)
        for a, b in zip(cuts, cuts[1:]):
            if b > a:
                out.append((int(a), int(b)))
    return out


def aed_batch_plan(paths, batch_size, budget_s):
    """AED 批量计划: 顺序分组,批内段数 <= batch_size 且总时长 <= budget_s。
    单段自身超预算时独占一批。显存峰值随批内总音频时长增长,固定 8 段一批时
    60s 段×8 是注意力显存峰值主因,故按双上限分批。"""
    import soundfile as sf
    batches, cur, cur_s = [], [], 0.0
    for p in paths:
        d = float(sf.info(str(p)).duration)
        if cur and (len(cur) >= batch_size or cur_s + d > budget_s):
            batches.append(cur)
            cur, cur_s = [], 0.0
        cur.append(p)
        cur_s += d
    if cur:
        batches.append(cur)
    return batches


def split_wav_at_quiet(wav_path, work_dir, depth):
    """在 25%-75% 时长区间找 50ms 能量最低点把 wav 二分。VAD 段是连续语音,
    最静点是可用的最优启发(可能切在语流中间,仅在 OOM 重试时触发)。
    返回 (前段路径, 后段路径, 切点ms)"""
    import soundfile as sf
    y, sr = sf.read(str(wav_path))
    win = max(1, int(0.05 * sr))
    n_frames = len(y) // win
    if n_frames < 4:
        mid = len(y) // 2
    else:
        energy = (y[:n_frames * win].reshape(n_frames, win) ** 2).mean(axis=1)
        lo, hi = n_frames // 4, max(n_frames // 4 + 1, (n_frames * 3) // 4)
        k = lo + int(np.argmin(energy[lo:hi]))
        mid = int((k + 0.5) * win)
    stem = Path(str(wav_path)).stem
    pa = work_dir / f"{stem}_d{depth}a.wav"
    pb = work_dir / f"{stem}_d{depth}b.wav"
    sf.write(str(pa), y[:mid], sr)
    sf.write(str(pb), y[mid:], sr)
    return str(pa), str(pb), mid / sr * 1000.0


def _ascii_boundary(ch):
    return ch.isascii() and (ch.isalnum() or ch in ".,!?;")


def merge_aed_results(ra, rb, offset_s, uttid):
    """二分重试结果合并: 文本拼接(英文词界补空格) + 后段字级时间戳平移 + 置信度取小。
    返回单元素列表,字段形状与 model.transcribe 输出一致"""
    a, b = ra[0], rb[0]
    ta, tb = (a.get("text") or ""), (b.get("text") or "")
    sep = " " if (ta and tb and (_ascii_boundary(ta[-1]) or _ascii_boundary(tb[0]))) else ""
    ts_a = a.get("timestamp") or []
    ts_b = [[c, round(s + offset_s, 3), round(e + offset_s, 3)]
            for c, s, e in (b.get("timestamp") or [])]
    confs = [r["confidence"] for r in (a, b) if r.get("confidence") is not None]
    out = {"uttid": uttid, "text": (ta + sep + tb).strip()}
    if confs:
        out["confidence"] = round(min(confs), 3)
    durs = [r["dur_s"] for r in (a, b) if r.get("dur_s") is not None]
    if durs:
        out["dur_s"] = round(sum(durs), 3)
    if ts_a or ts_b:
        out["timestamp"] = ts_a + ts_b
    return [out]


def aed_transcribe_oom_safe(model, audio_paths, ctx, depth=0):
    """AED 转写的显存兜底。FireRed transcribe 内部吞掉一切异常(fireredasr2/asr.py
    的 try/except),OOM 的表现是整批返回空文本而非抛错,故以"空结果"为重试信号:
    批 -> 拆成单段重试;单段仍空 -> 静音点二分递归,结果按切点偏移合并。
    深度与最短段长双限制;到限仍空则原样返回(真静音/纯噪声场景)。"""
    import soundfile as sf
    import torch
    results = model.transcribe([f"u{i}" for i in range(len(audio_paths))], list(audio_paths))
    if results and any((r.get("text") or "").strip() for r in results):
        return results
    if not audio_paths or depth >= ctx["max_depth"]:
        return results
    durs = [float(sf.info(str(p)).duration) for p in audio_paths]
    if len(audio_paths) == 1 and durs[0] < ctx["min_split_s"]:
        return results
    torch.cuda.empty_cache()
    ctx["log"](f"[warn] AED 批次({len(audio_paths)} 段)返回空结果,疑似显存不足,拆小重试(深度 {depth})")
    if len(audio_paths) > 1:
        out = []
        for i, p in enumerate(audio_paths):
            out.extend(aed_transcribe_oom_safe(model, [p], ctx, depth))
        return out
    a_path, b_path, split_ms = split_wav_at_quiet(audio_paths[0], ctx["work_dir"], depth)
    ra = aed_transcribe_oom_safe(model, [a_path], ctx, depth + 1)
    rb = aed_transcribe_oom_safe(model, [b_path], ctx, depth + 1)
    if len(ra) == 1 and len(rb) == 1:
        return merge_aed_results(ra, rb, split_ms / 1000.0, "u0")
    return ra + rb


def absorb_tiny_clusters(embeddings, labels, segments, max_cluster_ms=5000, min_share=0.02):
    """把总时长过短的小簇并入声纹最相似的大簇（消除碎段形成的假说话人）。
    背景：数百毫秒级碎段的声纹嵌入不稳定，聚类时易自成簇（实测 23 分钟单人口播
    被切成 15 个假说话人，其中 13 个是 1-3 秒碎块）。这类簇无法承载真实发言人信息，
    并入后说话人数回归真实。返回重编号后的 labels（numpy 数组）。

    判据要求「绝对时长够 且 占比够」——只看绝对时长会在长素材上失效：
    90 分钟云端实测聚出 18 个簇（实际 5 人），12 个假簇总时长都在 9-40 秒、
    全部超过 5 秒阈值，一个都吸收不掉。真实发言人的占比通常远超 2%，
    低于此的基本是同人被拆或噪音段；两个条件都满足才算真实发言人。"""
    import numpy as np
    labels = np.asarray(labels)
    clusters = sorted(set(labels.tolist()))
    if len(clusters) < 2:
        return labels
    dur = {c: sum(int(e) - int(s) for (s, e), l in zip(segments, labels) if l == c)
           for c in clusters}
    total_ms = sum(dur.values()) or 1
    big = [c for c in clusters
           if dur[c] > max_cluster_ms and dur[c] / total_ms >= min_share]
    if not big or len(big) == len(clusters):
        return labels
    centers = {c: embeddings[labels == c].mean(axis=0) for c in big}
    for c in clusters:
        if c in big:
            continue
        centroid = embeddings[labels == c].mean(axis=0)
        best, best_sim = None, -2.0
        for bc, center in centers.items():
            denom = float(np.linalg.norm(centroid) * np.linalg.norm(center)) + 1e-9
            sim = float(centroid @ center) / denom
            if sim > best_sim:
                best, best_sim = bc, sim
        labels[labels == c] = best
    remap, out = {}, []
    for l in labels.tolist():
        if l not in remap:
            remap[l] = len(remap)
        out.append(remap[l])
    return np.array(out)


def _merge_spans(spans, gap_ms=0):
    """合并重叠或相邻的时间区间，用于收敛跨块重复的重叠区标记"""
    if not spans:
        return []
    spans = sorted(spans)
    out = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= out[-1][1] + gap_ms:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(a, b) for a, b in out]


def extract_segment_embeddings(audio, segments, device, min_ms=300):
    """按段提 cam++ 声纹向量并 L2 归一化，返回 (embeddings, valid_idx)。

    pyannote 路径原本完全拿不到声纹，导致 --speaker-db 只能整条跳过。pyannote 的
    分割模型本身不吐 embedding，但段边界已经给好了，拿现成的段单独跑一遍 cam++
    就能补上——声纹库、跨文件身份复用这些能力因此与分离引擎解耦。

    过短段（<min_ms）不送声纹：几百毫秒的碎段嵌入方差极大，混进质心会污染匹配。
    这类段在返回值里没有对应行，由调用方按标签就近归类。"""
    from funasr import AutoModel
    spk = AutoModel(model="cam++", device=device, disable_update=True)

    def to_numpy(x):
        return x.cpu().numpy() if hasattr(x, "cpu") else np.asarray(x)

    embs, valid = [], []
    for i, (s_ms, e_ms) in enumerate(segments):
        if e_ms - s_ms < min_ms:
            continue
        seg = audio[int(s_ms) * 16: int(e_ms) * 16]
        if seg.size < 1600:
            continue
        emb = to_numpy(spk.generate(input=seg, fs=16000)[0]["spk_embedding"]).flatten()
        n = float(np.linalg.norm(emb))
        if n <= 0:
            continue
        embs.append(emb / n)
        valid.append(i)
    del spk
    gc_cuda()
    if not embs:
        return np.zeros((0, 512), dtype=np.float32), []
    return np.array(embs), valid


def centroid_by_label(embeddings, row_labels):
    """按标签聚合质心（成员向量取均值再 L2 归一化）。
    embeddings 每行一个向量，row_labels 与行一一对应；返回 {标签: 归一化质心}"""
    groups = {}
    for row, lab in enumerate(row_labels):
        if lab is None or lab < 0:
            continue
        groups.setdefault(int(lab), []).append(embeddings[row])
    out = {}
    for lab, vecs in groups.items():
        cen = np.mean(np.array(vecs), axis=0)
        n = float(np.linalg.norm(cen))
        if n > 0:
            out[lab] = cen / n
    return out


def map_labels_by_embedding(local_cents, global_cents, threshold=None):
    """按声纹相似度把本块的局部标签映射到全局标签（贪心一对一）。

    local_cents  = {局部标签: 归一化向量}
    global_cents = {全局标签: 归一化向量}
    返回 {局部标签: 全局标签}；相似度低于阈值的不映射（调用方会新建全局标签）。

    贪心顺序按相似度从高到低，一个全局标签只能被一个局部标签认领——否则两个局部标签
    可能同时抢同一个全局标签，把两个不同的人并成一个。
    """
    thr = PYANNOTE_EMB_THRESHOLD if threshold is None else threshold
    cand = []
    for spk, lc in local_cents.items():
        for g, gc in global_cents.items():
            cand.append((float(np.dot(lc, gc)), spk, g))
    out, taken = {}, set()
    for sim, spk, g in sorted(cand, key=lambda x: -x[0]):
        if sim < thr:
            continue
        if spk in out or g in taken:
            continue
        out[spk] = g
        taken.add(g)
    return out


def map_labels_by_time(raw, prev, win_lo, win_hi, min_overlap_ms=200):
    """时间重合投票映射（声纹不可用时的兜底）。

    按局部标签与每个全局标签在重叠窗口内的累计重合毫秒数取最大，贪心一对一。
    实测缺陷：块边界正好切在换人处时，窗口内两人都出现过，累计毫秒接近就投错边
    （chunk=15s 时两人被合成一个标签），因此只在声纹路径不可用时才用。
    """
    score = {}
    for s2, e2, spk2 in raw:
        if e2 <= win_lo or s2 >= win_hi:
            continue
        for s1, e1, g1 in prev:
            ov = min(e1, e2, win_hi) - max(s1, s2, win_lo)
            if ov > 0:
                score[(spk2, g1)] = score.get((spk2, g1), 0) + ov
    out, taken = {}, set()
    for (spk2, g1), ov in sorted(score.items(), key=lambda kv: -kv[1]):
        if spk2 in out or g1 in taken or ov < min_overlap_ms:
            continue      # 重合不足 min_overlap_ms 认定不足以判定同一人，宁可新建
        out[spk2] = g1
        taken.add(g1)
    return out


def _global_label_centroids(global_vecs, active_gids):
    """把已累积的全局标签声纹池聚成质心（均值再 L2 归一化），只取重叠窗口涉及的标签。
    global_vecs = {全局标签: [向量, ...]}；返回 {全局标签: 归一化质心}"""
    out = {}
    for g in active_gids:
        vecs = global_vecs.get(g)
        if not vecs:
            continue
        cen = np.mean(np.array(vecs), axis=0)
        n = float(np.linalg.norm(cen))
        if n > 0:
            out[g] = cen / n
    return out


def _pyannote_embed_segments(embed_infer, waveform, sr, segments, min_ms=500):
    """用 pyannote 自带的 WeSpeaker embedding 模型给每段提声纹向量（L2 归一化）。

    为什么不复用 extract_segment_embeddings：那支走 funasr cam++，模型与 pyannote 分割
    不同源，跨块匹配时声纹空间不一致；且额外加载一份模型要多占显存。pyannote community-1
    的 pipeline 本就带着 embedding 模型（WeSpeaker ResNet34，256 维），与分割同源，
    实测同人相似度 0.72–0.77、异人 0.11–0.17，0.5 阈值能干净切开。

    segments 为 [(start_ms, end_ms, ...), ...]，时间单位是**毫秒**（与 run_pyannote 的
    raw 一致）。返回 {段索引: 归一化向量}；过短段（<min_ms）不成向量，由调用方按最近段归类。
    """
    out = {}
    if embed_infer is None or not len(segments):
        return out
    import torch
    for i, seg in enumerate(segments):
        s_ms, e_ms = int(seg[0]), int(seg[1])
        if e_ms - s_ms < min_ms:
            continue
        chunk = waveform[int(s_ms * sr / 1000): int(e_ms * sr / 1000)]
        if chunk.size < int(sr * min_ms / 1000):
            continue
        wav = torch.from_numpy(np.asarray(chunk, dtype=np.float32)[None, :])
        try:
            vec = np.asarray(embed_infer({"waveform": wav, "sample_rate": sr})).flatten()
        except Exception:
            continue          # 单段失败不该整条分离失败，缺这段靠后续时间投票兜底
        n = float(np.linalg.norm(vec))
        if n > 0:
            out[i] = vec / n
    return out


def run_pyannote(src, device, num_speakers=None, min_speakers=None, max_speakers=None,
                 chunk_s=None, overlap_s=None):
    """pyannote 说话人分离（community-1 全套本地模型）。

    返回 (turns, overlaps)：
      turns    = [(start_ms, end_ms, spk_label), ...] 按时间排序的单标签回合
      overlaps = [(start_ms, end_ms), ...] 两人及以上同时发言的区间

    优于 cam++ 聚类之处：分割模型原生处理重叠语音（会议抢话场景），无需后验声纹聚类。
    用 soundfile 预载波形传入，绕开 torchcodec 的 DLL 依赖。

    说话人数（num/min/max_speakers）显式下发给 pipeline：社区版说话人计数本身不保证准确，
    已知会议人数时这是最便宜的一项精度提升。

    长音频分块：整段波形驻留内存随时长线性增长（16k 单声道 float32 约 230MB/小时，
    torch.from_numpy 再复制一份翻倍）。分块后峰值只与块长相关。块间留 overlap_s 重叠，
    跨块用声纹相似度把各块的局部标签并到全局标签——同一人跨块会各自得到独立标签，
    不映射就会凭空多出说话人。分块时不传 num_speakers：单块内人数可能少于全局，
    硬约束会逼模型把不同人合并或把同一人拆开。

    跨块映射用声纹而非时间重合投票：后者在块边界正好切在换人处时会失效——重叠窗口内
    两个说话人都出现过，累计毫秒数接近就投错边（实测 chunk=15s 时两人被合成一个标签）。
    声纹按段独立判定，与切点位置无关。声纹不可用（过短段/模型缺失）时才退回时间投票。
    """
    if not (PYANNOTE_DIR / "config.yaml").is_file():
        sys.exit(f"pyannote community-1 模型不存在: {PYANNOTE_DIR}\n"
                 f"下载: python -c \"from modelscope import snapshot_download; "
                 f"snapshot_download('pyannote/speaker-diarization-community-1')\"")
    import soundfile as sf
    import torch
    from pyannote.audio import Pipeline

    chunk_s = PYANNOTE_CHUNK_S if chunk_s is None else chunk_s
    overlap_s = PYANNOTE_OVERLAP_S if overlap_s is None else overlap_s
    info = sf.info(str(src))
    sr = int(info.samplerate)
    total_s = info.frames / float(sr)

    # 块长下限：太短的块 pyannote 聚不出多人（实测 10s 内两人被合成单标签，
    # 且下发 num_speakers=2 也无效）。用户显式给了更小值时提示并按真实值跑，
    # 不擅自改——他可能就是要试小块的内存表现。
    if 0 < chunk_s < PYANNOTE_MIN_CHUNK_S:
        print(f"[warn] pyannote 块长 {chunk_s:.0f}s 低于建议下限 {PYANNOTE_MIN_CHUNK_S}s，"
              f"短块内多人可能被合并成单标签（内存换精度）", file=sys.stderr)

    # 重叠必须小于块长，否则步进为 0 或负数会陷入死循环
    if chunk_s > 0 and overlap_s >= chunk_s:
        print(f"[warn] pyannote 块间重叠 {overlap_s}s 不小于块长 {chunk_s}s，已按块长一半处理",
              file=sys.stderr)
        overlap_s = chunk_s / 2.0
    chunked = chunk_s > 0 and total_s > chunk_s

    pipe = Pipeline.from_pretrained(str(PYANNOTE_DIR))
    pipe.to(torch.device(device))

    # 分块才需要跨块声纹映射；整段不分块时省掉这次模型加载
    embed_infer = None
    if chunked:
        from pyannote.audio import Inference, Model
        try:
            _emb_model = Model.from_pretrained(str(PYANNOTE_DIR / "embedding"))
            embed_infer = Inference(_emb_model, window="whole", device=torch.device(device))
        except Exception as e:
            print(f"[warn] pyannote 声纹模型加载失败，跨块映射退回时间投票: {e}",
                  file=sys.stderr)

    def call_dia(waveform, whole_file):
        """调用 pipeline；whole_file 为真时才允许断言确切人数"""
        kw = {}
        if whole_file and num_speakers:
            kw["num_speakers"] = num_speakers
        else:
            if min_speakers:
                kw["min_speakers"] = min_speakers
            if max_speakers:
                kw["max_speakers"] = max_speakers
        if kw:
            try:
                return pipe({"waveform": waveform, "sample_rate": sr}, **kw)
            except (TypeError, ValueError) as e:
                print(f"[warn] pyannote 不接受说话人数参数（{type(e).__name__}），"
                      f"改用自动推断: {e}", file=sys.stderr)
        return pipe({"waveform": waveform, "sample_rate": sr})

    if chunked:
        spans, start = [], 0.0
        step = chunk_s - overlap_s
        while start < total_s:
            end = min(start + chunk_s, total_s)
            spans.append((start, end))
            if end >= total_s:
                break
            start += step
    else:
        spans = [(0.0, total_s)]

    global_turns, overlap_spans, next_gid = [], [], 0
    global_vecs = {}      # {全局标签: [声纹向量, ...]}，供后续块做声纹比对
    for idx, (cs, ce) in enumerate(spans):
        data, _ = sf.read(str(src), start=int(cs * sr),
                          frames=int((ce - cs) * sr), dtype="float32", always_2d=False)
        if data.size == 0:
            continue
        if data.ndim > 1:
            data = data.mean(axis=1)  # 下混单声道
        wav = torch.from_numpy(data[None, :]).float()
        dia = call_dia(wav, whole_file=not chunked)
        base_ms = int(round(cs * 1000))
        raw = [(base_ms + int(round(t.start * 1000)),
                base_ms + int(round(t.end * 1000)),
                str(spk))
               for t, _, spk in dia.speaker_diarization.itertracks(yield_label=True)]
        # get_overlap() 返回 Timeline（可直接迭代 Segment），不是可调 itersegments 的对象
        for seg in dia.speaker_diarization.get_overlap():
            overlap_spans.append((base_ms + int(round(seg.start * 1000)),
                                  base_ms + int(round(seg.end * 1000))))

        # 本块全部回合的声纹（块内相对时间；waveform 也是块内波形）。
        # 分块时才提；整段不分块不需要跨块比对。
        local_embs_all = {}
        if embed_infer is not None:
            local_segs = [(s - base_ms, e - base_ms) for s, e, _ in raw]
            local_embs_all = _pyannote_embed_segments(embed_infer, data, sr, local_segs)

        # 跨块标签映射：优先声纹，声纹不可用时退回时间重合投票。
        # 声纹：本块每个局部标签取"重叠窗口内各段向量"的质心，与全局标签质心比相似度，
        # 取最相似且过阈值的认领。与切点位置无关，块边界正好落在换人处也不受影响。
        # 时间投票：只作兜底——它按累计毫秒判归属，换人点附近容易投错边。
        mapping = {}
        if idx > 0 and raw:
            win_lo, win_hi = base_ms, base_ms + int(round(overlap_s * 1000))
            prev = [(s, e, g) for s, e, g in global_turns if e > win_lo and s < win_hi]
            mapped = False

            if local_embs_all and global_vecs:
                # 局部标签质心用**本块全部段**，不只重叠窗口内的：某人的发言若完全落在
                # 块的后段（不跨重叠区），窗口里根本没有它的样本，只用窗口样本就会
                # 认不出它、凭空新建一个全局标签（实测 chunk=20s 多出 G2）。
                # 声纹按段独立，窗口内外同属一人，全用更稳。
                win_local = {}
                for i, (s_ms, e_ms, spk) in enumerate(raw):
                    if i in local_embs_all:
                        win_local.setdefault(spk, []).append(local_embs_all[i])
                local_cents = {}
                for spk, vecs in win_local.items():
                    cen = np.mean(np.array(vecs), axis=0)
                    n = float(np.linalg.norm(cen))
                    if n > 0:
                        local_cents[spk] = cen / n
                if local_cents:
                    # 声纹比对看**全部**已确定的全局标签，不只重叠窗口内的：
                    # 窗口只用来挑样本段，若某人本块没在窗口里出现，它仍是候选——
                    # 跨块身份是全局的，限制在窗口里会让新人凭空新建（实测多出说话人）。
                    global_cents = _global_label_centroids(
                        global_vecs, set(global_vecs.keys()))
                    mapping = map_labels_by_embedding(local_cents, global_cents)
                    mapped = True

            if not mapped:
                mapping = map_labels_by_time(raw, prev, win_lo, win_hi)

        # 非首块丢弃完全落在重叠区内的回合：那段内容由前一块负责，否则会重复计入
        keep_from = base_ms + int(round(overlap_s * 1000)) if idx > 0 else 0
        for s_ms, e_ms, spk in raw:
            if e_ms <= keep_from:
                continue
            gid = mapping.get(spk)
            if gid is None:
                gid = f"G{next_gid}"
                next_gid += 1
                mapping[spk] = gid
            global_turns.append((s_ms, e_ms, gid))

        # 累积声纹：把本块各段的向量挂到它最终归属的全局标签上
        for i, (s_ms, e_ms, spk) in enumerate(raw):
            if i in local_embs_all and e_ms > keep_from:
                gid = mapping.get(spk)
                if gid is None:
                    continue
                global_vecs.setdefault(gid, []).append(local_embs_all[i])
        del dia, wav, data
        if chunked:
            print(f"[2] pyannote 分块 {idx + 1}/{len(spans)} 完成"
                  f"（{cs:.0f}–{ce:.0f}s，累计 {len(global_turns)} 回合）", file=sys.stderr)

    # 必须显式释放：pyannote 模型驻留显存会与后续 Qwen3-ASR 转写争抢，
    # 实测不释放时进程在转写阶段无堆栈硬崩溃（16GB 显存不够两者共存）
    del pipe
    torch.cuda.empty_cache()
    global_turns.sort(key=lambda x: (x[0], x[1]))
    return global_turns, _merge_spans(overlap_spans)



def merge_by_speaker(segments, labels, gap_ms, max_ms=60000):
    """相邻同说话人且间隔 <= gap_ms 的段合并；单块时长超过 max_ms 强制断块（保转写吞吐）
    返回 [{"start_ms","end_ms","spk","seg_idx"}]"""
    merged = []
    for i, (s, e) in enumerate(segments):
        s, e = int(s), int(e)
        spk = int(labels[i])
        if (merged and merged[-1]["spk"] == spk
                and s - merged[-1]["end_ms"] <= gap_ms
                and e - merged[-1]["start_ms"] <= max_ms):
            merged[-1]["end_ms"] = e
            merged[-1]["seg_idx"].append(i)
        else:
            merged.append({"start_ms": s, "end_ms": e, "spk": spk, "seg_idx": [i]})
    return merged


def write_unit_wavs(audio, units, tmp_dir):
    """按单元切临时 wav(边界显式校验)"""
    import soundfile as sf
    files = []
    for i, u in enumerate(units):
        p = tmp_dir / f"unit_{i:04d}.wav"
        if p.parent != tmp_dir:
            raise RuntimeError("临时分段路径越出临时目录,已终止")
        sf.write(str(p), audio[u["start_ms"] * 16: u["end_ms"] * 16], 16000)
        files.append(str(p))
    return files


def to_wav16k(audio_path, tmp_dir):
    """转 16k 单声道 wav（AED 引擎只吃 wav；Qwen 引擎也用它分段后的统一样式）"""
    import librosa, soundfile as sf
    y, _ = librosa.load(str(audio_path), sr=16000, mono=True)
    p = tmp_dir / "input16k.wav"
    if p.parent != tmp_dir:
        raise RuntimeError("临时路径越出临时目录,已终止")
    sf.write(str(p), y, 16000)
    return str(p), y


def maybe_denoise(audio_path, enabled, log, tmp_dir=None):
    """ZipEnhancer 前处理(可选);返回新路径字符串。注意:实测对白噪声未见收益,含 BGM/真实嘈杂再试"""
    if not enabled:
        return str(audio_path)
    from modelscope.pipelines import pipeline
    from modelscope.utils.constant import Tasks
    t0 = time.time()
    ans = pipeline(Tasks.acoustic_noise_suppression, model="iic/speech_zipenhancer_ans_multiloss_16k_base")
    base = Path(tmp_dir) if tmp_dir else Path(tempfile.mkdtemp(prefix="qwen_asr_denoise_")).resolve()
    out = base / "enhanced.wav"
    ans(str(audio_path), output_path=str(out))
    log(f"[denoise] ZipEnhancer 前处理完成 ({time.time()-t0:.0f}s) -> {out}")
    return str(out)


def ensure_libsndfile_readable(src: str, log=print, tmp_dir=None) -> str:
    """libsndfile（soundfile/librosa 的底层）不解 m4a/AAC 等容器，直喂报 Format not
    recognised（2026-09-23 实测）。可读则原样返回；读不了用 ffmpeg 解码为 16k mono
    wav 临时文件并返回其路径（ffmpeg 缺失或解码失败返回空串，由调用方终止）。
    tmp_dir 给定时产物落其中（随 work_dir 的 atexit 一起清理），否则落系统 TEMP。"""
    import soundfile as sf
    try:
        sf.info(src)
        return src
    except Exception:
        pass
    import subprocess
    p = Path(src)
    base = Path(tmp_dir) if tmp_dir else Path(tempfile.gettempdir())
    dst = base / f"qwen_asr_{p.stem[:60]}_{os.getpid()}_16k.wav"
    log(f"[0] {p.suffix or '(无后缀)'} 容器 libsndfile 读不了，先用 ffmpeg 转 16k mono wav")
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src, "-ac", "1",
                        "-ar", "16000", str(dst)], capture_output=True)
    if r.returncode != 0 or not dst.is_file():
        log("ffmpeg 解码失败: " + r.stderr.decode("utf-8", "replace")[-400:])
        return ""
    return str(dst)


def merge_replace_dict(path: str, items: dict) -> int:
    """把纠错条目合并写回词典文件：键相同以新值覆盖，# 注释与未知行原样保留，
    新增键追加到文件尾部。返回本次合并的条目数。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    pending = dict(items)
    out_lines = []
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if "=>" in s and not s.startswith("#"):
                k, _, _v = s.partition("=>")
                k = k.strip()
                if k in pending:
                    out_lines.append(f"{k}=>{pending.pop(k)}")
                    continue
            out_lines.append(line)
    for k, v in pending.items():
        out_lines.append(f"{k}=>{v}")
    p.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return len(items)


def run_align_only(args, src, log, t0, audio_path, out_json, out_srt):
    """只做字幕对齐：读已有转写 JSON 的 lines，跑对齐，补写 sentences 并产出 .srt。

    不加载识别模型，只加载对齐模型——补字幕的成本从"整套重转"降到"一遍对齐"。
    已有 lines 的 start_ms/end_ms 就是各发言块的真实边界，直接沿用，只补充块内字级时间戳。
    """
    legacy = Path(os.path.realpath(args.align_only))
    if not legacy.is_file():
        sys.exit(f"--align-only 指定的转写结果不存在: {legacy}")
    payload = json.loads(legacy.read_text(encoding="utf-8"))
    lines = payload.get("lines") or []
    if not lines:
        sys.exit(f"--align-only 结果里没有 lines 字段，无法对齐: {legacy}")
    import numpy as np, soundfile as sf
    import librosa
    y, _ = librosa.load(str(src), sr=16000, mono=True)
    sr = 16000
    units = [{"start_ms": int(l["start_ms"]), "end_ms": int(l["end_ms"]),
              "text": l.get("text") or "", "spk": l.get("spk"),
              "spk_name": l.get("spk_name")} for l in lines]
    # 按段切 wav 供对齐器读取（对齐模型吃文件路径或 np 数组，用切好的临时文件最省内存）
    tmp_dir = Path(tempfile.mkdtemp(prefix="align_only_")).resolve()
    atexit.register(lambda d=tmp_dir: shutil.rmtree(d, ignore_errors=True))
    seg_files = write_unit_wavs(y, units, tmp_dir)

    want = args.align_engine
    if want == "auto":
        want = "native" if aligner_available() else "fa-zh"
    fa = None
    qwen_aligner = None
    if want == "native" and aligner_available():
        try:
            qwen_aligner = load_aligner(args.device)
            log(f"[1] Qwen3-ForcedAligner 已加载 ({qwen_aligner[2]:.0f}s)")
        except Exception as e:
            print(f"[warn] Qwen 对齐器加载失败，回落 fa-zh: {type(e).__name__}: "
                  f"{str(e)[:120]}", file=sys.stderr)
            want = "fa-zh"
    if qwen_aligner is None:
        from funasr import AutoModel
        fa = AutoModel(model="fa-zh", device=args.device, disable_update=True)
        log("[1] fa-zh 强制对齐模型已加载（中文专用）")

    texts = [u["text"] for u in units]
    if qwen_aligner is not None:
        aligns = align_batch_qwen(qwen_aligner[0], qwen_aligner[1], seg_files, texts,
                                  language=args.language)
    else:
        aligns = align_batch(fa, seg_files, texts)
    del fa, qwen_aligner
    gc_cuda()

    subs = subtitle_units(units, aligns, args.max_line, args.min_dur, args.max_cps)
    if not subs:
        sys.exit("对齐未产出可用字幕")
    payload["sentences"] = subs
    write_subtitles(subs, args, out_srt, use_speaker=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[2] 仅对齐完成：字幕 {out_srt.name}（{len(subs)} 条），结果已回写 {out_json.name}"
        f"（总耗时 {time.time()-t0:.0f}s）")
    return 0


def run_worker():
    """常驻 worker：stdin 逐行收 JSON 任务，同进程串行执行，模型只加载一次。

    管线批量处理时由父进程拉起一次，N 个素材省掉 N-1 次主引擎加载
    （Qwen3-ASR 单次 3.8GB 权重）。协议一行一个 JSON：
      请求 {"audio": "<路径>", "args": ["--diarize", ...]}
      应答 {"ok": bool, "error": str|null, "stdout": "<该任务的标准输出>"}
    单任务失败不回传致命错误、不中断循环：批处理里一份坏素材不该让其余全部作废。"""
    import contextlib
    import io
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            job = json.loads(line)
        except ValueError as e:
            print(json.dumps({"ok": False, "error": f"清单不是合法 JSON: {e}", "stdout": ""}),
                  ensure_ascii=False, flush=True)
            continue
        if job.get("op") == "shutdown":
            break
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                run_single([job.get("audio") or ""] + list(job.get("args") or []))
            resp = {"ok": True, "error": None, "stdout": buf.getvalue()}
        except SystemExit as e:
            # sys.exit 是本脚本的常规报错出口（参数错/模型缺失/无语音段），
            # 在 worker 里必须转成单项失败，否则第一份坏素材就终结整批
            resp = {"ok": False,
                    "error": str(e) if not isinstance(e.code, int) else f"退出码 {e.code}",
                    "stdout": buf.getvalue()}
        except Exception as e:
            resp = {"ok": False, "error": f"{type(e).__name__}: {e}", "stdout": buf.getvalue()}
        print(json.dumps(resp, ensure_ascii=False), flush=True)


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "--worker":
        return run_worker()
    run_single(argv)


def run_single(argv=None):
    ap = argparse.ArgumentParser(description="Qwen3-ASR 本地转写（可说话人分离/字幕/专名纠错）")
    ap.add_argument("audio", help="音频文件路径(mp3/m4a/wav/flac/ogg/webm)")
    ap.add_argument("--diarize", action="store_true", help="启用说话人分离(fsmn-vad + cam++ + 聚类);长音频不加此参也会自动分段提速")
    ap.add_argument("--language", default=None,
                    help="强制语言:代码(zh/en/yue/ja...)或全名(Chinese/English...),默认自动检测")
    ap.add_argument("--hotwords", default=None, help="热词/上下文,逗号分隔,作为 system prompt 偏置识别")
    ap.add_argument("--replace", default=None, help="专名纠错,如 '开饭时间=>开放时间,小蜜=>小米'（确定性替换）")
    ap.add_argument("--replace-file", nargs="?", const="auto", default=None,
                    help="纠错词典文件(每行 '错=>对' 或 目标词);裸写或 auto=用音频同目录 replace_dict.txt(无则空基座不报错)")
    ap.add_argument("--replace-save", nargs="?", const="auto", default=None,
                    help="把本次纠错条目合并写回词典:裸写或 auto=音频同目录 replace_dict.txt,也可给显式路径(键相同以新值为准)")
    ap.add_argument("--fuzzy", action="store_true", help="纠错启用拼音模糊匹配（需 pypinyin+rapidfuzz）")
    ap.add_argument("--srt", action="store_true", help="额外输出 .srt 字幕（加载 fa-zh 强制对齐出字级时间戳）")
    ap.add_argument("--vtt", action="store_true",
                    help="额外输出 .vtt 字幕（WebVTT，HTML5 <track> 与视频平台直接可用）；与 --srt 同源同成本")
    ap.add_argument("--max-line", type=int, default=28, help="单条字幕最大字数(默认28)")
    ap.add_argument("--min-dur", type=int, default=700,
                    help="字幕最短显示时长ms(默认700;语速快时避免一闪而过,0=不补)")
    ap.add_argument("--max-cps", type=float, default=12.0,
                    help="字幕最大阅读速度 字/秒(默认12;超过则按字数拉长显示时长,0=只按最小时长兜底)")
    ap.add_argument("--rttm", action="store_true",
                    help="额外输出 .rttm 说话人日志(RTTM 标准格式,pyannote.metrics/meeteval/dscore 可直接读)")
    ap.add_argument("--names", default=None, help="说话人重命名,如 '0=张三,1=李四'")
    ap.add_argument("--merge-gap", type=int, default=800, help="相邻同说话人合并间隔阈值ms(默认800,设0关闭)")
    ap.add_argument("--min-seg", type=int, default=400,
                    help="过短语音段并入相邻段的阈值ms(默认400,0=关闭;碎段声纹不稳易造假说话人)")
    ap.add_argument("--max-unit", type=int, default=60000,
                    help="发言块时长上限ms,超出强制切分(默认60000,0=关闭;超长块单次自回归转写极慢)")
    ap.add_argument("--vad", choices=["auto", "fsmn", "firered"], default="auto",
                    help="VAD 后端(默认 auto=装了 FireRedVAD 就用它:边界更准、耗时约为 fsmn 的 1/20;"
                         "未装回落 fsmn;fsmn/firered 为强制指定)")
    ap.add_argument("--diarize-engine", choices=["campp", "pyannote"], default="campp",
                    help="说话人分离引擎: campp=VAD+cam++声纹聚类(默认,轻量); "
                         "pyannote=community-1 分割模型(原生处理重叠语音,会议抢话场景更准; 需已装 pyannote.audio)")
    ap.add_argument("--engine", choices=["qwen", "aed", "auto", "cloud"], default="qwen",
                    help="识别引擎: qwen=Qwen3-ASR-1.7B(默认,多语言含日文); aed=FireRedASR2-AED(中/英/粤更准,不支持日文,输出经 ct-punc 补标点); "
                         "auto=按 --language 路由(中/英/粤走 aed,其余走 qwen;未指定语言时保守走 qwen)")
    ap.add_argument("--cloud-model", choices=["message", "omni", "filetrans", "auto"], default="auto",
                    help="cloud 引擎模型: auto=默认，有公网 URL(--cloud-url)走 filetrans、否则回落 message;"
                         " filetrans=qwen-audio-3.1-asr-flash-filetrans(异步整文件,仅公网 URL,"
                         "自带说话人分离与字级时间戳,跳过本地 VAD/模型,最快);"
                         " message=qwen-audio-3.1-asr-message(dashscope SDK,整文件直出"
                         "句级+字级时间戳;加 --diarize 时逐段并经本地声纹聚类);"
                         " omni=qwen3.8-omni-flash(OpenAI 兼容全模态转写,无时间戳)")
    ap.add_argument("--cloud-url", default=None,
                    help="filetrans 模式的音频公网 URL(仅此模式需要;本地文件用 3.1/3.0 即可)")
    ap.add_argument("--no-punc", action="store_true", help="aed 引擎不补标点(默认用 ct-punc 补)")
    ap.add_argument("--align-engine", choices=["auto", "fa-zh", "native"], default="auto",
                    help="字幕强制对齐后端: auto=有 Qwen3-ForcedAligner 就用它、否则回落 fa-zh(默认);"
                         "fa-zh=FunASR 中文专用对齐(38M,仅中文);"
                         "native=Qwen3-ForcedAligner-0.6B(多语言,需先下载约 1.2GB)")
    ap.add_argument("--num-speakers", type=int, default=None,
                    help="已知说话人数(pyannote 引擎):精确人数最准;仅在整段分析时生效,分块时忽略")
    ap.add_argument("--min-speakers", type=int, default=None,
                    help="说话人数下限(pyannote 引擎),与 --max-speakers 配合挡住过度切分")
    ap.add_argument("--max-speakers", type=int, default=None,
                    help="说话人数上限(pyannote 引擎),会议人数不确定时用它限制")
    ap.add_argument("--pyannote-chunk-s", type=int, default=None,
                    help=f"pyannote 单块时长上限秒(默认 {PYANNOTE_CHUNK_S},0=不分块);"
                         "长素材分块可显著降内存峰值")
    ap.add_argument("--pyannote-overlap-s", type=int, default=None,
                    help=f"pyannote 块间重叠秒(默认 {PYANNOTE_OVERLAP_S}),供跨块说话人标签映射")
    ap.add_argument("--denoise", action="store_true", help="先跑 ZipEnhancer 降噪(含 BGM/强噪时试;白噪声实测无明显收益)")
    ap.add_argument("--threshold", type=float, default=0.75,
                    help="声纹余弦聚类阈值(声音相近漏分调低0.65-0.7,同人被拆调高0.8)")
    ap.add_argument("--batch-size", type=int, default=8, help="转写批大小(AED 引擎另受批内总时长预算限制,默认每批最多 120s)")
    ap.add_argument("--max-new-tokens", type=int, default=2048, help="单段最大生成 token 数")
    ap.add_argument("--no-t2s", action="store_true", help="关闭默认的繁->简转换(保留模型原始输出)")
    ap.add_argument("--itn", action="store_true",
                    help="中文逆文本正则化:三百二十万元->320万元、百分之八十->80%%、二零二六年十月十五日->2026年10月15日")
    ap.add_argument("--itn-backend", choices=["auto", "wtp", "legacy"], default="auto",
                    help="ITN 后端: auto=装了 WeTextProcessing 就用、否则自写规则(默认); "
                         "wtp=强制 WeTextProcessing(覆盖更全,需 pynini,Windows 需 MSVC); legacy=自写零依赖规则")
    ap.add_argument("--emotion", action="store_true",
                    help="emotion2vec+large 整段情绪分析(8 类情绪,结果写入 json 的 emotions 字段);"
                         "注意它不做笑声/掌声这类音频事件检测")
    ap.add_argument("--speaker-db", nargs="?", const="auto", default=None,
                    help="声纹库 JSON 路径:跨文件复用说话人身份,命中已知声纹时直接显示库中姓名;"
                         "裸写或 auto=用音频同目录 speaker_db.json(有则复用无则自动建库、命中后更新)"
                         "(campp 与 pyannote 两条路径都可用:pyannote 按已定段边界补算声纹向量)")
    ap.add_argument("--speaker-db-save", action="store_true",
                    help="把本次识别到的说话人声纹写入 --speaker-db 指定的库(同名覆盖;auto 模式自动保存,无需此参)")
    ap.add_argument("--speaker-db-threshold", type=float, default=0.75,
                    help="声纹库命中阈值(余弦相似度,默认 0.75;同人被认成新人的话调低)")
    ap.add_argument("--device", default=None,
                    help="推理设备,默认自动选(cuda 可用则 cuda:0,否则 cpu);无 N 卡或装的是 CPU 版 torch 时自动走 cpu")
    ap.add_argument("--job-config", default=None,
                    help="任务配置 JSON 路径（供管线脚本透传参数；与命令行同名参数等价）")
    ap.add_argument("--align-only", default=None, metavar="JSON",
                    help="只做字幕对齐：读已有转写结果 JSON，产出 .srt 并补写 sentences 字段，"
                         "不重新跑识别。补字幕时可省掉最贵的转写环节")
    args = ap.parse_args(argv)

    # 任务配置合并：仅覆盖命令行未显式给出的项（命令行优先）
    if args.job_config:
        cfg_path = Path(os.path.realpath(args.job_config))
        if not cfg_path.is_file():
            sys.exit(f"任务配置不存在: {cfg_path}")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        defaults = {a.dest for a in ap._actions if a.dest != "job_config"}
        for k, v in cfg.items():
            if k not in defaults:
                sys.exit(f"任务配置含未知项: {k}")
            setattr(args, k, v)

    # 设备自动选择：--device 未显式给出时按 torch 实际能力定。装的是 CPU 版 torch 时
    # torch.cuda.is_available() 为 False，若照旧传 cuda:0，funasr/pyannote 会直接抛错
    # （实测：--engine cloud + --meeting 时识别在云端，但分离仍在本地，崩在设备选择上）
    if not args.device:
        args.device = "cpu"
        try:
            import torch
            if torch.cuda.is_available():
                args.device = "cuda:0"
        except ImportError:
            pass  # torch 未装=只能走纯云端通道（omni/filetrans）
    # --engine auto：按语言路由。中/英/粤在 AED 上实测更准、嘈杂素材更稳；其余语言
    # （尤其日文，AED 会输出中文乱码）留在 Qwen。未指定语言时无法预知，保守走 Qwen。
    if args.engine == "auto":
        lang = (args.language or "").strip().lower()
        if lang in ("zh", "en", "yue", "chinese", "english", "cantonese"):
            args.engine = "aed"
        else:
            args.engine = "qwen"
            if not lang:
                print("[提示] --engine auto 未指定 --language，保守使用 qwen；"
                      "若素材是中/英/粤，加 --language zh 可自动切到更准的 AED", file=sys.stderr)

    # --cloud-model auto：有公网 URL 走 filetrans（最快，云端一并做说话人分离与字级时间戳），
    # 没有就回落 message（本地文件直传，实测 90 分钟素材 170 秒，比本地 AED 快约 10 倍）。
    # filetrans 只收公网可访问 URL，本机没有天然出口，故 auto 是默认值而不是硬指 filetrans：
    # 这样配好 OSS/图床后无需改代码，自动走更快的通道。
    if args.cloud_model == "auto":
        if args.cloud_url:
            args.cloud_model = "filetrans"
            print("[提示] 检测到 --cloud-url，使用 filetrans（云端整文件，最快）", file=sys.stderr)
        else:
            args.cloud_model = "message"
            print("[提示] 未提供 --cloud-url，cloud 模型回落 message；"
                  "filetrans 需要公网可访问的音频 URL，用 --cloud-url 指定可自动切换",
                  file=sys.stderr)

    # --vad auto：就地解析成具体后端。解析必须发生在任何 run_vad 调用之前，
    # 且结果要能被上层当作缓存键的一部分（管线会显式下发解析后的名字，不依赖这里）。
    if args.vad == "auto":
        used = resolve_vad_backend("auto", for_diarization=bool(args.diarize))
        hint = ""
        if used == "fsmn" and vad_backend_available("firered"):
            hint = "（分离链路的段边界直接决定声纹聚类粒度，实测 fsmn 更稳；" \
                   "--no-diarize 长音频会自动改用 FireRedVAD）"
        elif used == "fsmn":
            hint = "（未装 FireRedVAD；可用 modelscope download --model xukaituo/FireRedVAD 安装）"
        if hint:
            print(f"[提示] VAD 后端自动选择 {used}{hint}", file=sys.stderr)
        args.vad = used

    # 输入路径校验:拒绝 ".."，realpath 规范化
    if ".." in args.audio:
        sys.exit("路径不允许包含 '..'")
    audio_path = Path(os.path.realpath(args.audio))
    if not audio_path.is_file():
        sys.exit(f"音频文件不存在: {audio_path}")
    # 输出路径锁定在音频同目录(派生后显式校验目录边界)
    out_json = audio_path.with_suffix(".json")
    out_srt = audio_path.with_suffix(".srt")
    out_vtt = audio_path.with_suffix(".vtt")
    for p in (out_json, out_srt, out_vtt):
        if p.parent != audio_path.parent:
            sys.exit("输出路径越出音频目录,已终止")
    hotwords = "、".join(w.strip() for w in args.hotwords.split(",") if w.strip()) if args.hotwords else None
    # auto 持久化文件用音频同目录（单用场景；管线场景由 separate 解析成显式路径透传）
    replace_file_arg = args.replace_file
    if args.replace_file == "auto":
        replace_file_arg = str(audio_path.parent / "replace_dict.txt")
    speaker_db_path = args.speaker_db
    speaker_db_save = args.speaker_db_save or args.speaker_db == "auto"
    if args.speaker_db == "auto":
        speaker_db_path = str(audio_path.parent / "speaker_db.json")
    replace_map = {}
    if replace_file_arg:
        p = Path(os.path.realpath(replace_file_arg))
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=>" in line:
                    k, v = line.split("=>", 1)
                    replace_map[k.strip()] = v.strip()
                else:
                    replace_map[line] = line
        elif args.replace_file != "auto":
            sys.exit(f"纠错词典文件不存在: {p}")
    if args.replace:
        for item in args.replace.replace("；", ";").replace(";", ",").split(","):
            if "=>" in item:
                k, v = item.split("=>", 1)
                if k.strip() and v.strip():
                    replace_map[k.strip()] = v.strip()
    names = {}
    if args.names:
        for item in args.names.replace("；", ";").replace(";", ",").split(","):
            if "=" in item:
                k, v = item.split("=", 1)
                if k.strip().isdigit() and v.strip():
                    names[int(k.strip())] = v.strip()

    t0 = time.time()
    def log(msg):
        print(msg, file=sys.stderr, flush=True)

    if args.replace_save and replace_map:
        save_path = (str(audio_path.parent / "replace_dict.txt")
                     if args.replace_save == "auto" else args.replace_save)
        merge_replace_dict(save_path, replace_map)
        log(f"[0] 纠错词典已合并写回: {save_path}")

    use_aed = args.engine == "aed"
    use_cloud = args.engine == "cloud"
    work_dir = Path(tempfile.mkdtemp(prefix="qwen_asr_")).resolve()
    # 临时目录随进程清理（覆盖正常结束/sys.exit/异常/中断等全部退出路径）；
    # 2026-10-11 检查发现此前从不清理，TEMP 实测累积 46 个残留
    atexit.register(lambda d=work_dir: shutil.rmtree(d, ignore_errors=True))
    src0 = ensure_libsndfile_readable(str(audio_path), log, tmp_dir=work_dir)
    if not src0:
        sys.exit("输入音频无法读取（ffmpeg 解码失败，见上方日志）")
    if args.denoise:
        src = maybe_denoise(src0, True, log, tmp_dir=work_dir)
    else:
        src = src0
    import librosa, soundfile as sf
    preloaded_audio = None

    # ---- 只补字幕模式：读已有转写结果，只跑对齐 ----
    # 补字幕之所以要独立通道，是因为对齐的成本远低于转写（38M/0.6B 对齐模型 vs 1.7B
    # 自回归识别）。把 srt 放进转写缓存键会让"加字幕"等于整套重转，白白烧掉最贵环节。
    if args.align_only:
        return run_align_only(args, src, log, t0, audio_path, out_json, out_srt)

    # AED 只吃 16k wav;为统一,两种引擎都先落到工作目录的 16k wav(顺便完成格式归一)
    if use_aed or use_cloud:
        src, preloaded_audio = to_wav16k(src, work_dir)
    if use_aed:
        model, processor, load_s = load_aed_model(args.device)
    elif use_cloud:
        model, processor = None, None   # 云端推理,不加载本地大模型
        load_s = time.time() - t0
    else:
        model, processor, load_s = load_model(args.device)
    t2s = build_t2s(args.no_t2s)
    matcher = build_matcher(replace_map, args.fuzzy)
    punc = load_punc(args.device) if (use_aed and not args.no_punc) else None
    itn_fn = None
    if args.itn:
        # itn_zh.py 与本脚本同目录；直接运行/子进程调用时脚本目录在 sys.path[0]
        try:
            from itn_zh import normalize as _itn_normalize, wtp_available
            backend = args.itn_backend
            if backend == "auto":
                backend = "wtp" if wtp_available() else "legacy"
                log(f"[0] ITN 后端: {'WeTextProcessing' if backend == 'wtp' else '自写规则'}"
                    + ("" if backend == "wtp" else "（装 WeTextProcessing 可获更全覆盖）"))
            itn_fn = lambda s, _b=backend: _itn_normalize(s, backend=_b)
        except Exception as e:
            print(f"[warn] ITN 模块加载失败，跳过逆文本正则化: {type(e).__name__}: {e}", file=sys.stderr)
    if use_cloud and args.cloud_model == "message" and not args.diarize:
        run_cloud_message(args, out_json, out_srt, matcher, t2s, itn_fn, log, t0, src)
        return
    if args.engine == "cloud" and args.cloud_model == "filetrans":
        run_cloud_filetrans(args, out_json, out_srt, matcher, t2s, itn_fn, names, log, t0, audio_path)
        return
    log(f"[1] {('CLOUD:' + args.cloud_model) if use_cloud else args.engine.upper()} 引擎已加载到 {args.device} ({load_s:.0f}s)"
        + ("" if t2s else "（繁->简已关闭）")
        + ("；ITN 已启用" if itn_fn is not None else "")
        + (f"；专名纠错 {len(replace_map)} 条" if replace_map else "")
        + ("；ct-punc 已加载" if punc is not None else ""))
    # ---- 字幕对齐后端：fa-zh（中文专用 38M）或 Qwen3-ForcedAligner（多语言）----
    # auto 默认优先 Qwen 对齐器：它的词表覆盖 11 种语言，fa-zh 只认中文，
    # 非中文素材用 fa-zh 会产出错位时间戳。模型没下时静默回落 fa-zh 而不是报错。
    use_fa = False
    fa = None
    qwen_aligner = None
    if args.srt and not (use_cloud and args.cloud_model == "message"):
        want = args.align_engine
        if want == "auto":
            want = "native" if aligner_available() else "fa-zh"
            if want == "fa-zh" and not args.language:
                pass  # 未指定语言也可能是中文，fa-zh 仍是可用兜底
        if want == "native":
            if aligner_available():
                try:
                    qwen_aligner = load_aligner(args.device)
                    log(f"[1b] Qwen3-ForcedAligner 已加载 ({qwen_aligner[2]:.0f}s，多语言对齐)")
                except Exception as e:
                    print(f"[warn] Qwen 对齐器加载失败，回落 fa-zh: {type(e).__name__}: "
                          f"{str(e)[:120]}", file=sys.stderr)
                    use_fa = True
            else:
                if args.align_engine == "native":
                    sys.exit(f"Qwen3-ForcedAligner 模型不存在: {ALIGNER_MS}\n"
                             f"下载: python -c \"from modelscope import snapshot_download; "
                             f"snapshot_download('Qwen/Qwen3-ForcedAligner-0.6B', "
                             f"local_dir=r'{ALIGNER_MS.parents[2]}')\"")
                use_fa = True
                log("[1b] 未找到 Qwen3-ForcedAligner，回落 fa-zh 中文对齐")
        else:
            use_fa = True
        if use_fa:
            from funasr import AutoModel
            fa = AutoModel(model="fa-zh", device=args.device, disable_update=True)
            log("[1b] fa-zh 强制对齐模型已加载（中文专用）")

    def align_units(wav_paths, texts):
        """统一对齐入口：按当前后端产出与 fa-zh 同构的 aligns，失败返回 None"""
        if qwen_aligner is not None:
            return align_batch_qwen(qwen_aligner[0], qwen_aligner[1], wav_paths, texts,
                                    language=args.language)
        if fa is not None:
            return align_batch(fa, wav_paths, texts)
        return None

    def infer(paths):
        """统一识别入口（按引擎分发,批量）"""
        out = []
        if use_cloud:
            cctx = {"key": load_dashscope_key(), "model": args.cloud_model,
                    "log": log, "hotwords": hotwords}
            out.extend(cloud_transcribe_batch(paths, cctx, t2s=t2s, matcher=matcher, itn=itn_fn))
            return out
        if use_aed:
            # 时长感知分批: 段数≤--batch-size 且批内总时长≤AED_BATCH_BUDGET_S;
            # 空结果(疑似 OOM)自动拆小重试
            aed_ctx = {"work_dir": work_dir, "log": log,
                       "min_split_s": AED_MIN_SPLIT_S, "max_depth": AED_MAX_SPLIT_DEPTH}
            for chunk in aed_batch_plan(paths, args.batch_size, AED_BATCH_BUDGET_S):
                out.extend(transcribe_batch(model, processor, chunk, args.language,
                                            hotwords, args.max_new_tokens, t2s=t2s, matcher=matcher,
                                            punc=punc, engine=args.engine, aed_ctx=aed_ctx,
                                            itn=itn_fn))
            return out
        bs = max(args.batch_size, 8)
        for i in range(0, len(paths), bs):
            out.extend(transcribe_batch(model, processor, paths[i:i + bs], args.language,
                                        hotwords, args.max_new_tokens, t2s=t2s, matcher=matcher,
                                        punc=punc, engine=args.engine, itn=itn_fn))
        return out

    _engine_tag = ("FireRedASR2-AED" if use_aed else
                   CLOUD_MODELS[args.cloud_model] if use_cloud else "Qwen3-ASR-1.7B")

    if not args.diarize:
        # ---- 整段转写;长音频(>60s)自动 VAD 分段批量(提速+每段语言独立检测),不标说话人 ----
        duration_s = sf.info(src).duration
        units, aligns = [], None
        if duration_s > AUTO_SEGMENT_S:
            segments = run_vad(src, args.vad, args.device)
            if not segments:
                sys.exit("未检测到语音(VAD 结果为空)")
            log(f"[2] 音频 {duration_s:.0f}s > {AUTO_SEGMENT_S}s, 自动 VAD 分段 {len(segments)} 段批量转写")
            audio = preloaded_audio if preloaded_audio is not None else librosa.load(src, sr=16000, mono=True)[0]
            units = [{"start_ms": int(s), "end_ms": int(e), "text": "", "spk": None, "seg_idx": [i]}
                     for i, (s, e) in enumerate(segments)]
            seg_files = write_unit_wavs(audio, units, work_dir)
            parsed = infer(seg_files)
            punc = None; gc_cuda()   # ct-punc 已用完,置 None 放开闭包 cell 引用(小模型不驻留到转写峰值后)
            for u, p in zip(units, parsed):
                u["text"] = p["transcription"]
                u["language"] = p.get("language")
            texts = [u["text"] for u in units]
            if use_aed and aed_ts_usable(parsed):
                aligns = aed_aligns_from(parsed)
                log(f"[2b] 使用 AED 原生字级时间戳（{len(parsed)} 段）")
            elif fa is not None or qwen_aligner is not None:
                aligns = align_units(seg_files, texts)
                if use_aed:
                    log("[2b] AED 时间戳不可用,已改用强制对齐")
            fa = None; qwen_aligner = None; gc_cuda()   # 对齐已完成
            print("\n".join(texts))
            langs = [p.get("language") for p in parsed if p.get("language")]
            main_lang = max(set(langs), key=langs.count) if langs else None
            payload = {"schema_version": SCHEMA_VERSION, "language": main_lang,
                       "text": "\n".join(texts), "engine": _engine_tag,
                       "segments": [{"start_ms": u["start_ms"], "end_ms": u["end_ms"], "language": u.get("language"),
                                     "text": u["text"]} for u in units]}
            if aligns is not None:
                payload["sentences"] = subtitle_units(units, aligns, args.max_line, args.min_dur, args.max_cps)
            out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            if aligns is not None:
                write_subtitles(payload["sentences"], args, out_srt, use_speaker=False)
            log(f"[3] 完成 (总耗时 {time.time()-t0:.0f}s), 结构化结果: {out_json}"
                + (f", 字幕: {out_srt}" if aligns is not None else ""))
            return
        res = infer([src])[0]
        punc = None; gc_cuda()   # 转写已完成,同上释放
        lang = res.get("language") or ("aed" if use_aed else "auto")
        print(f"[{lang}] {res['transcription']}")
        u = {"start_ms": 0, "end_ms": int(duration_s * 1000), "text": res["transcription"],
             "spk": None, "spk_name": None}
        payload = {"schema_version": SCHEMA_VERSION, "language": res.get("language"),
                   "text": res["transcription"],
                   "engine": _engine_tag}
        if use_aed and aed_ts_usable([res]):
            aligns = aed_aligns_from([res])
            log("[2b] 使用 AED 原生字级时间戳")
        elif fa is not None or qwen_aligner is not None:
            aligns = align_units([src], [res["transcription"]])
        else:
            aligns = None
        fa = None; qwen_aligner = None; gc_cuda()   # 对齐已完成
        if aligns:
            payload["sentences"] = subtitle_units([u], aligns, args.max_line, args.min_dur, args.max_cps)
            write_subtitles(payload["sentences"], args, out_srt, use_speaker=False)
        out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"[2] 完成 (总耗时 {time.time()-t0:.0f}s), 结构化结果: {out_json}"
            + (f", 字幕: {out_srt}" if payload.get("sentences") else ""))
        return

    # ---- 说话人分离模式:分环实现(与技能 diarize.py 同一逻辑,识别引擎换成 Qwen3-ASR) ----
    audio = preloaded_audio if preloaded_audio is not None else librosa.load(src, sr=16000, mono=True)[0]

    overlaps = []
    if args.diarize_engine == "pyannote":
        # pyannote 路径：分割模型原生给出「片段+说话人」，跳过 VAD/声纹聚类链
        turns, overlaps = run_pyannote(
            src, args.device,
            num_speakers=args.num_speakers,
            min_speakers=args.min_speakers,
            max_speakers=args.max_speakers,
            chunk_s=args.pyannote_chunk_s,
            overlap_s=args.pyannote_overlap_s)
        if not turns:
            sys.exit("pyannote 未检测到语音")
        spk_ids, segments, labels = {}, [], []
        for s, e, spk in turns:
            if spk not in spk_ids:
                spk_ids[spk] = len(spk_ids)
            # 超长片段切分保转写吞吐；切点吸附到静音点，避免把词切成两半
            for ps, pe in split_long_segments([(s, e)], args.max_unit, audio=audio):
                segments.append((ps, pe))
                labels.append(spk_ids[spk])
        n_before_merge = len(segments)
        # 碎段并入同说话人的前一段（pyannote 路径原先完全没做这步，min_seg 在这里无效）：
        # 90 分钟实测 1221 段里 654 段不足 1 秒，每段都要独立跑一次转写。
        # 只在「同一说话人 + 相邻」时合并——跨说话人合并会破坏归属，实测 654 个碎段里
        # 566 个后段是不同人或间隔远，强行并会把两个人合成一个人。
        if args.min_seg > 0:
            merged_seg, merged_lab = [], []
            for (s, e), lab in zip(segments, labels):
                if (merged_seg and (e - s) < args.min_seg
                        and merged_lab[-1] == lab
                        and s - merged_seg[-1][1] <= args.merge_gap
                        and e - merged_seg[-1][0] <= args.max_unit):
                    merged_seg[-1][1] = e      # 并入前段尾部，说话人不变
                else:
                    merged_seg.append([s, e])
                    merged_lab.append(lab)
            segments = [(a, b) for a, b in merged_seg]
            labels = np.array(merged_lab)
        else:
            labels = np.array(labels)
        log(f"[2] pyannote 分离: {len(turns)} 个片段 -> {len(spk_ids)} 个说话人"
            + (f"（超长切分后 {len(segments)} 段）" if len(segments) != len(turns) else "")
            + (f"，碎段并入后 {len(segments)} 段（原 {n_before_merge} 段）"
               if len(segments) != n_before_merge else "")
            + (f"（超长切分后 {len(segments)} 段）" if len(segments) != len(turns) else "")
            + (f"，重叠区间 {len(overlaps)} 处" if overlaps else "")
            + f" (累计 {time.time()-t0:.0f}s)")
    else:
        from sklearn.cluster import AgglomerativeClustering

        segments = run_vad(src, args.vad, args.device)
        if not segments:
            sys.exit("未检测到语音(VAD 结果为空)")
        n_raw = len(segments)
        segments = merge_short_segments(segments, args.min_seg, 500)   # 碎段先并入邻近段
        segments = split_long_segments(segments, args.max_unit, audio=audio)  # 超长段切分(静音点吸附)
        log(f"[2] VAD({args.vad}) 切出 {n_raw} 个语音段"
            + (f" -> 碎段合并/超长切分后 {len(segments)} 段" if len(segments) != n_raw else "")
            + f" (累计 {time.time()-t0:.0f}s)")

        from funasr import AutoModel
        spk = AutoModel(model="cam++", device=args.device, disable_update=True)

        def to_numpy(x):
            return x.cpu().numpy() if hasattr(x, "cpu") else np.asarray(x)

        embeddings = []
        for (s_ms, e_ms) in segments:
            seg = audio[s_ms * 16: e_ms * 16]
            emb = to_numpy(spk.generate(input=seg, fs=16000)[0]["spk_embedding"]).flatten()
            embeddings.append(emb / np.linalg.norm(emb))
        embeddings = np.array(embeddings)
        del spk; gc_cuda()   # 声纹提取完毕,campp 不驻留到主引擎批量转写阶段
        # campp 分支的声纹按段一一对应（无跳过），质心用于声纹库
        all_seg_idx = list(range(len(segments)))

        sim = embeddings @ embeddings.T
        dist = 1 - sim
        np.fill_diagonal(dist, 0)
        if len(segments) >= 2:
            labels = AgglomerativeClustering(
                n_clusters=None, distance_threshold=1 - args.threshold,
                metric="precomputed", linkage="average",
            ).fit_predict((dist + dist.T) / 2)
            n_raw_spk = len(set(labels.tolist()))
            labels = absorb_tiny_clusters(embeddings, labels, segments)
            n_spk = len(set(labels.tolist()))
            log(f"[3] 声纹聚类完成: {n_spk} 个说话人 (阈值 {args.threshold})"
                + (f"；小簇吸收 {n_raw_spk} -> {n_spk}（碎段簇并入最相似大簇）" if n_spk < n_raw_spk else ""))

            # 簇间相似度边缘提示:帮助发现"同人被拆分"(实测合成音频 5 人聚成 6 簇即此情形)
            uniq = sorted(set(labels.tolist()))
            if len(uniq) >= 2:
                pair_sims = []
                for i in range(len(uniq)):
                    for j in range(i + 1, len(uniq)):
                        a, b = embeddings[labels == uniq[i]], embeddings[labels == uniq[j]]
                        pair_sims.append((float((a @ b.T).mean()), uniq[i], uniq[j]))
                pair_sims.sort(reverse=True)
                top_sim, spk_a, spk_b = pair_sims[0]
                if top_sim >= args.threshold - 0.08:
                    log(f"提示: 说话人{spk_a}与说话人{spk_b}声纹相似度 {top_sim:.2f}, 接近阈值 {args.threshold}; "
                        f"若实为同一人可试 --threshold {max(0.5, args.threshold - 0.05):.2f}")
        else:
            labels = np.zeros(len(segments), dtype=int)
            log("[3] 语音段不足 2 段,跳过聚类(视为单一说话人)")

    # 合并相邻同说话人碎段(预设 800ms,纪要可读性)
    units = merge_by_speaker(segments, labels, args.merge_gap, args.max_unit)
    if args.merge_gap > 0 and len(units) < len(segments):
        log(f"[4] 碎段合并: {len(segments)} 段 -> {len(units)} 个发言块 (间隔阈值 {args.merge_gap}ms)")
    else:
        log(f"[4] 输出 {len(units)} 个发言块")

    tmp_dir = Path(tempfile.mkdtemp(prefix="qwen_asr_seg_")).resolve()
    atexit.register(lambda d=tmp_dir: shutil.rmtree(d, ignore_errors=True))   # 分段 wav 随进程清理
    unit_files = write_unit_wavs(audio, units, tmp_dir)

    parsed = infer(unit_files)
    punc = None; gc_cuda()   # 转写已完成,置 None 放开闭包 cell 引用
    if len(parsed) != len(units):  # 防御：任何引擎返回短列表都不能让 zip 静默截断
        print(f"[warn] 识别结果 {len(parsed)} 条 / 输入 {len(units)} 条，缺失段按空文本补齐",
              file=sys.stderr)
        parsed = list(parsed) + [{"transcription": ""}
                                 for _ in range(len(units) - len(parsed))]
    # 声纹库：跨文件复用说话人身份（同一批会议里不必每次都重新认人）
    # pyannote 与 campp 现在都能供声纹：campp 分支的 embeddings 按段一一对应，
    # pyannote 分支在现场用 extract_segment_embeddings 按已定好的段边界补算。
    db_names = {}
    if speaker_db_path and len(labels):
        emb = locals().get("embeddings")
        valid_idx = locals().get("all_seg_idx")
        if emb is None or not len(emb):
            log("[4c] 提取声纹向量供声纹库使用（pyannote 路径按已定段边界补算）")
            emb, valid_idx = extract_segment_embeddings(audio, segments, args.device)
        if emb is None or not len(emb):
            print("[提示] 未能提取到可用声纹（语音段过短或过少），声纹库本次跳过",
                  file=sys.stderr)
        else:
            seg_labels = [int(x) for x in labels]
            rows = valid_idx if valid_idx else list(range(len(seg_labels)))
            # 只取有向量的段对应的标签；行号与 emb 行一一对应
            row_labels = [seg_labels[i] for i in rows if i < len(seg_labels)]
            cents = centroid_by_label(emb, row_labels)
            db = load_speaker_db(speaker_db_path)
            for c, cen in cents.items():
                hit, sim = match_speaker_db(cen, db, args.speaker_db_threshold)
                if hit:
                    db_names[c] = hit
                    log(f"[4c] 声纹库命中: 说话人{c} -> {hit}（相似度 {sim:.3f}）")
            if speaker_db_save:
                for c, cen in cents.items():
                    db[names.get(c) or db_names.get(c) or f"说话人{c}"] = cen
                save_speaker_db(speaker_db_path, db)
                log(f"[4c] 声纹库已更新: {speaker_db_path}（共 {len(db)} 条）")
    for u, p in zip(units, parsed):
        u["text"] = p["transcription"]
        u["language"] = p.get("language")
        u["spk_name"] = names.get(u["spk"]) or db_names.get(u["spk"])
    aligns = None
    if use_aed and all(p.get("timestamp") for p in parsed):
        if all(len(p["timestamp"]) == len(p["transcription"]) for p in parsed):
            aligns = aed_aligns_from(parsed)
            log("[4b] 使用 AED 原生字级时间戳")
        elif fa is not None or qwen_aligner is not None:
            aligns = align_units(unit_files, [u["text"] for u in units])
    elif use_cloud and args.cloud_model == "message" and args.srt:
        # 部分段有字级时间戳就用，别要求全部都有：90 分钟素材实测 211 段里有少数段
        # 云端没回 words，原先 all() 判定直接放弃整份字幕。缺的段回填空 ts，
        # subtitle_units 会按「整段一条」兜底，其余段仍是字级精确边界。
        have = sum(1 for p in parsed if p.get("cloud_words"))
        if have:
            aligns = [{"tokens": [], "ts": p.get("cloud_words") or []} for p in parsed]
            log(f"[4b] 使用云端 message 字级时间戳（跳过 fa-zh，{have}/{len(parsed)} 段有）")
            if have < len(parsed):
                print(f"[warn] {len(parsed) - have} 段云端未返回字级时间戳，"
                      f"这些段字幕按整段时长兜底", file=sys.stderr)
        else:
            print("[warn] 云端未返回字级时间戳,本次未产出字幕", file=sys.stderr)
    elif fa is not None or qwen_aligner is not None:
        aligns = align_units(unit_files, [u["text"] for u in units])
    fa = None; qwen_aligner = None; gc_cuda()   # 对齐已完成
    log(f"[5] 转写完成 (累计 {time.time()-t0:.0f}s),结果:")

    lines = []
    for u in units:
        s_ms, e_ms = u["start_ms"], u["end_ms"]
        mm1, ss1 = divmod(s_ms // 1000, 60)
        mm2, ss2 = divmod(e_ms // 1000, 60)
        label = u["spk_name"] or f"说话人{u['spk']}"
        text = u["text"]
        print(f"{label} [{mm1:02d}:{ss1:02d}-{mm2:02d}:{ss2:02d}] {text}")
        lines.append({"spk": u["spk"], "spk_name": u["spk_name"], "start_ms": s_ms, "end_ms": e_ms,
                      "text": text, "language": u.get("language")})

    emotions = analyze_emotion(audio_path, args.device) if args.emotion else None
    if emotions:
        log("[5b] 情绪分析: " + "、".join(f"{e['label']} {e['score']:.2f}" for e in emotions))
    payload = {"schema_version": SCHEMA_VERSION, "engine": _engine_tag, "audio": str(audio_path),
               "speakers": names or None, "lines": lines}
    if overlaps:
        # 重叠区间（两人及以上同时发言）：只做标记不改归属。纪要提炼时它是"这段在抢话"
        # 的信号，比把交叠内容强行判给某一个人更诚实。
        payload["overlaps"] = [{"start_ms": int(s), "end_ms": int(e)} for s, e in overlaps]
        log(f"[5b] 重叠语音区间 {len(overlaps)} 处（已写入 overlaps 字段）")
    if emotions:
        payload["emotions"] = emotions
    if aligns is not None:
        subs = subtitle_units(units, aligns, args.max_line, args.min_dur, args.max_cps)
        payload["sentences"] = subs
        write_subtitles(subs, args, out_srt, use_speaker=True)
    if getattr(args, "rttm", False):
        n = write_rttm(lines, audio_path.with_suffix(".rttm"), uri=audio_path.stem)
        log(f"[6b] RTTM 说话人日志 {n} 行: {audio_path.with_suffix('.rttm')}")
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[6] 结构化结果已保存: {out_json}" + (f", 字幕: {out_srt}" if aligns is not None else "")
        + f"  (总耗时 {time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
