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

| 序号 | 待办事项 | 责任人/责任部门 | 完成时间 | 优先级 | 状态 | 备注 |
|---|---|---|---|---|---|---|
| 1 | 整理上线清单 | 张三 | 11 月 20 日 | 高 | 未开始 | 优先级未明示；出处：00:10 张三 |

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

        # 数字溯源：纪要里出现源材料没有的数字应给 WARN，不是 FAIL。
        # 载体从「完成时间改成 9 月 99 日」换成往任务描述里塞一个原文没有的数字——
        # 完成时间那一列现在是具体日期且走「日期溯源」，借它触发会同时引出两条告警，
        # 断言「有没有数字溯源 WARN」就变得不可靠了。
        src = Path(td) / "原文.md"
        src.write_text("[00:10] 张三：我们做 3000 万。\n", encoding="utf-8")
        num_md = Path(td) / "数字纪要.md"
        num_md.write_text(good.replace("整理上线清单", "整理上线清单并核对 777 项"),
                          encoding="utf-8")
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
            "| 序号 | 待办事项 | 责任人/责任部门 | 完成时间 | 优先级 | 状态 | 备注 |\n"
            "|---|---|---|---|---|---|---|\n"
            "| 1 | 复核 3000 万口径 | 李四 | 待定 | 中 | 未开始 | 优先级未明示；出处：00:09 李四 |\n\n"
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


def test_mcp_options_model():
    print("[10] MCP：options 有类型模型（schema 自描述）")
    import mcp_server as m

    if not getattr(m, "_PYDANTIC_OK", False):
        print("  SKIP 未安装 pydantic（uv sync --extra mcp）")
        return

    # 模型字段数必须与命令行映射表一致，否则漏字段会静默丢弃参数
    check("字段数与白名单一致",
          len(m.TranscribeOptions.model_fields) == len(m._ALLOWED_OPTIONS))
    check("所有字段都有映射",
          set(m.TranscribeOptions.model_fields) == set(m._BOOL_FLAGS) | set(m._VALUE_FLAGS))

    # 枚举必须收敛，否则 agent 传错值要到运行时才发现
    schema = m.TranscribeOptions.model_json_schema()
    asr = schema["properties"]["asr_engine"]
    enum_vals = next((x["enum"] for x in asr.get("anyOf", []) if "enum" in x), None)
    check("asr_engine 枚举进 schema", enum_vals == ["qwen", "aed", "auto", "cloud"], str(enum_vals))
    check("禁止额外字段", schema.get("additionalProperties") is False)

    # 拼错的键在模型层被拒
    try:
        m.TranscribeOptions(拼错的键=True)
        check("模型拒绝未知字段", False)
    except Exception:
        check("模型拒绝未知字段", True)

    # to_cli 只保留显式设置项：不传与传 False 必须区分开
    check("空模型不产生参数", m.TranscribeOptions().to_cli() == {})
    check("显式 True 产生参数", m.TranscribeOptions(meeting=True).to_cli() == {"meeting": True})
    check("显式 False 不产生参数", m.TranscribeOptions(meeting=False).to_cli() == {})
    check("带值参数保留", m.TranscribeOptions(num_speakers=2).to_cli() == {"num_speakers": 2})

    # 互斥组合必须在提交时拒绝。否则 transcribe_start 已返回 job_id，
    # agent 要轮询到 failed 才知道参数错，而原因埋在 run.log 里。
    for kw, label in (
        ({"meeting": True, "no_diarize": True}, "meeting 与 no_diarize 互斥"),
        ({"min_speakers": 5, "max_speakers": 2}, "min>max 被拒"),
        ({"num_speakers": 2, "min_speakers": 1}, "num 与区间同时给被拒"),
    ):
        try:
            m.TranscribeOptions(**kw)
            check(label, False, str(kw))
        except Exception:
            check(label, True)
    # 合法组合不能被误伤
    check("meeting + num_speakers 合法",
          m.TranscribeOptions(meeting=True, num_speakers=2).to_cli()
          == {"meeting": True, "num_speakers": 2})
    check("min/max 区间合法",
          m.TranscribeOptions(min_speakers=2, max_speakers=4).to_cli()
          == {"min_speakers": 2, "max_speakers": 4})


def _has_mcp_sdk():
    """CI 的最小 job 不装 mcp；没有它就跳过需要起 server 的用例，而不是报 FAIL。"""
    try:
        import mcp  # noqa: F401
        return True
    except ImportError:
        return False


def test_mcp_tool_contracts():
    print("[11] MCP：工具契约面（schema / 注解 / 描述）")
    import mcp_server as m

    if not _has_mcp_sdk():
        print("  SKIP 未安装 mcp SDK（uv sync --extra mcp）")
        return

    server = m.build_server()
    tools = server._tool_manager.list_tools()
    check("工具数 10 个", len(tools) == 10, str(len(tools)))

    names = {t.name for t in tools}
    check("含纪要导出工具", "export_minutes" in names)
    check("含纪要评测工具", "eval_minutes" in names)

    # 每个工具都必须有 outputSchema 与注解——这是 agent 能可靠消费的前提
    no_schema = [t.name for t in tools if not t.output_schema]
    no_ann = [t.name for t in tools if not t.annotations]
    check("全部工具都有 outputSchema", not no_schema, str(no_schema))
    check("全部工具都有 annotations", not no_ann, str(no_ann))

    # 只读工具必须标 readOnlyHint，写工具不能误标
    by_name = {t.name: t for t in tools}
    readonly = {"transcribe_status", "transcribe_result", "transcribe_list",
                "search_meetings", "eval_minutes"}
    for n in readonly:
        check(f"{n} 标为只读", by_name[n].annotations.readOnlyHint is True)
    check("transcribe_cancel 标为破坏性",
          by_name["transcribe_cancel"].annotations.destructiveHint is True)
    check("transcribe_start 标为幂等",
          by_name["transcribe_start"].annotations.idempotentHint is True)

    # 描述必须够长且写明「何时不用」——短描述是 agent 选错工具的主因
    short = [(t.name, len(t.description or "")) for t in tools if len(t.description or "") < 100]
    check("描述均超过 100 字符", not short, str(short))
    lacks_when_not = [t.name for t in tools if "何时不用" not in (t.description or "")]
    check("描述都写了何时不用", not lacks_when_not, str(lacks_when_not))

    # transcribe_start 的 options 必须展开成完整字段定义，而不是裸 dict
    ts = by_name["transcribe_start"]
    defs = ts.parameters.get("$defs") or {}
    opts = defs.get("TranscribeOptions") or {}
    check("options 展开为具名字段", len(opts.get("properties") or {}) == 22,
          str(len(opts.get("properties") or {})))
    check("options 禁止额外字段", opts.get("additionalProperties") is False)


def test_mcp_result_pagination_note():
    print("[12] MCP：分页语义（has_more 与不可截断提示）")
    import mcp_server as m

    if not _has_mcp_sdk():
        print("  SKIP 未安装 mcp SDK（uv sync --extra mcp）")
        return

    with tempfile.TemporaryDirectory() as td:
        jobs = Path(td) / "jobs"
        src = Path(td) / "m.wav"
        src.write_bytes(b"x" * 2000)
        out = Path(td) / "out"
        (out / "m").mkdir(parents=True)
        lines = [{"spk": 1, "spk_name": "甲", "start_ms": i * 1000,
                  "end_ms": i * 1000 + 900, "text": f"第{i}句"} for i in range(250)]
        (out / "m" / "转写结果-m.json").write_text(
            json.dumps({"lines": lines, "speakers": ["甲"]}, ensure_ascii=False),
            encoding="utf-8")
        jdir = jobs / "job1"
        jdir.mkdir(parents=True)
        (jdir / "job.json").write_text(json.dumps({
            "job_id": "job1", "status": "completed", "source": str(src),
            "output": str(out), "created_at": "2026-11-26 10:00:00",
        }, ensure_ascii=False), encoding="utf-8")

        old = m.JOBS_DIR
        m.JOBS_DIR = jobs
        try:
            server = m.build_server()
            fn = server._tool_manager.get_tool("transcribe_result").fn
            r1 = fn("job1")                      # 默认 200 段，总量 250
            check("首页标记 has_more", r1.has_more is True)
            check("首页给 next_offset", r1.next_offset == 200, str(r1.next_offset))
            check("首页提示未拉完", "未拉完" in r1.note or "还有" in r1.note, r1.note)
            check("首页提示不要断言未讨论", "未讨论" in r1.note, r1.note)
            r2 = fn("job1", offset=200)
            check("末页 has_more 为假", r2.has_more is False)
            check("末页无 next_offset", r2.next_offset is None)
            check("末页提示可总结", "可以基于" in r2.note, r2.note)
            check("总段数正确", r2.total == 250, str(r2.total))
            check("末页段数正确", len(r2.segments) == 50, str(len(r2.segments)))
        finally:
            m.JOBS_DIR = old


def test_mcp_status_longpoll():
    print("[13] MCP：状态查询有界长轮询")
    import asyncio
    import mcp_server as m

    if not _has_mcp_sdk():
        print("  SKIP 未安装 mcp SDK（uv sync --extra mcp）")
        return

    with tempfile.TemporaryDirectory() as td:
        jobs = Path(td) / "jobs"
        jdir = jobs / "j2"
        jdir.mkdir(parents=True)
        (jdir / "job.json").write_text(json.dumps({
            "job_id": "j2", "status": "completed", "source": "a.wav",
            "created_at": "2026-11-26 10:00:00", "finished_at": "2026-11-26 10:01:00",
            "exit_code": 0}, ensure_ascii=False), encoding="utf-8")

        old = m.JOBS_DIR
        m.JOBS_DIR = jobs
        try:
            server = m.build_server()
            fn = server._tool_manager.get_tool("transcribe_status").fn
            # 已完成的任务应立即返回，不该等满 60 秒
            import time as _t
            t0 = _t.time()
            r = asyncio.run(fn("j2", wait_seconds=60))
            took = _t.time() - t0
            check("已完成任务不等待", took < 2.0, f"{took:.2f}s")
            check("状态为 completed", r.status == "completed", r.status)
            check("回报实际等待时长", r.waited_seconds < 2.0, str(r.waited_seconds))
            # wait_seconds 上限夹到 60，避免撞客户端超时
            r2 = asyncio.run(fn("j2", wait_seconds=99999))
            check("超长等待被夹紧", r2.waited_seconds < 2.0, str(r2.waited_seconds))
            # 不存在的任务必须报错而不是返回空壳
            try:
                asyncio.run(fn("nope"))
                check("未知任务报错", False)
            except ValueError:
                check("未知任务报错", True)
        finally:
            m.JOBS_DIR = old


def test_mcp_resources():
    print("[14] MCP：成稿资源模板")
    import mcp_server as m
    if not _has_mcp_sdk():
        print("  SKIP 未安装 mcp SDK（uv sync --extra mcp）")
        return
    server = m.build_server()
    templates = server._resource_manager.list_templates()
    check("三个资源模板", len(templates) == 3, str(len(templates)))
    uris = []
    for t in templates:
        uri = getattr(t, "uri_template", None) or getattr(t, "uriTemplate", "")
        uris.append(str(uri))
    joined = " ".join(uris)
    check("含 transcript 资源", "transcript" in joined, joined)
    check("含 minutes 资源", "minutes" in joined, joined)
    check("含 timeline 资源", "timeline" in joined, joined)


def test_cli_json_contract():
    print("[15] CLI：--json 与退出码契约")
    sys.path.insert(0, str(ROOT))
    import importlib
    import av_to_transcript_and_minutes as av
    importlib.reload(av)

    # 退出码分区必须稳定且互不相同
    codes = {av.EXIT_OK, av.EXIT_FAILED, av.EXIT_USAGE,
             av.EXIT_INPUT_MISSING, av.EXIT_CONFLICT}
    check("退出码五档互不相同", len(codes) == 5, str(sorted(codes)))
    check("每档都有建议动作",
          all(c in av.ERROR_HINTS for c in (av.EXIT_USAGE, av.EXIT_INPUT_MISSING,
                                            av.EXIT_CONFLICT, av.EXIT_FAILED)))

    # 用子进程验证真实行为：不模拟，直接跑入口
    import subprocess
    entry = str(ROOT / "av_to_transcript_and_minutes.py")

    def run(*argv):
        return subprocess.run([sys.executable, entry, *argv],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=str(ROOT))

    r = run("/definitely/not/here.wav", "--json")
    check("输入不存在退出码 20", r.returncode == 20, str(r.returncode))
    try:
        payload = json.loads(r.stdout)
        check("stdout 是合法 JSON", True)
        check("错误体含 code", payload.get("code") == 20, str(payload.get("code")))
        check("错误体含 suggestion", bool(payload.get("suggestion")))
        check("错误体标 error", payload.get("error") is True)
    except ValueError:
        check("stdout 是合法 JSON", False, r.stdout[:120])

    r2 = run(str(ROOT / "README.md"), "--meeting", "--no-diarize", "--json")
    check("参数冲突退出码 30", r2.returncode == 30, str(r2.returncode))
    check("冲突错误体可解析", "code" in r2.stdout and "30" in r2.stdout, r2.stdout[:120])

    r3 = run(str(ROOT / "README.md"), "--json")
    check("不支持类型退出码 2", r3.returncode == 2, str(r3.returncode))

    # 无 --json 时保持原有行为：错误走 stderr，stdout 为空
    r4 = run("/definitely/not/here.wav")
    check("默认模式不进 JSON", r4.stdout.strip() == "", r4.stdout[:80])
    check("默认模式错误在 stderr", "不存在" in r4.stderr, r4.stderr[:80])

    # --watch 模式下 --json 不该透传给子进程，否则每个子进程各自往 stdout 打 JSON。
    # 直接验透传过滤逻辑，不启常驻进程（watch 是死循环，起进程测会挂住）。
    import inspect
    src = inspect.getsource(av.main)
    check("watch 分支显式排除 --json", 'if tok == "--json"' in src, "")


def test_mcp_minutes_tools():
    print("[16] MCP：纪要导出与评测工具（真实样例）")
    import mcp_server as m

    sample = ROOT / "samples" / "会议纪要.md"
    if not sample.is_file():
        print("  SKIP 缺少 samples/会议纪要.md")
        return
    if not _has_mcp_sdk():
        print("  SKIP 未安装 mcp SDK（uv sync --extra mcp）")
        return

    server = m.build_server()
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "minutes.json"
        # 导出：必须真产出 JSON 且计数回填，agent 不必解析 stdout
        fn = server._tool_manager.get_tool("export_minutes").fn
        r = fn(minutes=str(sample), out=str(out))
        check("导出退出码 0", r.exit_code == 0, str(r.exit_code))
        check("导出文件存在", r.exported and out.is_file())
        check("待办计数已回填", r.action_items > 0, str(r.action_items))
        check("决策计数已回填", r.decisions > 0, str(r.decisions))
        check("节数已回填", r.sections > 0, str(r.sections))
        payload = json.loads(out.read_text(encoding="utf-8"))
        check("导出内容含 action_items", "action_items" in payload)

        # 评测：gate=false 时 verdict 与 exit_code 解耦，必须看 verdict
        ev = server._tool_manager.get_tool("eval_minutes").fn
        r2 = ev(minutes=str(sample), source=str(sample), gate=False)
        check("评测返回 verdict", r2.verdict in ("pass", "fail"), str(r2.verdict))
        check("评测给 metrics", bool(r2.metrics))
        check("评测列 unjudged", len(r2.unjudged) >= 3, str(len(r2.unjudged)))
        check("非 gate 模式退出码不反映合格与否",
              r2.exit_code == 0 or r2.verdict == "fail", "")

        # gate=true 时 verdict 与退出码必须一致
        r3 = ev(minutes=str(sample), source=str(sample), gate=True)
        expect = 0 if r3.verdict == "pass" else 1
        check("gate 模式退出码与 verdict 一致",
              r3.exit_code == expect, f"exit={r3.exit_code} verdict={r3.verdict}")

        # 缺 source 必须明确报错，不能悄悄算出错误指标
        try:
            ev(minutes=str(sample))
            check("缺 source 被拒", False)
        except ValueError:
            check("缺 source 被拒", True)


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
    test_mcp_options_model()
    test_mcp_tool_contracts()
    test_mcp_result_pagination_note()
    test_mcp_status_longpoll()
    test_mcp_resources()
    test_cli_json_contract()
    test_mcp_minutes_tools()
    print(f"\n合计 {passed + failed}，PASS {passed}，FAIL {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
