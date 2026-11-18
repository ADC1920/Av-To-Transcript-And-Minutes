# 2026-11 第四轮调研：通用总结与会议纪要的输出范式

> 目的：前三轮分别覆盖识别引擎（r1/r2）与纪要产品化能力（r3）。本轮只回答一个问题——
> 「通用总结与会议纪要该输出成什么形态」，用外部一手案例对照本项目现范式，给出可落地的改进项。
> 方法：GitHub 同类项目源码与 schema 直读 + 2026 年会议摘要失败归因论文 + 回本项目逐条核实。
> 收录标准：每条结论要么指到外部仓库/论文/产品文档，要么指到本项目代码行；拿不到证据的标「未核实」。

## 一、结论先行

1. **本项目的范式在「文档形态」上已经领先多数开源同类**。七节结构、四类会议分支、四类通用分支、
   三档变体、完整分层版、会前叙事模式、golden 用例回归——这套东西在 GitHub 上找不到第二个
   中文实现做到这个粒度。参照项目中输出结构最完整的 `silverstein/minutes` 也只有一套会议模板，
   没有按会议类型分支。
2. **真正的缺口不在模板，在「导出之后」**。纪要产出即终点：没有行动项状态、没有原文锚点、
   没有跨会跟踪。外部对此有共识（详见四、五节），而这三条本项目的底层能力都已具备。
3. **外部评测给出的最高频失效，本项目的机械门禁全部覆盖不到**。MeetingProbe 三类主错
   （过度自信转述 27.3%、无依据量化 16.9%、说话人张冠李戴 16.0%）合计过六成，全部落在
   语义层；本项目 `minutes_eval.py` 的 `unjudged` 字段已诚实列出，但没有一条被拆下来机检。
4. **最值得抄的一条不是功能，是「类型化」**。`silverstein/minutes` 把行动项、决策、开放问题、
   承诺统一成 `Intent` 四类并各带状态字段；本项目把「软承诺」写在备注文字里，机器读不出来。

## 二、外部范式样本（按可借鉴度排序）

### 2.1 `silverstein/minutes`（2764 commits，v0.33.2，2026-11 活跃）

许可未逐一核对，仅作范式参照，代码不引入。它的 `schema/meeting.schema.json` 是本轮找到的
最完整的一份会议纪要 schema，要点：

| 字段 | 结构 | 对本项目的意义 |
|---|---|---|
| `action_items[]` | `assignee`、`task`、`due`、**`status`（required）** | 状态是必填，不是可选备注 |
| `decisions[]` | `text`、`topic` | 决策带主题归组，可直接按议题聚合 |
| `intents[]` | `kind`（action-item / decision / open-question / **commitment**）、`what`、`who`、`by_date`、`status` | 四类产出物是一个枚举，不是四个章节 |
| `entities` | `people[]` / `projects[]`，每条 `slug` + `label` + `aliases` | 人名归一走实体表，不走全文替换 |
| `status` | complete / no-speech / transcript-only / **degraded** | 文档级完整性信号 |
| `processing_warnings[]` | `step`、`reason`、`timeout_secs`、`message` | 失败原因结构化，不靠翻日志 |
| `attendees` / `duration` / `recorded_by` / `visibility` / `tags` | 平铺字段 | 与基本信息区对应 |

值得单独记的一条工程取舍：它的 PR #249 把「摘要步骤失败却写 `status: complete`」定为缺陷，
改成 `degraded` + `processing_warnings`。理由是用户不该去 grep 日志才能发现这份纪要是残的。
本项目的三态（complete / degraded / failed）思路一致，但只在校验器返回值里，没有进导出结构。

### 2.2 `Yale-LILY/QMSum`（NAACL 2021，232 场会议 / 1808 组问答）

数据格式里每个摘要都带 `relevant_text_span`，即这段总结对应的原文段落区间列表。
它说明的是评测范式：**摘要要能被追回到原文的哪几段**，否则无法判忠实度。
本项目管线本来就有段级时间戳与说话人，这个锚点在导出时被丢掉了。

### 2.3 `tadrop/meet-to-action`

每个行动项附一句**逐字引文**供人工核对承诺出处。它的定位值得注意：输出全部是 draft（草稿），
等用户 review 后才落 Gmail / Asana / Notion。这条与本项目「纪要不是创作」的口径互证，
但多了一层工程保障：**引文可核对**。

### 2.4 `meetstream-ai/labs` 的 action-item-extractor

请求模型时附带会议日期与说话人列表，要求 strict JSON，输出含 `decisions` 与
`open_questions` 两个独立数组。做法朴素，但把「三类产出物分开」落实到了 schema 层。

### 2.5 `danieliser/take-minutes`

本地 LLM 抽取六类：decisions / ideas / questions / action_items / concepts / terms，
分块后做模糊去重，再提供关键词、向量、混合三种检索。它验证了一件事：
**抽取的分类维度可以比纪要章节更细**，检索时按 category 过滤才有用。

### 2.6 反面对照

大量同类项目的输出是「Executive summary + Action items 表」两段式（LangChain 示例、
各类 agent 模板、AWS 样例），没有任何结构校验，也没有状态字段。这类项目的通病正是
MeetingProbe 指出的：结构越自由，模型越容易把推测写成事实，且事后无法机检。

## 三、评测侧的证据

### 3.1 MeetingProbe（arXiv 2608.20392，Evaluation-as-Search）

1049 条失败归因，八类 grounding 错误，前四类：

| 失效类型 | 占比 | 含义 |
|---|---|---|
| 过度自信转述（overconfident paraphrase） | 27.3% | 原文是模糊表述，纪要写成确定结论 |
| 无依据量化（unjustified quantification） | 16.9% | 原文没给数字或范围，纪要写出精确值 |
| 说话人张冠李戴（speaker misattribution） | 16.0% | 归属错人；模型越弱占比越高 |
| 无依据的人数枚举 | 3.3% | 断言「三个人都反对」而原文无支撑 |

另一条对本项目直接相关的发现：**前沿模型的独特失效是编造行动项**（fabricated action items）。
能力越强越会「补全」一个看起来该有的任务。这正好落在本项目最看重的待办表上。

### 3.2 COLING 2025《What's Wrong? Refining Meeting Summaries with LLM Feedback》

两阶段：先让模型定位错误，再修。模仿人工 review 流程。它的价值不在模型选择，
而在确认了「**先挑错、再改写**比一次生成好」这条流程性结论——本项目现在的流程是
生成完跑机械校验，缺一个「定点挑错」环节。

### 3.3 与本项目现有门禁的对照

| 外部失效类型 | 本项目能否机检 | 现状 |
|---|---|---|
| 编造数字 | 能 | `invented_numbers_max = 0`，含中文数字展开（`_cn_numbers`） |
| 责任人无出处 | 能 | `owner_groundedness_min = 0.8` |
| 数字覆盖率 | 能 | `fact_coverage_min = 0.5` |
| 必填节齐全 | 能 | `section_presence_min = 1.0` |
| 过度自信转述 | 不能 | 列入 `unjudged` |
| 无依据量化 | 部分 | 阿拉伯数字能抓，模糊表述转精确值抓不到 |
| 说话人张冠李戴 | 不能 | 列入 `unjudged` |
| 编造行动项 | 部分 | 只校验责任人与数字，任务本身无出处校验 |

## 四、通用总结侧的对照

会议之外的四种类型（访谈 / 播客 / 讲座 / 其他）在开源项目里几乎没有范式可参照——
绝大多数同类项目只做会议。本项目的通用骨架（核心结论 / 关键要点 / 行动事项 /
待定与开放问题 / 内容详情）反而是较完整的。可补的只有两处：

1. **播客与讲座的时间戳可机检**。现在只在自检清单里写「章节时间戳从 0:00 起且与原文对得上」，
   但校验器对通用分支只查必需节，没有查时间戳是否真的存在于原文。这类检查与会议版的
   「数字溯源」是同一套逻辑，可直接复用。
2. **讲座的术语定义可机检**。判据是「定义句式」（「X 是指」「所谓 X」）出现在转写稿中，
   而纪要的术语节列了 X。误报率会高于时间戳，可作为 WARN 而非 FAIL。

## 五、本次核实的本项目现状

| 项 | 位置 | 现状 |
|---|---|---|
| 待办表六列 | SKILL.md 待办事项节 / `ACTION_COLUMNS` | 序号 / 待办事项 / 责任人 / 完成时间 / 优先级 / 备注，**无状态列** |
| schema 行动项 | `references/minutes-schema.json` | `id`/`task`/`owner`/`due`/`priority`/`note`，priority 四档，**无 status** |
| 导出结构 | `validate_minutes.py:776 export_structured()` | 输出 basic_info / sections / action_items / decisions，**无 status、无 processing_warnings** |
| 三态 | `validate()` 返回值 835/837 行 | complete / degraded / failed 存在，但只在校验结果里，不随纪要导出 |
| 语义层 | `minutes_eval.py` 的 `unjudged` | 四项已明列，无一项机检 |
| 跨会检索 | `tools/meeting_index.py` | FTS5 trigram 全文检索 + `action_item_count` 计数，**无行动项状态表** |
| 上期回顾 | SKILL.md 条件节 | 靠用户手动提供上期纪要才触发，不自动带 |
| 软承诺 | SKILL.md 待办事项节 | 入表 + 优先级低 + 备注写「会上为软性表态」，**无类型字段** |
| golden 用例 | `references/golden/cases/` | 8 个：formal-board / interview-branch / layered-review / no-actions / relative-time / retro-sprint / soft-commitment / standup-daily |

## 六、改进项

### P0：改完立刻能用

**1. 行动项加状态列与枚举**

六列改七列，加「状态」，取值未开始 / 进行中 / 受阻 / 完成 / 取消。schema 的 `action_items`
加 `status`，校验器加枚举校验。外部共证：`silverstein/minutes` 把 status 定为 required；
通用实践里五态是共识，且有一条硬约定——**同一项滑期两次，必须在会上改责任人、改日期或改范围，
不许悄悄顺延**。这条约定写进 SKILL.md 才能真正管住跨会跟踪。

影响面：`SKILL.md` 模板与自检、`minutes-schema.json`、`validate_minutes.py` 六列解析与
`collect_action_items`、`fix_minutes_docx.py` 列宽、`minutes_eval.py` 的 items 读取、
golden 8 个用例的 expected。这是本轮唯一需要同步改多处的一项。

**2. 行动项与决策挂原文锚点**

每条待办与每条决策带一个 `source` 字段：段 id 或一句逐字引文。管线的会议原文每段已有
`[mm:ss] 说话人N：` 前缀，引文可以直接从原文摘。外部共证：QMSum 的 `relevant_text_span`、
meet-to-action 的逐字引文。收益是双份的——人工能核对，评测能算「行动项出处覆盖率」。
这是把 `unjudged` 里「编造行动项」拆下来的最低成本路径。

**3. 上期未完成项自动带入**

`meeting_index.py` 已按文件索引会议与纪要，加一张 `action_items` 表（meeting_id、task、
owner、due、status）就能按 owner 或关键词查出未完成项，出稿时自动生成「上期行动项回顾」。
外部共证：通用实践里「下次会开头过上次行动项」被称为留存率最高的一条习惯；
`scribbl` 模板直接有 CARRIED OVER FROM LAST MEETING 区块。现状是这条完全靠人手给上期纪要。

### P1：口径补机器可读

**4. 软承诺升为类型字段**

`minutes-schema.json` 加 `kind` 枚举（行动项 / 决策 / 开放问题 / 承诺），或单独加
`strength` 字段（确定 / 软性）。外部共证：`silverstein/minutes` 的 `Intent` 四类。
现状「会上为软性表态」写在备注自由文本里，机器无法统计「本次会有几条软承诺被漏跟」。
考虑成熟度，建议先只给行动项加 `strength`，不重构为统一 Intent 数组。

**5. 语义层挑一条先机检**

四项 `unjudged` 里，说话人归属是唯一有现成数据支撑的：管线已有 RTTM 与说话人分离，
会议原文每段带说话人标签，只要把行动项的 `source` 引文反向定位到原文段落，就能判断
「这条行动项的归属人是否在那一段发过言」。误报会有（同一人跨段发言），所以先出 WARN，
攒够样本再决定是否升 FAIL。这也是外部 COLING 2025「先挑错」流程的本地化最小版。

### P2：范式完整性

**6. 文档级完整性状态进导出结构**

`export_structured()` 补 `status` 与 `processing_warnings` 两个字段，值直接取 `validate()`
返回值里已有的那份。外部共证：`silverstein/minutes` 的 PR #249。现状记录被截断时，
SKILL.md 只在基本信息区加一句话，机器读不到「这份是残的」。

**7. 通用总结补机械门禁**

播客章节时间戳与讲座术语定义各加一条检查，复用会议版数字溯源的实现方式。
播客时间戳可判 FAIL（原文时刻能精确匹配），术语定义先出 WARN。

**8. 正式会议加「对应动议编号」**

正式会议的待办表加一列或在备注标明该行动项执行的是哪条动议。外部共证：
正式会议模板的通行做法是行动项回指动议编号。本项目 `formal-board` 用例已有法定要素节，
加这一列是自然延伸。

## 七、明确不做

| 候选 | 理由 |
|---|---|
| 重构成统一 Intent 数组 | 七节结构是文档形态，Intent 是数据形态，两者可并存；改结构等于推翻模板与 golden 用例，收益不成比例 |
| 用 LLM 自动把纪要改写成 schema 优先的形态 | 与本项目「纪要是给人看的正式文本」的定位冲突 |
| 实体表（slug + aliases）入 schema | 人名归一现在靠 SKILL.md 规则已够用；引入实体表要先有跨会实体消歧需求，目前没有 |
| 引入向量检索做跨会行动项匹配 | 现在没有真实查询日志；关键词加 owner 过滤已能覆盖 |

## 八、未核实

1. `silverstein/minutes` 的许可证与是否可参考实现细节——本轮只读 schema，未读许可。
2. 五态行动项（未开始/进行中/受阻/完成/取消）在中文语境下的实际采纳情况——来源是英文
   产品文档与模板，没有中文一手案例。
3. 「滑期两次必须改主人或改范围」这条约定的实际执行效果——是通行建议，没有量化数据。
4. 说话人归属机检的误报率——需先用现有 golden 用例跑一遍才知道，本轮未实测。

## 九、与前三轮的关系

r1 / r2 定的是识别引擎选型，r3 定的是纪要质量闭环，本轮定的是输出范式的字段层。
三轮的结论是叠加的，不冲突：r3 说「纪要产出后既没有机器可读形态也没有自动核验」，
本轮把它具体化为「缺 status / 缺 source 锚点 / 缺跨会跟踪」三个字段级缺口，
并给出 P0 三项的最小改动路径。
