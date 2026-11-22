# 会议纪要技能场景可变通改造 · 设计规范

> 依据：2026-11-22 一场 29 分钟、4 人的吴语会议录音真实出稿全过程，
> 以及当时仓库内 `skills/meeting-notes-expert/` v1.13.1 的代码与校验器实读（该技能现已拆为独立仓库）。
> 外部调研证据见本文「外部对照」一节。

## 一、问题：模板刚性，不是分支不够

现有技能已有 8 类分支（常规/正式/复盘/站会/访谈/播客/讲座/其他），分支数量不是瓶颈。真实瓶颈有三层。

### 1.1 识别了会议性质，却没让性质驱动结构

`SKILL.md` 工作流程第 2 步原文已写明：

> 会议看性质（决策审批/工作推进/研讨交流/汇报通报，常为混合，决策类突出结论、推进类突出分工与受阻、研讨类突出共识与分歧）

这句话只影响**提炼侧重**，不影响**章节结构**。四种性质全部落到同一套七节。于是「研讨交流」类会议也必须摆出「关键决策」「待办事项」两节，没有内容就写占位句。

### 1.2 空节被当成必填，产出占位符

本次出稿实测（`.tmp/av2m-final/会议纪要-20261122-0836.md`，共 2018 字）：

| 章节 | 字数 | 占比 |
| --- | --- | --- |
| 会议基本信息 | 106 | 5% |
| 会议结论 | 123 | 6% |
| **关键决策** | **14** | **0%** |
| 待办事项 | 396 | 19% |
| 待定与开放问题 | 344 | 17% |
| 会议内容 | 966 | 47% |

「关键决策」全节只有「本次会议未形成决策。」这一句占位。这次会议的真实价值集中在「待定与开放问题」（6 条未决事项）和「会议内容」。模板强制它占一个一级标题，读者打开纪要看到的第 3 节是个空壳。

### 1.3 技术层把刚性焊死了

不只是文档措辞问题，代码里也是硬编码：

- `validate_minutes.py:29`：`REQUIRED = ["会议结论", "关键决策", "待办事项", "会议内容"]` —— 常规会议四节缺一即 FAIL。
- `validate_minutes.py:472-481`：类型判定**从稿件章节名反推**（有「本次目标与结果」就是复盘），而不是从材料特征判。
- `references/minutes-schema.json`：`sections.required` 硬列四节，`minLength: 1`。
- `validate_minutes.py:553`：`elif not decisions: bad(...)` —— 无决策时只允许写一句声明，不能删节。

因此「改文案」不够，必须同时改校验口径，否则变通后的稿子会被自家校验器判 FAIL。

## 二、本次素材暴露的另外三个缺口

### 2.1 转写质量分层没有承载机制

本次是吴语高密度素材（「搿个/勿/侬/格末」），识别文本可读但碎片化。现有技能只有一句「把握不准的句子保留原文表述」，没有机制表达「这份纪要的整体可信度是分层的」：哪些结论是高置信、哪些是单句孤证、哪些只能存疑。

结果是纪要写成「所有句子同等确定」，而实际证据强度差别很大。

### 2.2 说话人身份全未知时，七节结构仍然照套

本次 4 个说话人一个都推不出真名，全文「说话人0/1/2/3」。这种稿子的可用性主要在「说了什么议题」，而不在「谁承诺了什么」——待办表 4 条里 3 条责任人待定，这张表的跟踪价值其实很低。但模板仍要求它作为一级章节、七列齐全。

### 2.3 议题密度与结构粒度不匹配

29 分钟里真正有实质内容的议题不足一半（大量家庭、子女教育、装修闲谈）。技能的处理是把闲谈归入「其他事项」一句话带过，这是对的；但对「议题少、讨论深」与「议题多、每条都浅」这两种形态，七节的固定层级没有区分手段。

## 三、改造方向（待外部调研回填后定稿）

初步方向，未经调研验证前不作为结论：

1. **性质驱动结构**：把已经识别出的四种会议性质（决策审批/工作推进/研讨交流/汇报通报）升级为结构选择器，不同性质给不同的章节集与顺序。
2. **章节分三档**：必填 / 条件必填（有内容才出，无内容整节删除而不是写占位）/ 可选。空节删掉，不留占位句。
3. **置信度分层**：新增可选的「证据强度」表达机制，让低置信段落有承载位置，而不是混在正文里冒充确定事实。
4. **校验口径同步放宽**：`REQUIRED` 改为按性质分派；schema 的 `required` 对应调整；保留「无决策时必须显式声明」这条底线（防止漏做），但声明后允许删节。

## 四、外部对照（一手核实）

### 4.1 `silverstein/minutes`（MIT，GitHub，本地会议记忆工具）

**必填字段的层级选对了。** 一手读 `schema/meeting.schema.json` 与其
`docs/architecture/frontmatter-schema.md`（对应 v0.29.0）：

```json
"required": ["title", "type", "date", "duration"]
```

顶层 `required` 只有这四个元数据字段，**四个内容章节一个都不必填**；
`action_items`、`decisions`、`intents` 全是可选数组。

`docs/architecture/frontmatter-schema.md` 原文（「Structured extraction」节）：

> Empty extraction lists are omitted even on `complete` artifacts.

即：**没有内容就不出这个字段，哪怕整份文档状态是 `complete`**。这跟本项目
「无决策也必须写一句占位、节标题不能删」正好相反。本项目把「正文」当必填、
把元数据当可省略；它把元数据当必填、把正文内容当可省略。**这是本次诊断里最有价值的一条对照。**

同类但更细的一条：它的产出物不按章节切，按 `kind` 枚举归类
（`action-item` / `decision` / `open-question` / `commitment`），正文只有
`## Summary` 与 `## Transcript` 两节。文档形态与数据形态分离，所以正文多一节少一节都不影响校验。

### 4.2 该项目的 23 个 skill 按「工作阶段」拆，不按「会议类型」拆

实际目录（`.claude/plugins/minutes/skills/`）：`minutes-brief`、`minutes-prep`、
`minutes-debrief`、`minutes-recap`、`minutes-record`、`minutes-note`、`minutes-ingest`、
`minutes-graph`、`minutes-lint`、`minutes-verify`、`minutes-cleanup`、`minutes-search`、
`minutes-list`、`minutes-tag`、`minutes-mirror`、`minutes-copilot`、`minutes-ideas`、
`minutes-setup`、`minutes-weekly`。

**没有一个 skill 叫「meeting-minutes」，也没有按会议类型分。** 它按「用户在做什么」
拆：会前 brief/prep、会中 record/note、会后 debrief/recap。同一个会议在
`debrief`（单场回顾）与 `recap`（跨场日报）里输出结构完全不同。

`minutes-brief/SKILL.md` 里有一段分流写法值得直接借鉴——它把「什么时候用 A、什么时候用 B」
写进 description：

> Different from /minutes-prep — brief is the fast hook-fireable version that
> doesn't ask questions and doesn't set goals. Use brief when speed matters;
> use prep when the user wants to think hard about goals first.

### 4.3 模板数按「会议要产出什么」定，且明确写了无产出怎么写

`meetingsbrief.com`《Best Meeting Notes Template — 5 Formats for Every Meeting Type》
（2026-10-24 更新）给了五套模板（常规／客户通话／站会／策略会／一对一），选型依据一句话：

> The simplest question: what does this meeting need to produce?
> A decision → general or strategy template
> A set of commitments with owners → general template's action items
> A record of what the client said and wants → client call template
> A quick status update → standup
> A development conversation → one-on-one
> **If none of these fit, adapt the general template: change the section headers,
> add or remove rows, and use it.**

关于空模板，原文（三条原则之一）：

> A good template is shorter than you think it should be. If a template has eight
> sections and your meeting only fills two of them, you will either skip the
> template or leave six sections blank. Both outcomes mean the template is wrong.

关于无决议会议，FAQ 原文直接回答了本次的问题：

> **What if a meeting produces no decisions and no action items?**
> Write that. "No decisions made. No action items. Next session: [purpose]."
> A meeting notes document that honestly records a meeting with no output is more
> useful than one that manufactures false outcomes. It also flags that the meeting
> may not need to happen in its current form.

注意这条与「保留空节」并不矛盾：它写的是**一句话坦白**，不是留一个空章节占位。
本项目当前的做法是保留「关键决策」一级标题 + 一句「本次会议未形成决策」，
比它的做法更重。

### 4.4 三条可落地的借鉴

1. **必填下沉到元数据，内容节按有无决定去留**（来源 4.1）。本项目现在是反的，
   改法是：基本信息（主题/时间/参会）必填，「会议结论」必填（它承载「这次到底谈了什么」），
   其余内容节全部条件化。
2. **按「会议要产出什么」分流，不按「会议叫什么」分流**（来源 4.3）。
   本项目现有的四种性质（决策审批/工作推进/研讨交流/汇报通报）其实已经是这个维度，
   只是没有接到结构上。接到结构上即可，不需要新增分类体系。
3. **分流依据写进技能描述本身**（来源 4.2）。让模型在出稿前先自己判一次
   「这场会要产出什么」并把判定结论作为选模板的依据，而不是让读者从章节名反推。

**不适用的部分**：4.2 的多 skill 拆分方式不适用本项目——本项目是单一技能、
面向中文商务会议与口语转写稿，拆成 19 个技能会让触发词互相打架，且本机部署
成本高于收益。借鉴其「分流写法」，不借鉴其「拆分粒度」。

### 4.5 补充调研（子代理，一手来源逐条核验）

**① 类型字段必须排在正文之前，顺序是硬约束。** `ricardojustus/blaise`，
`specs/c6_notes_synthesis.md` 原文：

> **Schema property order is LOAD-BEARING:** structured-output generation follows
> schema property order, so the schema is spliced into the request body as its RAW
> authored JSON (title → summary → meeting_type → detailed_notes → decisions →
> action_items …) — analysis before conclusions, and the meeting-type
> classification committed BEFORE the notes are written

同文件记录了反例：字典往返把属性按字母排序后，`action_items` 先生成，
实测 3/3 产出「schema 合法但完全空的骨架」，仅 48 个输出 token。**这条对本项目的直接含义：
「会议性质」必须写在稿子基本信息区靠前位置，不能写在末尾或脚注。**

它的枚举设计也值得吸收：8 值严格枚举 + `general` 作为显式逃生口
（`general` is the explicit "no strong cue" escape — an enum cannot leave the taxonomy），
另有固定的平局顺序。

**② 章节计划是上限，不是配额。** 同上文件原文（大写在原文里）：

> **THE PLAN IS A MAXIMUM, NOT A QUOTA** — empty sections are omitted, the
> anti-hallucination rules apply inside every section

这条把「空节删除」和「反幻觉」绑成一条规则：**删节不是为了好看，是为了不给模型填满空节的动机。**
与 `shaowen310/skills` 的渲染器口径一致：`omitted entirely — no placeholder or fallback remains`。

**③ 无决议应是「带关闭路径的记录」，不是空字段。** Scripta《A meeting with no decision
still needs a record》给出的三态区分原文：

> The summary should distinguish three states: **Decided**: the owner made the call and
> the outcome is accepted. **Open**: the options and closure path are known, but no call
> has been made. **Not addressed**: the meeting never reached the question.

配套的四要素 closeout 范式：待决问题、缺失证据、证据责任人与决策责任人、回看触发点。
GitLab Handbook 的 Technical Discovery 节给同一立场：`We should not force a proposal,
but aim to get there and write the conclusion accordingly with potential follow-ups.`

**④ 反模式对照：不裁节会长出占位噪音。** `danielmiessler/Fabric` 的
`data/patterns/summarize_meeting/system.md` 规定 `If information for a section is not
available in the transcript, write "No information available".` —— 这正是本项目当前
「本次会议未形成决策」占位句的同款做法，会让讨论型会议也撑出一个 DECISIONS 节。

**⑤ 「讨论升格为决议」的判据可机检。** `mohitagw15856/pm-claude-skills` 的
`references/decisions-vs-discussion.md` 给出一条可执行的追问测试：

> The follow-up test: Read only your Decisions + Action items sections. Could someone
> who missed the meeting act correctly all week? If they'd need the Discussion section
> to act, something is mis-bucketed.

**⑥ 中文场景的适配边界。** `tiroq/mnemofy` 的加权关键词分类器（`src/mnemofy/classifier.py`，
9 值枚举 + 权重表 + 问句/时间词/承诺词三个结构特征 + 分差加权置信度）结构可迁移，
**但英文词表不可迁移**（`stand-up`/`root cause`/`trade-offs` 这类），需重写中文词表。
其无命中时显式回落低置信度（`default to TALK with low confidence`）而非硬猜，这点值得照做。

## 六、已定决策（2026-11-22）

| 议题 | 决定 | 依据 |
| --- | --- | --- |
| 改造力度 | 内容节按有无决定去留；不加性质驱动的骨架分派 | 改动集中在 3 文件，历史稿不受影响；性质驱动骨架留待观察 |
| 无决策写法 | 整节删除，不写占位句 | 4.1 空列表直接省略 + 4.5 上限非配额 + 4.3 FAQ 一句话坦白 |
| 性质判定 | 模型自判，写进基本信息区（`会议性质：研讨交流`） | 4.5 顺序是硬约束，类型须在正文之前提交 |

**保留的底线**：原「无决策必须显式声明」改为「无决策时整节不出现，且必须在
『会议结论』首段说明本次未形成决议」——防止漏做，但不再单独占一个章节。

## 七、实施口径

改动三处，同步两份副本：

1. `SKILL.md`：性质判定上移到工作流程第 1 步；七节改为「1 必填 + 5 条件节」；
   新增「会议性质」字段；补「上限非配额」规则；补「无决议三态」写法。
2. `validate_minutes.py`：`REQUIRED` 收窄为 `["会议结论", "会议内容"]`；
   从基本信息解析「会议性质」；无决策时不再判缺节，改为检查「会议结论」是否声明未形成决议。
3. `minutes-schema.json`：`sections.required` 同步收窄。

**兼容性**：历史稿仍含「关键决策」节且写占位句，校验器必须继续认它通过（新增的条件节
只在缺失时放行，不追究）。这条要写进 selftest。

## 八、待决策（遗留）

- 「无决议三态」（Decided/Open/Not addressed）是否引入？本次素材的 6 条未决事项
  已有「卡在哪」描述，但缺「谁负责关闭、何时回看」。引入会增加出稿成本。
- 置信度分层本次不做，理由见第一节问题诊断：属独立课题，混进本次改造会失焦。
