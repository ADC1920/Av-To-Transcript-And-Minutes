#!/usr/bin/env python3
"""test_mcp_e2e.py — MCP 服务端到端握手测试（真走 stdio 协议，不是模拟调用）。

与 test_tools_units.py 的分工：
    test_tools_units.py 直接调工具函数，验证内部逻辑；
    本文件起真实 MCP 子进程、走 JSON-RPC 握手，验证协议层契约——
    工具能否被发现、参数 schema 是否被客户端接受、结构化结果是否按 outputSchema 返回、
    资源模板能否读取、错误是否以协议正确的方式传出。

跑法：python tests/test_mcp_e2e.py
依赖：mcp SDK（uv sync --extra mcp）；缺依赖时整体 SKIP 而非 FAIL。
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "tools" / "mcp_server.py"

passed = failed = skipped = 0


def attr(obj, *names):
    """跨 SDK 版本取属性。

    1.x 的 pydantic 模型用 camelCase 字段名（serverInfo / inputSchema / uriTemplate），
    2.x 全改成 snake_case（server_info / input_schema / uri_template）。
    这里按给定顺序试，都不存在时抛 AttributeError 让调用方看见。
    """
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    raise AttributeError(f"{type(obj).__name__} 没有这些属性：{names}")


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name}" + (f"  <<{detail}>>" if detail else ""))


async def run_session():
    # SDK 1.x 与 2.x 的客户端入口不同：2.x 把 streamablehttp/stdio 客户端挪了位置，
    # 但 stdio_client 在 2.x 仍在 mcp.client.stdio（迁移指南未列改名）。
    # 先按 1.x 路径导，失败再试 2.x 的候选位置。
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError:
        from mcp.client import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)],
                                   cwd=str(ROOT))

    with tempfile.TemporaryDirectory() as td:
        jobs = Path(td) / "jobs"
        out = Path(td) / "out"
        (out / "demo").mkdir(parents=True)
        lines = [{"spk": 1, "spk_name": "甲", "start_ms": i * 1000,
                  "end_ms": i * 1000 + 900, "text": f"第{i}句内容"} for i in range(30)]
        (out / "demo" / "转写结果-demo.json").write_text(
            json.dumps({"lines": lines, "speakers": ["甲"]}, ensure_ascii=False),
            encoding="utf-8")
        (out / "demo" / "转写文稿-demo.md").write_text("# 演示稿\n\n第0句内容\n", encoding="utf-8")
        jdir = jobs / "jobdemo"
        jdir.mkdir(parents=True)
        (jdir / "job.json").write_text(json.dumps({
            "job_id": "jobdemo", "status": "completed", "source": str(out / "demo" / "demo.wav"),
            "output": str(out), "created_at": "2026-11-26 10:00:00",
            "finished_at": "2026-11-26 10:01:00", "exit_code": 0,
        }, ensure_ascii=False), encoding="utf-8")

        env = dict(os.environ)
        env["AV2M_JOBS"] = str(jobs)
        params_with_env = StdioServerParameters(
            command=sys.executable, args=[str(SERVER)], cwd=str(ROOT), env=env)

        async with stdio_client(params_with_env) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                check("握手成功", attr(init, "serverInfo", "server_info").name == "av-to-minutes",
                      attr(init, "serverInfo", "server_info").name)

                # 1. 工具发现
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                check("发现 10 个工具", len(tools.tools) == 10, str(len(tools.tools)))
                check("纪要工具可见",
                      {"export_minutes", "eval_minutes"} <= names, str(sorted(names)))

                # 2. 客户端视角看 schema：每个工具都要有 outputSchema，参数要是对象
                no_out = [t.name for t in tools.tools if not attr(t, "outputSchema", "output_schema")]
                check("客户端看到 outputSchema", not no_out, str(no_out))
                no_ann = [t.name for t in tools.tools if not t.annotations]
                check("客户端看到 annotations", not no_ann, str(no_ann))
                ts = next(t for t in tools.tools if t.name == "transcribe_start")
                check("客户端看到 options 具名字段",
                      "TranscribeOptions" in json.dumps(attr(ts, "inputSchema", "input_schema")), "")

                # 3. 真实调用只读工具
                r = await session.call_tool("transcribe_list", {"limit": 5})
                payload = json.loads(r.content[0].text)
                check("transcribe_list 可调用", payload.get("count") == 1, str(payload))
                check("列表含预置任务", payload["jobs"][0]["job_id"] == "jobdemo")

                # 4. 分页语义经协议验证
                r2 = await session.call_tool("transcribe_result",
                                             {"job_id": "jobdemo", "offset": 0, "limit": 10})
                p2 = json.loads(r2.content[0].text)
                check("分页首页 has_more", p2.get("has_more") is True, str(p2.get("has_more")))
                check("分页首页 next_offset", p2.get("next_offset") == 10, str(p2.get("next_offset")))
                check("分页提示不可截断", "未拉完" in p2.get("note", "") or
                      "还有" in p2.get("note", ""), p2.get("note", ""))
                check("结构化结果同时有 structuredContent 或文本",
                      bool(attr(r2, "structuredContent", "structured_content")) or bool(r2.content), "")

                r3 = await session.call_tool("transcribe_result",
                                             {"job_id": "jobdemo", "offset": 20, "limit": 10})
                p3 = json.loads(r3.content[0].text)
                check("分页末页 has_more 为假", p3.get("has_more") is False, str(p3.get("has_more")))
                check("末尾提示可总结", "可以基于" in p3.get("note", ""), p3.get("note", ""))

                # 5. 状态查询（含 wait_seconds 参数被接受）
                r4 = await session.call_tool("transcribe_status",
                                             {"job_id": "jobdemo", "wait_seconds": 5})
                p4 = json.loads(r4.content[0].text)
                check("状态查询返回 completed", p4.get("status") == "completed", str(p4))
                check("状态回报等待时长", "waited_seconds" in p4, str(p4.keys()))

                # 6. 错误路径：未知任务应以协议错误传出，而不是返回空壳
                err = None
                try:
                    e = await session.call_tool("transcribe_status", {"job_id": "does-not-exist"})
                    err = "no-raise"
                    if attr(e, "isError", "is_error"):
                        err = "isError"
                except Exception as ex:
                    err = type(ex).__name__
                check("未知任务以错误传出", err != "no-raise", str(err))

                # 7. 资源模板
                resources = await session.list_resource_templates()
                tpls = attr(resources, "resourceTemplates", "resource_templates")
                uris = " ".join(str(attr(t, "uriTemplate", "uri_template")) for t in tpls)
                check("三个资源模板可见", len(tpls) == 3, str(len(tpls)))
                check("模板含 transcript", "transcript" in uris, uris)

                # 8. 真读一个资源
                try:
                    rr = await session.read_resource("meeting://demo/transcript")
                    text = rr.contents[0].text if rr.contents else ""
                    check("资源可读取", "演示稿" in text, text[:60])
                except Exception as ex:
                    check("资源可读取", False, f"{type(ex).__name__}: {ex}")

                # 9. 提示能力（FastMCP 未注册 prompt 时不应报错）
                try:
                    await session.list_prompts()
                    check("list_prompts 不报错", True)
                except Exception as ex:
                    check("list_prompts 不报错", False, str(ex))


def main():
    try:
        import mcp  # noqa: F401
    except ImportError:
        print("SKIP：未安装 mcp SDK（uv sync --extra mcp）")
        return 0
    from mcp import ClientSession  # noqa: F401

    print("[E2E] MCP stdio 握手与工具调用")
    # 环境自检：SDK 与 pydantic 任一缺失都直接报出来，别让它以
    # 「TaskGroup 1 sub-exception」这种不可读形态出现（CI 首次踩过）。
    try:
        import importlib.metadata as md
        print(f"  env: mcp={md.version('mcp')} pydantic={md.version('pydantic')} "
              f"python={sys.version.split()[0]}")
    except Exception as e:
        print(f"  env: 版本读取失败 {type(e).__name__}: {e}")
    try:
        asyncio.run(asyncio.wait_for(run_session(), timeout=120))
    except asyncio.TimeoutError:
        check("整体不超时", False, "120s")
    except SystemExit as e:
        # build_server 缺 SDK 时抛 SystemExit，必须单独接住并报成可读失败
        check("服务端可启动", False, f"SystemExit: {e}")
    except Exception as e:
        # asyncio 的 TaskGroup 会把子异常包成 ExceptionGroup（3.11+）。
        # 只打 type+str 会得到一句「unhandled errors」，看不到根因——
        # 用 traceback 展开子异常，CI 上直接能读出是版本不兼容还是握手失败。
        import traceback
        detail = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        subs = getattr(e, "exceptions", None)
        if subs:
            for sub in subs:
                detail += "".join(traceback.format_exception(
                    type(sub), sub, sub.__traceback__))
        check("会话正常结束", False, detail[-700:].replace("\n", " ⏎ "))

    print(f"\n合计 {passed + failed}，PASS {passed}，FAIL {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
