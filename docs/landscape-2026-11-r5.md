# 2026-11 第五轮调研：agent 调用面

> 目的：前四轮分别解决识别引擎（r1/r2）、纪要质量闭环（r3）、输出范式字段层（r4）。
> 本轮换维度，只回答一个问题——**这个项目要怎么改，才更好被 agent 调用**。
> 方法：GitHub 同域 MCP 实现直读 + 三大厂工具调用官方文档 + 回本项目逐项实测（MCP 握手套件实测 8 个工具的真实 schema）。
> 收录标准：每条结论指到外部仓库文件或本项目代码行/实测输出；拿不到证据的标「未核实」。

## 一、结论先行

1. **本项目在「任务模型」上已经领先同域绝大多数项目。** `transcribe_start` 立即返 `job_id`、
   状态落盘可跨进程查询、幂等键用内容哈希——这三条是对的，同域里只有 `AudioSmith`、
   `whisper-windows-mcp` 做到了同等程度，官方 `FunASR` 示例仍是单工具阻塞。
2. **真正的短板是「工具契约面」，不是任务模型。** 8 个工具全部 `annotations=None`、
   无 `outputSchema`、返回值是 `json.dumps` 出来的字符串。SDK 1.29.0 这四项能力全都支持
   （`structured_output`/`annotations`/`report_progress`/资源），本项目一个都没用。
3. **最大的单点障碍是 `transcribe_start` 的 `options` 参数。** 它在 schema 里是
   `additionalProperties: true` 的裸 dict，22 个白名单键只写在 docstring 里。agent 从 schema
   完全看不出能传什么、什么类型、什么取值——只能读长文本猜，这正是官方文档反复警告的形态。
4. **链路断在转写，没接到纪要。** `tools/minutes_quality.py`（结构化导出 + 校验）与
   `tools/minutes_eval.py`（质量门禁）都没进 MCP 工具面。agent 想从「给个音频」走到
   「拿到可验收的纪要」，中途必须自己拼命令行。
5. **一个反直觉的结论：工具数量不是本项目的问题。** 8 个工具远低于 OpenAI 建议的 20 与
   Google 建议的 10-20，`github-mcp-server` 从 101 收到 52 才解决问题、`playwright-mcp`
   72 个按能力分组默认只开 24 个——这些教训在本项目不适用。**不要为了「学大厂」去收窄工具面**，
   该做的是把缺的工具补上（纪要侧），补到 11-12 个仍在安全区。

## 二、同域项目横向对照

| 仓库 | 规模 | 工具数 | 任务模型 | 大结果处理 | 本项目现状 |
|---|---|---|---|---|---|
| `modelscope/FunASR` 的 `examples/mcp_server` | 21.1k★ | 1 | 一次性阻塞 | 全文拼进 result | 本项目更好 |
| `Vexa-ai/vexa` | 3k★ | 18 内置 | 无长任务，游标分页 | `since_index`/`limit`/`has_more`/`next_index` | 本项目有 job，无 `has_more` 语义 |
| `silverstein/minutes` | 1.6k★ | ~32 | 同步阻塞 + 资源订阅 | 256KB 硬截断 + 9 个 resource | 本项目两者都无 |
| `nexmoe/DayBee` | 431★ | 3 | 纯只读检索 | offset/limit(≤100)/nextOffset | 可借鉴资源化 |
| `hehehai/AudioSmith` | 264★ | 数十 | `job_id` + `wait_seconds`≤60 长轮询 | 返回文件绝对路径 | 本项目缺长轮询 |
| `arcaputo3/mcp-server-whisper` | 61★ | 8 | 一次性阻塞 | Pydantic 对象整段返回 | 本项目 Pydantic 也没用 |
| `eviscerations/whisper-windows-mcp` | 2★ | 13 | 阻塞/后台双模 | 写文件 + 日志末行 | 已具备 |
| `fan200617120-ui/WhisperX-Workstation` | 10★ | 4 | 一次性阻塞 | 写 `output/` 返回路径 | 已具备 |

规模为 2026-11-26 GitHub API 单次快照。

三点值得单独记的实测：

**2.1 Vexa 的分页注释记录了一场真实事故。** `get_meeting_transcript` 的返回里，当
`has_more` 为真时附一句硬话，大意是「再次调用直到 has_more 为 false 才能总结，绝不能从一部分
就断言某事未讨论」。源码注释写明事故经过：538 段转写一次性返回被调用方截断，写出的纪要只覆盖
前半程，还对后半程断言「未讨论」。**本项目风险相同**：`transcribe_result` 有 `offset`/`limit`
分页（默认 200 段），但返回里只有 `next_offset`，没有任何一句告诉 agent「还有下一页就还没完」。
一句话素材两小时会议能到上千段，agent 拿第一页就去总结是最可能的失效路径。

**2.2 AudioSmith 把超时治理写进了连接期指令。** `Instructions` 直接教 agent「长任务返回
`job_id`，用 `get_job` 的 `wait_seconds`（≤60）轮询到完成」，`get_job` 内部把等待夹到
`min(max(wait,0),60)` 秒。`whisper-windows-mcp` 走另一条路：`FOREGROUND_MAX_SEC=210`，
估算超时就拒绝前台并转 detached job，注释点明 Claude Desktop 约 240 秒的工具调用天花板。
本项目 `transcribe_status` 返回即走、无等待，agent 只能自己循环空转。

**2.3 只有 WhisperX-Workstation 真正发 progress。** 它从 `params._meta.progressToken`
取 token，一路把 `on_progress` 回调下沉到转写 pipeline。其余项目（含 `silverstein/minutes`、
`Vexa`、`DayBee`）都没用。说明这条**在转写域是空白**，本项目补上就是差异化。

## 三、agent 调用面的通用范式（三大厂一手文档）

**3.1 工具描述要 3-4 句，必须写「何时不用」。** Anthropic 的 `define-tools` 文档明确要求描述
涵盖「做什么、何时用、何时不用、参数含义、局限」。本项目实测（MCP 握手套件读 `description` 长度）：

| 工具 | 描述长度 |
|---|---|
| `transcribe_start` | 383 字符 |
| `search_meetings` | 67 |
| `transcribe_status` | 57 |
| `transcribe_result` | 36 |
| `index_meetings` | 34 |
| `render_timeline` | 28 |
| `transcribe_cancel` | 25 |
| `transcribe_list` | **8** |

除 `transcribe_start` 外全部低于 70 字符，`transcribe_list` 只有「最近的转写任务。」七个字。
agent 无法从中判断「什么时候该用它、和 `transcribe_status` 什么区别、会不会污染上下文」。

**3.2 `strict: true` + `additionalProperties: false` 是标配，本项目正相反。**
OpenAI 的 function-calling 文档把这两项写进示例标配，并给了反面例子
`toggle_light(on: bool, off: bool)`——允许非法状态组合。本项目实测 `transcribe_start`
的参数 schema：

```json
{"properties": {"path": {"type": "string"},
                "options": {"anyOf": [{"additionalProperties": true, "type": "object"},
                                      {"type": "null"}], "default": null}},
 "required": ["path"]}
```

`options` 是自由 dict。22 个白名单键（`meeting`/`srt`/`rttm`/`asr_engine`/`diarize_engine`…）
在 schema 里一个都看不见，`build_cmd` 的拒绝逻辑只在运行时才生效。**agent 只能靠读 docstring
的一长串中文枚举来拼参数，拼错了要等调用失败才知道。** 这是本轮最该改的一处。

**3.3 结构化输出是规范级契约，本项目只返回字符串。** MCP 规范里 `outputSchema` 一旦声明，
服务端 MUST 返回符合它的 `structuredContent`，客户端 SHOULD 校验；规范还要求同时回一份
序列化文本以兼容旧客户端。FastMCP 1.29.0 的 `@mcp.tool(structured_output=True)` 配合类型注解
即可自动生成。本项目实测 8 个工具全部 `output_schema=None`，返回 `json.dumps(...)` 字符串——
**agent 拿到的是文本，不是结构，无法做 schema 校验也无法依赖字段名稳定**。

**3.4 大结果不该塞进 tool result。** Google 文档明确要求大文件「返回 `uri` 而不是内联
`data`」；Anthropic 把 Claude Code 的工具响应上限写死 25000 token 并建议
「pagination、range selection、filtering、truncation with sensible defaults」。三条可选路径：
`resource_link`（推荐给需反复读的产物）、写文件返回路径（`playwright-mcp` 的
`browser_snapshot(filename=...)`）、分页截断（filesystem 的 `head`/`tail`）。

**3.5 长任务：异步优先 + 同步超时自动降级。** `speechweave-mcp` 的做法最省事——sync 工具带
`timeout_ms`，超时就把 `job_id` 与已完成部分一起返回并提示改用轮询。Claude Code 侧一手数值：
`MCP_TIMEOUT` 默认 30 秒（启动）、`MCP_TOOL_TIMEOUT` 默认约 28 小时；但 HTTP/SSE 传输每请求
另有 60 秒首字节计时器（取 60s / server timeout / `MCP_TIMEOUT` 三者最大）。stdio 与
WebSocket 无此计时器。另有 idle timeout：网络 server 5 分钟、stdio 30 分钟无响应即中止，
**progress notification 会续命**。

## 四、本项目实测现状

MCP 握手套件真跑（`mcp` 1.29.0，8 个工具全部注册成功）：

| 项 | 实测结果 | 代码位置 |
|---|---|---|
| 工具数 | 8 | `tools/mcp_server.py:216-363` |
| `annotations` | 8 个全 `None` | 同上 |
| `outputSchema` | 8 个全无 | 同上 |
| 返回值形态 | `json.dumps(...)` 字符串 | 全部 `return` 语句 |
| `options` 参数 | `additionalProperties: true` 裸 dict | `mcp_server.py:224` |
| 进度上报 | 无 | 全文无 `report_progress` |
| MCP resources | 无 | 全文无 `@mcp.resource` |
| 纪要工具 | 未暴露 | `minutes_quality`/`minutes_eval` 零引用 |

SDK 能力核实（`mcp` 1.29.0 + `pydantic` 2.13.4，本机实测）：

```
FastMCP.tool 签名: (name, title, description, annotations, icons, meta, structured_output)
Context.report_progress(progress: float, total: float | None, message: str | None)
ToolAnnotations 字段: title, readOnlyHint, destructiveHint, idempotentHint, openWorldHint
```

**四项能力全部可用，一项都没用。** 升级不需要引入任何新依赖。

CLI 侧（`av_to_transcript_and_minutes.py`）：

| 项 | 现状 |
|---|---|
| `--json` 输出 | 无，全程中文 `print` |
| 退出码 | 只有 0 / 1（`return 1 if (fail or invalid) else 0`） |
| 错误形态 | `sys.exit(f"输入不存在：{src}")` 等中文文本 |
| 机器可读产物 | `manifest.json` 已有，结构完整（`schema_version`/`summary`/`items`/`artifacts`） |
| 产物命名 | 可预测（`会议原文-<stem>.md` / `转写文稿-<stem>.md`），agent 能直接算路径 |

**`manifest.json` 是本项目已经做对的一件事**，同域项目里没有见到同等程度的运行清单。
缺口在于它只在批量运行结束时写，MCP 的 `transcribe_status` 读的是 `job.json` 而非它。

## 五、改进项

### P0：改完立刻见效，且都是「已有能力没接上」

**1. 把 `options` 从裸 dict 改成有类型的模型**

22 个白名单键变成 Pydantic 模型的显式字段，枚举值用 `Literal` 收敛（`diarize_engine` 只有
`campp|pyannote|auto` 三值，`asr_engine` 只有 `qwen|aed|auto|cloud` 四值——现在全靠 docstring
中文枚举）。收益：schema 直接告诉 agent 每个参数的类型与合法值，拼错在 schema 校验阶段就失败，
不必等 `build_cmd` 运行时报错。这一步同时消掉 `_ALLOWED_OPTIONS` 与 `flag_map`/`value_map`
三处手工维护的漂移风险（三张表目前必须人工保持一致）。

外部共证：OpenAI `strict:true`+`additionalProperties:false`、FastMCP 类型注解自动生成 schema。

**2. 纪要侧补两个工具**

`export_minutes(path, source)` 与 `eval_minutes(path, source, gate)`，内部直接调用
`tools/minutes_quality.py` 与 `tools/minutes_eval.py` 的现有函数（两者都已有可复用的
`export()`/`check()`/`evaluate()` 接口，不需要重写）。工具数从 8 到 11，仍在安全区。

外部共证：同域项目没有一个覆盖「转写之后」的链路（`silverstein/minutes` 有 32 个工具但它是
完整产品不是管线）；本项目的 `minutes_eval.py` 已有 `invented_numbers`/`source_coverage`/
`status_filled` 五道门禁，是本项目相对同域真正的差异化能力，藏在 CLI 后面没人能用。

**3. `transcribe_result` 补「还有下一页就不是全文」的硬提示**

在返回里加 `has_more`（现有 `next_offset is not None` 即可推出）与一句 `note`，措辞照 Vexa
那套：还有下一页时明确要求继续拉取、未拉完不得总结、不得对未覆盖部分断言「未讨论」。

外部共证：Vexa 的 538 段截断事故（`core/gateway/services/mcp/src/vexa_mcp/app.py:875-987`）。

### P1：工具契约正规化

**4. 补 `outputSchema` 与 `annotations`**

用 Pydantic 模型做单一事实源，`@mcp.tool(structured_output=True)` 自动生成 schema 并校验返回。
注解按语义标注：`transcribe_status`/`list`/`result`/`search_meetings` 是
`readOnlyHint=True`；`transcribe_start`/`index_meetings`/`render_timeline` 是
`idempotentHint=True`；`transcribe_cancel` 是 `destructiveHint=True`。
收益不止文档——`filesystem` server 用注解实现 `--read-only`（只注册只读工具，写工具直接不出现），
本项目将来要加只读模式时这是唯一干净的实现路径。

**5. 补 progress notification**

SDK 的 `Context.report_progress` 已在手边。管线侧进度目前写 stderr 中文日志
（`qwen_asr.py:1802` 的「pyannote 分块 3/8 完成」），要接进 MCP 需要在 `run_job` 里读
`run.log` 增量并转成 progress 事件。收益两条：agent 知道还要等多久；**idle timeout 靠它续命**
（stdio 默认 30 分钟无响应即中止，两小时会议转写很容易超）。

外部共证：MCP spec 的 `notifications/progress` 硬约束（`progress` 必须递增、完成即停）；
`WhisperX-Workstation` 是本域唯一实现（`agent/mcp_server.py` 的 `_progress_notifier`）。

**6. 给 `transcribe_status` 加有界长轮询**

加 `wait_seconds` 参数，内部夹到 `min(max(wait,0),60)` 秒后返回。agent 一次调用等一分钟，
比高频空转查状态省 token 也省往返。默认 0 保持现有行为不变，不破坏兼容。

外部共证：`AudioSmith`（`internal/mcp/studio.go:83-95`）、`whisper-windows-mcp` 的
`FOREGROUND_MAX_SEC=210` 与 Claude Desktop 240s 天花板的注释。

### P2：结构化与资源化

**7. 把成稿挂成 MCP resource**

`meeting://{stem}/transcript`、`meeting://{stem}/minutes`、`meeting://{stem}/timeline` 三个
资源模板，工具只负责「列、搜、按 id 读」。agent 按需拉片段，不必一次把上万字稿件塞进上下文。
`render_timeline` 现在返回路径，改成同时返回 `resource_link` 更符合规范。

外部共证：`DayBee` 的 `daybee://transcripts/{id}` + 「先列再读」写进 `Instructions`；
`silverstein/minutes` 的 `resources:{subscribe:true}`。

**8. CLI 补 `--json` 与固定退出码**

`--json` 输出单个可被 `jq .` 解析的对象（不要混日志），进度继续走 stderr 并 flush；
退出码固定 `0` 成功 / `1` 通用失败 / `2` 参数错误 / `20` 输入不存在 / `30` 互斥参数冲突，
错误体统一 `{error:true, code, message, suggestion}`。注意这是**加分支而非改默认**——
人类可读输出保持不变，`--json` 只给程序用。

外部共证：`gh` 的 exit code 表（0/1/2/4，并明确警告「单个命令可能另有定义」）、
`ripgrep --json` 的 JSON Lines 五类消息（`begin`/`end`/`match`/`context`/`summary`，
且 `--json` 会隐式关闭颜色与超链接，避免 agent 拿到 ANSI 转义）。

**9. 补齐 7 个过短的工具描述**

每个工具按「做什么 / 何时用 / 何时不用 / 返回什么」四句写。重点是划清
`transcribe_status` 与 `transcribe_result` 的边界——现在两者的描述都短到无法区分，
而它们一个查状态一个取内容。

## 六、明确不做

| 候选 | 理由 |
|---|---|
| 按 toolset 分组收窄工具面 | 本项目 8 个、补完 11 个，远低于 20 的安全阈值。`github-mcp-server` 101→52 的教训不适用，为分组而分组只会增加复杂度 |
| 迁到 MCP Tasks 扩展 | 2025-11 实验性、2026-07 已移出核心成官方扩展。本项目 `job.json` 落盘 + 轮询已经覆盖同等语义，等扩展稳定再说 |
| 用 `resources:{subscribe:true}` 做实时推送 | 本项目没有「转写中实时看稿」的需求场景，订阅机制的复杂度换不来收益。要增量读取有 `offset` 就够 |
| 引入 elicitation 做确认 | 本项目没有不可逆的破坏性操作需要二次确认（`transcribe_cancel` 可重跑），`AudioSmith` 用是在删除场景 |
| 把 `manifest.json` 也搬进 MCP | 已有，但 `job.json` 覆盖了单任务查询。要查批量运行结果应当直接读文件，不必绕一道工具 |

## 七、未核实

1. 「MCP TypeScript SDK 默认每请求 60 秒」的源码常数未取到，只有二手转述。
   Claude Code 侧的 60 秒是官方文档明写的，两者不要混为一谈。
2. 不存在行业级的「agent-friendly CLI 标准」。找到最接近的 `ChaosRealmsAI/agent-cli-spec`
   （98 条/11 维，含 exit code 表）只有 3 stars、单人维护，实质是个人 Skill 规范，
   不能当公认标准引用；本文只用它交叉验证了 exit code 分段这一条。
3. `jq`/`ripgrep`/`fd` 都不是为 agent 设计的（早于 agent 浪潮），只能说事后被广泛采用。
   `fd` 甚至没有 `--json`——说明「被 agent 高频调用」不等于「为 agent 设计了结构化输出」。
4. MCP sampling 在转写领域的实际使用：多轮 code search 未命中，GitHub search 期间多次
   返回 503，覆盖不完整，只能报「未发现」不能断言不存在。
5. `Vexa` 工具总数口径：README 列 15 个，`operation_id` 实际抓出 18 个，
   未确认多出的 3 个是否受 feature flag 默认关闭。
6. 本项目 `scrub_units`（2026-11-26 合入，默认开启）对真实素材的文本改写效果未实测，
   与本轮改动无关但会影响「转写结果的可信度」这条基线。

## 八、与前四轮的关系

r1/r2 定识别引擎，r3 定纪要闭环，r4 定输出字段层，本轮定**调用面**。
四轮不冲突且互补：r4 的 P0（行动项状态列、原文锚点、跨会带入）是「纪要内容该有什么」，
本轮的 P0（options 结构化、纪要工具、分页语义）是「这些内容怎么被 agent 拿到」。
r4 提出的 `minutes_eval` 门禁指标已落地五道（`invented_numbers`/`owner_groundedness`/
`fact_coverage`/`section_presence`/`source_coverage`），本轮的问题正是**它们只存在于 CLI**。
