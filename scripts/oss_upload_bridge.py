"""OSS 上传桥接：本地音频 → 带签名的公网 URL，供百炼 filetrans 通道读取。

用途：filetrans 只接受公网可访问 URL，本机没有天然出口。本脚本把文件传到用户自建的
      OSS Bucket，默认生成**签名 URL**（私有 Bucket 也能读），无需把 Bucket 改成公共读。

跑法：
  # 只上传，打印签名 URL
  python scripts/oss_upload_bridge.py <本地文件>
  # 上传 + 直接跑 filetrans 全流程（产物落在音频同目录）
  python scripts/oss_upload_bridge.py <本地文件> --run --extra --srt --vtt

依赖：oss2（已在 .venv 中，版本 2.19.1）

凭据只从环境变量读取，不落盘、不入库、不打印：
  OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET
可选默认值（省去每次传参）：
  OSS_BUCKET / OSS_ENDPOINT

签名有效期默认 24 小时：filetrans 是异步任务，长音频排队+转写可能耗时较久，
URL 太短会在转写中途失效（实测 3 小时音频排队数分钟才开跑）。转写完成后对象仍留在
Bucket 里，含业务信息，需自行清理。
"""
import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 签名有效期：24 小时。filetrans 异步任务从排队到取结果可能跨数十分钟，
# 短时效 URL 会在中途失效，故给足余量（这是上限而不是典型耗时）。
DEFAULT_SIGNED_SECONDS = 24 * 3600


def require_env(name: str) -> str:
    v = (os.environ.get(name) or "").strip()
    if not v:
        sys.exit(f"缺少环境变量 {name}（凭据不落盘，只从环境变量读）")
    return v


def main() -> int:
    ap = argparse.ArgumentParser(description="把本地音频传到 OSS，生成签名 URL 供 filetrans 读取")
    ap.add_argument("file", help="本地音频文件路径")
    ap.add_argument("--bucket", default=os.environ.get("OSS_BUCKET"),
                    help="Bucket 名（默认读 OSS_BUCKET）")
    ap.add_argument("--endpoint", default=os.environ.get("OSS_ENDPOINT"),
                    help="Endpoint，如 oss-cn-beijing.aliyuncs.com（默认读 OSS_ENDPOINT）")
    ap.add_argument("--prefix", default="av-transcribe",
                    help="对象键前缀目录（默认 av-transcribe）")
    ap.add_argument("--signed-seconds", type=int, default=DEFAULT_SIGNED_SECONDS,
                    help=f"签名 URL 有效期秒数（默认 {DEFAULT_SIGNED_SECONDS}，即 24 小时）")
    ap.add_argument("--public-url", action="store_true",
                    help="改用公共读直连 URL（仅当 Bucket 已设为公共读时需要）")
    ap.add_argument("--run", action="store_true",
                    help="上传后直接调 filetrans 跑完整转写（一步到位）；不加则只上传并打印 URL")
    ap.add_argument("--keep-object", action="store_true",
                    help="转写成功后保留 OSS 对象（默认删除，避免业务音频长期留在云端）")
    ap.add_argument("--no-compress", action="store_true",
                    help="跳过上传前压缩（默认转 16k 单声道 mp3，体积可缩到 1/20）")
    # 其余未知参数原样透传给 qwen_asr.py（如 --srt --vtt --num-speakers 2）。
    # 用 parse_known_args 而不是 --extra nargs="*"：后者遇到以 - 开头的值会当成
    # 本脚本的参数而报 unrecognized，用户得写成 --extra=--srt，反直觉。
    args, passthrough = ap.parse_known_args()

    src = Path(args.file).expanduser().resolve()
    if not src.is_file():
        sys.exit(f"文件不存在: {src}")
    if not args.bucket:
        sys.exit("缺少 Bucket 名：用 --bucket 或设环境变量 OSS_BUCKET")
    if not args.endpoint:
        sys.exit("缺少 Endpoint：用 --endpoint 或设环境变量 OSS_ENDPOINT")

    try:
        import oss2
    except ImportError:
        sys.exit("缺少 oss2：在项目 venv 里执行 uv add oss2")

    auth = oss2.Auth(require_env("OSS_ACCESS_KEY_ID"), require_env("OSS_ACCESS_KEY_SECRET"))
    bucket = oss2.Bucket(auth, args.endpoint, args.bucket)

    upload_src = src
    tmp_compressed = None
    if not args.no_compress:
        # ASR 内部一律按 16kHz 单声道处理，原样上传 44.1kHz 立体声 wav 只是把
        # 20 倍冗余数据推过网线。实测 90 分钟素材 912MB -> 16k 单声道 64k mp3 仅 41MB，
        # 本机上行 1.2MB/s 时上传从 13 分钟降到 34 秒。转码不损失识别信息。
        import shutil
        import subprocess as _sp
        if not shutil.which("ffmpeg"):
            print("[warn] 未找到 ffmpeg，跳过压缩直接上传原文件", file=sys.stderr)
        else:
            tmp_compressed = Path(".tmp") / f"oss-upload-{src.stem}.mp3"
            tmp_compressed.parent.mkdir(parents=True, exist_ok=True)
            t_c = time.time()
            print(f"[0] 压缩到 16k 单声道 mp3: {tmp_compressed.name}", file=sys.stderr)
            rc = _sp.call(["ffmpeg", "-y", "-v", "error", "-i", str(src),
                           "-ar", "16000", "-ac", "1", "-b:a", "64k", str(tmp_compressed)])
            if rc != 0 or not tmp_compressed.is_file():
                print("[warn] 压缩失败，改用原文件上传", file=sys.stderr)
                tmp_compressed = None
            else:
                upload_src = tmp_compressed
                print(f"[0b] 压缩完成 {time.time()-t_c:.1f}s，"
                      f"{src.stat().st_size/1024/1024:.1f} MB -> "
                      f"{tmp_compressed.stat().st_size/1024/1024:.1f} MB", file=sys.stderr)

    key = f"{args.prefix.strip('/')}/{upload_src.name}"
    size_mb = upload_src.stat().st_size / 1024 / 1024
    print(f"[1] 上传 {upload_src.name}（{size_mb:.1f} MB）-> oss://{args.bucket}/{key}", file=sys.stderr)

    t_up = time.time()
    try:
        # 统一用简单 PUT 而不是 resumable_upload：后者在部分 Bucket 上会在
        # InitiateMultipartUpload 阶段返回异常 XML（实测 OSS 返回分片列表时
        # 解析失败 mismatched tag），且会在 .tmp 留续传记录污染后续尝试。
        # put_object_from_file 内部走分块流式上传，近 GB 文件同样可用。
        bucket.put_object_from_file(key, str(upload_src))
    except oss2.exceptions.OssError as e:
        sys.exit(f"上传失败: {e.code} {e.message}\n"
                 f"排查：AccessKey 是否有效且已授权该 Bucket、Bucket 地域与 endpoint 是否匹配")
    finally:
        if tmp_compressed is not None:
            tmp_compressed.unlink(missing_ok=True)
    speed = size_mb / max(time.time() - t_up, 0.01)
    print(f"[1b] 上传完成，耗时 {time.time()-t_up:.1f}s（{speed:.1f} MB/s）", file=sys.stderr)

    if args.public_url:
        host = args.endpoint.replace("http://", "").replace("https://", "").rstrip("/")
        url = f"https://{args.bucket}.{host}/{key}"
        print("[2] 已生成公共读直连 URL（Bucket 必须已设为公共读）", file=sys.stderr)
    else:
        url = bucket.sign_url("GET", key, args.signed_seconds, slash_safe=True)
        hours = args.signed_seconds / 3600
        print(f"[2] 已生成签名 URL（{hours:.1f} 小时有效，私有 Bucket 可读）", file=sys.stderr)

    if not args.run:
        print(url)
        return 0

    print("[3] 提交 filetrans 转写", file=sys.stderr)
    qwen_asr = (Path(__file__).resolve().parent.parent
                / "skills" / "funasr-transcribe" / "scripts" / "qwen_asr.py")
    if not qwen_asr.is_file():
        sys.exit(f"找不到 {qwen_asr}")
    cmd = [sys.executable, str(qwen_asr), str(src),
           "--engine", "cloud", "--cloud-model", "filetrans",
           "--cloud-url", url, "--diarize"]
    cmd += passthrough
    # 打印时脱敏：签名 URL 里的 Signature 与 AccessKeyId 等价于临时凭据，
    # 落进日志或终端回滚缓冲就等于泄漏（实测 grep 全项目确认过这个风险）。
    safe = [re.sub(r"(Signature|OSSAccessKeyId|security-token)=[^&]*", r"\1=<REDACTED>", str(c))
            for c in cmd]
    print("[3] " + " ".join(safe), file=sys.stderr)
    rc = subprocess.call(cmd)

    # 默认清理：业务音频不该长期留在云端。失败也清理——重跑会重新上传，
    # 留着不清理的坏处（数据滞留）大于省一次上传的好处。
    if not args.keep_object:
        try:
            bucket.delete_object(key)
            print("[4] 已删除 OSS 对象（--keep-object 可保留）", file=sys.stderr)
        except oss2.exceptions.OssError as e:
            print(f"[warn] 删除 OSS 对象失败（可手工清理 oss://{args.bucket}/{key}）: {e.code}",
                  file=sys.stderr)

    print(f"[4] 产物落在 {src.with_suffix('.json')} / .srt", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
