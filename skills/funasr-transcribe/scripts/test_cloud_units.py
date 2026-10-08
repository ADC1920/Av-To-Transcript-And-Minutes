# -*- coding: utf-8 -*-
"""cloud 引擎离线单测：百炼三通道的纯逻辑与解析（全程 mock，零网络零 API 花费）。

运行：python scripts/test_cloud_units.py
覆盖：_cloud_hotword_vocab 即时词表 / cloud_post 退避重试 / _cloud_message_seg 解析与字级
      时间戳展开 / _cloud_omni_seg 请求体与文本提取 / cloud_transcribe_batch 保序与后处理链 /
      load_dashscope_key 取值优先级与无密钥友好退出。
"""
import os
import sys
import tempfile
import types
import shutil
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qwen_asr
from qwen_asr import (_cloud_hotword_vocab, _cloud_message_seg, _cloud_omni_seg,
                      cloud_transcribe_batch, load_dashscope_key)

fails = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        fails.append(name)


# 退避重试的真实 sleep 拦截：测试不真睡
class _TimeStub:
    def sleep(self, s):
        pass


_real_time = qwen_asr.time
qwen_asr.time = _TimeStub()

print("[1] _cloud_hotword_vocab 即时词表")
v = _cloud_hotword_vocab("十三希诺,柯桥、面料")
check("逗号/顿号分拆", v == {"十三希诺": 4, "柯桥": 4, "面料": 4}, v)
check("权重固定 4", all(w == 4 for w in v.values()), v)
check("空串=None", _cloud_hotword_vocab("") is None)
check("None=None", _cloud_hotword_vocab(None) is None)
check("空白词剔除", _cloud_hotword_vocab(" 甲 , ,乙 ") == {"甲": 4, "乙": 4})
big = ",".join(f"词{i}" for i in range(60))
v2 = _cloud_hotword_vocab(big, log=lambda m: None)
check("上限 50 截断", len(v2) == 50 and "词0" in v2 and "词50" not in v2, len(v2))

print("[2] cloud_post 退避重试（mock requests）")


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class _ReqModule(types.ModuleType):
    """伪 requests：post 按预设序列返回响应或抛异常，记录调用次数"""
    class RequestException(Exception):
        pass

    def __init__(self, seq):
        super().__init__("requests")
        self.seq = list(seq)
        self.calls = 0

    def post(self, url, headers=None, json=None, timeout=None):
        item = self.seq[min(self.calls, len(self.seq) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def _with_requests(seq):
    fake = _ReqModule(seq)
    old = sys.modules.get("requests")
    sys.modules["requests"] = fake
    return fake, old


def _restore_requests(old):
    if old is None:
        sys.modules.pop("requests", None)
    else:
        sys.modules["requests"] = old


NetErr = _ReqModule.RequestException
ok = {"choices": [{"message": {"content": "好"}}]}

fake, old = _with_requests([_Resp(200, ok)])
try:
    j = qwen_asr.cloud_post("http://x", {}, {"a": 1}, log=lambda m: None)
    check("200 直返 json", j == ok and fake.calls == 1, (j, fake.calls))
finally:
    _restore_requests(old)

fake, old = _with_requests([_Resp(429, text="busy"), _Resp(200, ok)])
try:
    j = qwen_asr.cloud_post("http://x", {}, {}, log=lambda m: None)
    check("429 重试后成功", j == ok and fake.calls == 2, (j, fake.calls))
finally:
    _restore_requests(old)

fake, old = _with_requests([_Resp(500, text="boom")] * 5)
try:
    try:
        qwen_asr.cloud_post("http://x", {}, {}, retries=2, log=lambda m: None)
        check("5xx 重试耗尽退出", False)
    except SystemExit as e:
        check("5xx 重试耗尽退出", "HTTP 500" in str(e), str(e))
finally:
    _restore_requests(old)

fake, old = _with_requests([NetErr("conn reset"), _Resp(200, ok)])
try:
    j = qwen_asr.cloud_post("http://x", {}, {}, log=lambda m: None)
    check("网络异常重试后成功", j == ok and fake.calls == 2, (j, fake.calls))
finally:
    _restore_requests(old)

fake, old = _with_requests([NetErr("conn reset")] * 5)
try:
    try:
        qwen_asr.cloud_post("http://x", {}, {}, retries=1, log=lambda m: None)
        check("网络异常耗尽退出", False)
    except SystemExit as e:
        check("网络异常耗尽退出", "网络失败" in str(e), str(e))
finally:
    _restore_requests(old)

print("[3] _cloud_message_seg 解析与字级时间戳（mock dashscope SDK）")


class _RecRes:
    def __init__(self, status_code=200, sents=None, message=""):
        self.status_code = status_code
        self._sents = sents or []
        self.message = message

    def get_sentence(self):
        return self._sents


def _install_fake_dashscope(res):
    dash = types.ModuleType("dashscope")
    dash.api_key = None
    audio = types.ModuleType("dashscope.audio")
    asr = types.ModuleType("dashscope.audio.asr")
    captured = {}

    class Recognition:
        def __init__(self, **kw):
            captured.update(kw)

        def call(self, path):
            captured["path"] = path
            return res

    asr.Recognition = Recognition
    dash.audio = audio
    audio.asr = asr
    mods = {"dashscope": dash, "dashscope.audio": audio, "dashscope.audio.asr": asr}
    saved = {k: sys.modules.get(k) for k in mods}
    sys.modules.update(mods)
    return captured, saved


def _restore_saved(saved):
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


sents = [
    {"text": "开放时间，",
     "words": [{"text": "开放", "begin_time": 0, "end_time": 400},
               {"text": "时间", "begin_time": 400, "end_time": 800}]},
    {"text": "早上九点。",
     "words": [{"text": "早上", "begin_time": 800, "end_time": 1200},
               {"text": "九点", "begin_time": 1200, "end_time": 1600}]},
]
cap, saved = _install_fake_dashscope(_RecRes(sents=sents))
try:
    txt, ts = _cloud_message_seg("dummy.wav", {"key": "sk-x", "hotwords": "十三希诺"})
    check("文本拼接", txt == "开放时间，早上九点。", txt)
    check("字级时间戳展开(8 字符)", len(ts) == 8, len(ts))
    check("词内均分", ts[0] == [0, 200] and ts[1] == [200, 400], ts[:2])
    check("末字符时间戳", ts[-1] == [1400, 1600], ts[-1])
    check("热词进 vocabulary", cap.get("vocabulary") == {"十三希诺": 4}, cap.get("vocabulary"))
    check("model 走 message 通道", cap.get("model") == qwen_asr.CLOUD_MODELS["message"], cap.get("model"))
    check("sample_rate=16000", cap.get("sample_rate") == 16000, cap.get("sample_rate"))
finally:
    _restore_saved(saved)

cap, saved = _install_fake_dashscope(_RecRes(status_code=400, message="bad"))
try:
    try:
        _cloud_message_seg("d.wav", {"key": "sk"})
        check("非 200 退出", False)
    except SystemExit as e:
        check("非 200 退出", "message 通道失败" in str(e), str(e))
finally:
    _restore_saved(saved)

print("[4] _cloud_omni_seg 请求体与文本提取（mock cloud_post）")

tmpd = tempfile.mkdtemp(prefix="cloud_omni_test_")
try:
    wav = Path(tmpd) / "t.wav"
    wav.write_bytes(b"RIFF....")
    captured = {}

    def fake_post(url, headers, body, **kw):
        captured.update(url=url, headers=headers, body=body)
        return {"choices": [{"message": {"content": " 测试文本 "}}]}

    real_post = qwen_asr.cloud_post
    qwen_asr.cloud_post = fake_post
    try:
        out = _cloud_omni_seg(str(wav), {"key": "sk-y", "hotwords": "热词A", "log": print})
        check("返回去除空白", out == "测试文本", out)
        b = captured["body"]
        check("model=omni", b["model"] == qwen_asr.CLOUD_MODELS["omni"], b["model"])
        check("modalities 限文本", b["modalities"] == ["text"], b.get("modalities"))
        check("reasoning_effort=none", b["reasoning_effort"] == "none")
        check("音频 data URI",
              b["messages"][0]["content"][0]["input_audio"]["data"].startswith("data:;base64,"))
        text_part = b["messages"][0]["content"][-1]["text"]
        check("热词入 prompt", "热词A" in text_part, text_part[:60])
        check("Authorization 头", captured["headers"]["Authorization"] == "Bearer sk-y")
    finally:
        qwen_asr.cloud_post = real_post
finally:
    shutil.rmtree(tmpd, ignore_errors=True)

print("[5] cloud_transcribe_batch 保序与后处理（mock 段级函数）")

saved_ms = qwen_asr._cloud_message_seg
saved_om = qwen_asr._cloud_omni_seg


class _Matcher:
    def apply_text(self, t):
        return (t.replace("x", "X"),)


try:
    qwen_asr._cloud_message_seg = lambda p, ctx: (f"文{p}", [[0, 100]])
    qwen_asr._cloud_omni_seg = lambda p, ctx: f"文{p}"

    out = cloud_transcribe_batch(["a", "b", "c"], {"model": "message", "key": "k", "log": lambda m: None})
    check("保序", [o["transcription"] for o in out] == ["文a", "文b", "文c"], out)
    check("message 带 cloud_words", all(o.get("cloud_words") == [[0, 100]] for o in out))

    out2 = cloud_transcribe_batch(["a"], {"model": "omni", "key": "k", "log": lambda m: None})
    check("omni 无 cloud_words", "cloud_words" not in out2[0], out2[0])

    out3 = cloud_transcribe_batch(["x"], {"model": "message", "key": "k", "log": lambda m: None},
                                  t2s=lambda s: s + "T", itn=lambda s: s + "I", matcher=_Matcher())
    check("后处理链 t2s→itn→replace", out3[0]["transcription"] == "文XTI", out3[0]["transcription"])
finally:
    qwen_asr._cloud_message_seg = saved_ms
    qwen_asr._cloud_omni_seg = saved_om

print("[6] load_dashscope_key 优先级与无密钥友好退出")

_saved_env = os.environ.pop("DASHSCOPE_API_KEY", None)
try:
    os.environ["DASHSCOPE_API_KEY"] = "sk-env-test"
    check("环境变量优先", load_dashscope_key() == "sk-env-test")
    os.environ.pop("DASHSCOPE_API_KEY", None)

    # 模拟「注册表也没有」：Windows 用伪 winreg；类 Unix 靠 import winreg 失败路径
    fake_reg = types.ModuleType("winreg")
    fake_reg.HKEY_CURRENT_USER = 1

    def _open(*a, **kw):
        raise OSError("not found")

    fake_reg.OpenKey = _open
    old_reg = sys.modules.get("winreg")
    sys.modules["winreg"] = fake_reg
    try:
        try:
            load_dashscope_key()
            check("无密钥友好退出", False)
        except SystemExit as e:
            check("无密钥友好退出", "DASHSCOPE_API_KEY" in str(e), str(e))
    finally:
        if old_reg is None:
            sys.modules.pop("winreg", None)
        else:
            sys.modules["winreg"] = old_reg
finally:
    if _saved_env is not None:
        os.environ["DASHSCOPE_API_KEY"] = _saved_env
    qwen_asr.time = _real_time

print()
if fails:
    print(f"FAILED: {len(fails)} 项 -> {fails}")
    sys.exit(1)
print("ALL PASS")
