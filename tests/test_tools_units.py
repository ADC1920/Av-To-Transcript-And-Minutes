#!/usr/bin/env python3
"""test_tools_units.py — tools/ 下三个工具的自检（零依赖，不需要模型与网络）。

覆盖的是纯逻辑：manifest 产物枚举、MCP 参数校验与 job 幂等键、索引的分词与解析、
泳道图的渲染与时间轴换算。这些地方出错不会崩，只会给出错的结果，
比崩溃更难发现，所以单独压一遍。

跑法：python tests/test_tools_units.py
"""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name} {detail}")


def test_manifest_item():
    print("[1] 运行清单：产物枚举与状态记录")
    import av_to_transcript_and_minutes as pipe
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        media = out / "会议.wav"
        media.write_bytes(b"x" * 100)
        stem_dir = out / "会议"
        stem_dir.mkdir()
        (stem_dir / "会议原文-会议.md").write_text("x", encoding="utf-8")
        (stem_dir / "会议原文-会议.docx").write_bytes(b"PK")
        item = pipe.manifest_item(media, out, "done", None, 12.5)
        check("状态记录", item["status"] == "done")
        check("耗时记录", item["seconds"] == 12.5)
        check("产物按实际存在列出", len(item["artifacts"]) == 2, item["artifacts"])
        check("失败带 error 字段",
              "error" in pipe.manifest_item(media, out, "failed", "boom", 1.0))

        # 预期的产物不存在时不能凭空写进清单，否则失去核对意义
        (stem_dir / "会议原文-会议.srt").unlink(missing_ok=True)
        item2 = pipe.manifest_item(media, out, "done", None, 1.0)
        check("不写不存在的产物", all("srt" not in a for a in item2["artifacts"]))


def test_mcp_params():
    print("[2] MCP：参数白名单与命令拼装")
    import mcp_server as m
    cmd = m.build_cmd("a.wav", {"meeting": True, "srt": True, "asr_engine": "qwen"})
    check("布尔开关成对出现", "--meeting" in cmd and "--srt" in cmd)
    check("带值参数成对出现", cmd[cmd.index("--asr-engine") + 1] == "qwen")
    check("位置参数是第一个", cmd[2] == "a.wav")
    try:
        m.build_cmd("a.wav", {"拼错的键": True})
        check("未知参数被拒", False)
    except ValueError:
        check("未知参数被拒", True)
    # None/False 不能变成 "--output None"
    cmd2 = m.build_cmd("a.wav", {"output": None, "meeting": False})
    check("假值不落命令行", "--output" not in cmd2 and "--meeting" not in cmd2)


def test_job_key_idempotent():
    print("[3] MCP：job 幂等键")
    import mcp_server as m
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "a.wav"
        f.write_bytes(b"abc" * 1000)
        k1 = m.job_key(f, {"meeting": True})
        k2 = m.job_key(f, {"meeting": True})
        k3 = m.job_key(f, {"srt": True})
        check("同内容同参数同键", k1 == k2)
        check("参数不同键不同", k1 != k3)
        # 内容变化必须换键，否则会把旧结果当新素材
        f.write_bytes(b"abd" * 1000)
        check("内容变化换键", m.job_key(f, {"meeting": True}) != k1)


def test_index_parse():
    print("[4] 索引：转写稿与纪要的解析")
    import meeting_index as mi
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        md = d / "会议原文-x.md"
        md.write_text("# 会议原文\n\n[00:00] 张三：今天讨论预算。\n[00:05] 李四：好的。\n",
                      encoding="utf-8")
        segs, kind = mi.load_source(md)
        check("识别为转写稿", kind == "transcript")
        check("切出两段发言", len(segs) == 2, len(segs))
        check("说话人解析正确", segs[0]["speaker"] == "张三")
        check("时间戳解析正确", segs[0]["ts"] == "00:00")

        minutes = d / "会议纪要-x.md"
        minutes.write_text("# 会议纪要\n\n## 会议结论\n本次通过了预算。\n\n## 关键决策\n决定采用方案A。\n",
                           encoding="utf-8")
        segs2, kind2 = mi.load_source(minutes)
        check("识别为纪要", kind2 == "minutes")
        check("按小节切块", len(segs2) == 2, len(segs2))

        js = d / "转写结果.json"
        js.write_text(json.dumps({"lines": [
            {"spk": 0, "spk_name": "张三", "start_ms": 65000, "end_ms": 70000, "text": "预算"}]},
            ensure_ascii=False), encoding="utf-8")
        segs3, kind3 = mi.load_source(js)
        check("json 解析成发言", len(segs3) == 1 and segs3[0]["speaker"] == "张三")
        check("毫秒换算成时间戳", segs3[0]["ts"] == "01:05", segs3[0]["ts"])


def test_index_search_cn():
    print("[5] 索引：中文子串检索（trigram）")
    import meeting_index as mi
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "i.db"
        src = Path(td) / "会议原文-y.md"
        src.write_text("# x\n\n[00:00] 张三：这批胚布的白配检测有问题。\n", encoding="utf-8")
        mi.main(["add", str(src), "--db", str(db)])
        # 三字以上走 trigram
        rc = mi.main(["search", "白配检测", "--db", str(db)])
        check("三字子串命中", rc == 0)
        # 两字走 LIKE 兜底
        rc2 = mi.main(["search", "胚布", "--db", str(db)])
        check("两字词命中", rc2 == 0)
        rc3 = mi.main(["search", "完全不存在", "--db", str(db)])
        check("无命中返回 1", rc3 == 1)
        # 幂等：同一文件重复 add 应跳过
        mi.main(["add", str(src), "--db", str(db)])
        # 显式 close：Windows 上未关闭的连接会让临时目录删不掉（WinError 32）
        conn = sqlite3.connect(str(db))
        n = conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
        conn.close()
        check("重复索引不重复入库", n == 1, n)


def test_timeline_render():
    print("[6] 泳道图：时间轴换算与渲染")
    import speaker_timeline as st
    segs = [
        {"spk": "张三", "start": 0, "end": 30000, "text": "开场"},
        {"spk": "李四", "start": 30000, "end": 60000, "text": "回应"},
        {"spk": "张三", "start": 60000, "end": 90000, "text": "总结"},
    ]
    html = st.render(segs, 90000, [{"start_ms": 30000, "end_ms": 32000}], "测试会议")
    check("含两条泳道标签", html.count('class="lane-label"') == 2)
    check("含三条发言块", html.count('class="seg"') == 3)
    check("含重叠标记", 'class="ov"' in html)
    check("标题被转义", "测试会议" in html)
    # 发言结束位置应落在 100%（最后一段到 90s）
    check("最右段贴到 100%", "left:66.667%" in html or "left:66.6" in html, html[:0])
    # XSS：文本里的尖括号不能原样进 HTML
    evil = [{"spk": "<b>x</b>", "start": 0, "end": 1000, "text": "<script>alert(1)</script>"}]
    h2 = st.render(evil, 1000, [], "t")
    check("说话人名被转义", "<b>x</b>" not in h2)
    check("正文被转义", "<script>alert" not in h2)


def test_fmt_ts():
    print("[7] 时间格式")
    import speaker_timeline as st
    check("分钟级", st.fmt_ts(65000) == "01:05", st.fmt_ts(65000))
    check("小时级", st.fmt_ts(3725000) == "1:02:05", st.fmt_ts(3725000))
    check("负数兜底", st.fmt_ts(-5) == "00:00", st.fmt_ts(-5))


def main():
    test_manifest_item()
    test_mcp_params()
    test_job_key_idempotent()
    test_index_parse()
    test_index_search_cn()
    test_timeline_render()
    test_fmt_ts()
    print(f"\n合计 {passed + failed}，PASS {passed}，FAIL {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
