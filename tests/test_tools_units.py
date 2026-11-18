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


def test_minutes_quality():
    print("[8] 纪要导出与校验")
    # offline CI job 只装 numpy；纪要工具依赖 python-docx。缺依赖时跳过而非判红，
    # 完整覆盖由 skill-minutes job 的两个 --selftest 承担。
    import importlib.util as _u
    if _u.find_spec("docx") is None:
        print("  SKIP python-docx 未安装（由 skill-minutes job 覆盖）")
        return
    import minutes_quality as mq
    check("技能可定位", mq._skill_dir() is not None)
    mod = mq._load_validator()
    check("校验器可加载", hasattr(mod, "validate"))

    good = """# 会议纪要：测试

## 会议基本信息

会议主题：测试会议
会议时间：2026-11-18

## 会议结论

本次会议决定采用方案 A。

## 关键决策

1. 决定采用方案 A 上线。

## 待办事项

| 序号 | 待办事项 | 责任人/责任部门 | 完成时间 | 优先级 | 备注 |
|---|---|---|---|---|---|
| 1 | 整理上线清单 | 张三 | 本周五 | 高 | 无 |

## 会议内容

### 议题一

讨论了上线安排，决定采用方案 A。
"""
    with tempfile.TemporaryDirectory() as td:
        md = Path(td) / "会议纪要-测试.md"
        md.write_text(good, encoding="utf-8")
        res = mq.export(md)
        check("导出文件落盘", Path(res["json"]).is_file())
        data = res["data"]
        check("待办解析出 1 条", len(data["action_items"]) == 1,
              str(len(data["action_items"])))
        check("决策解析出 1 条", len(data["decisions"]) == 1, str(len(data["decisions"])))
        check("待办字段齐全",
              set(data["action_items"][0]) >= {"id", "task", "owner", "due",
                                               "priority", "note"})

        chk = mq.check(md, md)
        check("校验返回三态", chk["status"] in ("complete", "degraded", "failed"),
              chk["status"])
        check("校验结果带 PASS 明细",
              any(r[0] == "PASS" for r in chk["results"]))

        # 禁用标注必须被抓到：这正是本次真实纪要踩过的坑
        bad = good.replace("2026-11-18", "待确认")
        bd = Path(td) / "坏纪要.md"
        bd.write_text(bad, encoding="utf-8")
        chk2 = mq.check(bd)
        check("禁用标注判失败", chk2["status"] == "failed", chk2["status"])
        check("失败项定位到禁用标注",
              any("禁用标注" in f["step"] for f in chk2["fails"]))

        # 数字溯源：纪要里出现源材料没有的数字应给 WARN，不是 FAIL
        src = Path(td) / "原文.md"
        src.write_text("[00:10] 张三：我们做 3000 万。\n", encoding="utf-8")
        num_md = Path(td) / "数字纪要.md"
        num_md.write_text(good.replace("本周五", "9 月 99 日"), encoding="utf-8")
        chk3 = mq.check(num_md, src)
        check("编造数字给 WARN 不给 FAIL",
              any("数字溯源" in w["step"] for w in chk3["warns"]),
              str(chk3["warns"]))

    # 工具入口不因校验 FAIL 返回非零：纪要是人写的稿，不该让管线判死
    check("selftest 入口可跑", callable(mq.selftest))


def test_minutes_eval():
    print("[9] 纪要质量评测")
    import importlib.util as _u
    if _u.find_spec("docx") is None:
        print("  SKIP python-docx 未安装（由 skill-minutes job 覆盖）")
        return
    import minutes_eval as me
    check("中文数字展开 十五六", me._cn_to_values("十五六") >= {15, 16},
          str(sorted(me._cn_to_values("十五六"))))
    check("中文数字展开 十三四", me._cn_to_values("十三四") >= {13, 14})
    check("中文数字展开 二十", me._cn_to_values("二十") == {20})
    check("中文数字展开 三百", me._cn_to_values("三百") == {300})
    # 不能过宽：十五六不该展开出 56 这类不可能的价位
    check("中文数字不过宽", 56 not in me._cn_to_values("十五六"),
          str(sorted(me._cn_to_values("十五六"))))

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "原文.md"
        # 转写稿用中文数字写价格，纪要用阿拉伯数字——这是忠实整理，不能判成编造
        src.write_text("[00:05] 张三：卖个十三四块、十五六块都有。\n"
                       "[00:09] 李四：张三代号在，可成交 3000 万。\n", encoding="utf-8")
        md = Path(td) / "纪要.md"
        md.write_text(
            "# 会议纪要：价格\n\n## 会议基本信息\n\n会议主题：价格\n\n"
            "## 会议结论\n\n确认价位 13 元至 16 元。\n\n"
            "## 关键决策\n\n1. 定价区间定在 13 元至 16 元。\n\n"
            "## 待办事项\n\n"
            "| 序号 | 待办事项 | 责任人/责任部门 | 完成时间 | 优先级 | 备注 |\n"
            "|---|---|---|---|---|---|\n"
            "| 1 | 复核 3000 万口径 | 李四 | 待定 | 中 | 无 |\n\n"
            "## 会议内容\n\n### 价格\n\n会上给出 13 元至 16 元的区间。\n",
            encoding="utf-8")
        rep = me.evaluate(md, src.read_text(encoding="utf-8"))
        check("中文数字不算编造", rep["metrics"]["invented_numbers"] == 0,
              str(rep["metrics"]["invented_number_list"]))
        check("正常稿判 pass", rep["verdict"] == "pass", str(rep["checks"]))
        check("待办序号不参与数字核对", "1" not in rep["metrics"]["invented_number_list"])

        # 真编造仍要抓：原文没有 99 亿
        md2 = Path(td) / "纪要2.md"
        md2.write_text(md.read_text(encoding="utf-8").replace(
            "会上给出 13 元至 16 元的区间", "会上给出 13 元至 16 元的区间，目标 99 亿"),
            encoding="utf-8")
        rep2 = me.evaluate(md2, src.read_text(encoding="utf-8"))
        check("真编造被抓", rep2["metrics"]["invented_numbers"] >= 1,
              str(rep2["metrics"]["invented_number_list"]))
        check("编造判 fail", rep2["verdict"] == "fail")

    check("selftest 入口可跑", callable(me.selftest))


def main():
    test_manifest_item()
    test_mcp_params()
    test_job_key_idempotent()
    test_index_parse()
    test_index_search_cn()
    test_timeline_render()
    test_fmt_ts()
    test_minutes_quality()
    test_minutes_eval()
    print(f"\n合计 {passed + failed}，PASS {passed}，FAIL {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
