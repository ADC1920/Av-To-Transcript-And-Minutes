# 吸纳 pm-claude-skills 与 Fabric · 设计规范

> 触发：用户要求「pm-claude-skills 和 Fabric 完全吸纳进本技能，必要时可以对本技能进行重构」。
> 本文先把两个项目的资产摊开，标出**哪些能直接吸纳、哪些与既有红线冲突、哪些不适用中文场景**，
> 再给重构方案。不确认不动手。

## 一、资产清单（一手直读，2026-11-22）

### 1.1 `mohitagw15856/pm-claude-skills`（1,585 stars，999 技能，会议相关 24 个）

| 技能 | 核心资产 | 吸纳判断 |
| --- | --- | --- |
| `meeting-notes` | 「Meeting Types & Adaptations」四类模板增量（1:1 / Sprint Planning / Product Review / Stakeholder Sync）；Decisions 四字段（Decision/Context/Owner/Deadline）；Action 表格（Action/@Owner/Due）；Open Questions 三字段 | **吸纳**：模板增量与四字段结构 |
| `meeting-action-extractor` | **Orphan 概念**（「someone should…」提出但无人认领）；**Action vs Wish 判别**；每条 action 带 context 以便直接变 ticket；「Owner or orphan」「Due date or TBD，不编日期」 | **吸纳**：Orphan 与 Action/Wish 判别是我们缺的 |
| `board-minutes` | 正式会议完整骨架：Attendance（含 Apologies/Quorum）→ Conflicts of Interest → Approval of Previous Minutes → Agenda Items（Paper/讨论/决议三段）→ Action Register → Approval Footer | **部分吸纳**：Quorum 与 Apologies 我们已有法定要素节，但「上次纪要批准」与「审批脚注」值得补 |
| `briefing-note` | 一页简报五段：Issue / Background / Key considerations / Recommendation / Contact | **吸纳**：作为新的「会前简报」输出变体 |
| `decision-meeting-format` | 会前预读（Options steelmanned）、会中三段（Clarify → Advocate → Decide）、决议记录含 dissent | **部分吸纳**：dissent 记录我们已有；「Options steelmanned」可进会前叙事模式 |
| `engagement-retro` / `post-event-debrief` | 项目结项复盘、活动复盘 | **不吸纳**：超出会议纪要定位 |
| `meeting-prep-live` / `meeting-prep-pack` | 依赖 Google Calendar / Drive / Gmail connector | **不吸纳**：本机无这些 connector |
| `async-standup-compiler` | 依赖 Slack / Notion / Linear connector | **不吸纳**：同上 |
| `meeting-cost-meter` / `meeting-room-etiquette` / `presenter-notes` / `notes-humanizer` / `note-taking-system` | 会议成本计算、会议室规范、演讲者备注、去 AI 味、笔记系统 | **不吸纳**：全部超出定位 |

**关键冲突**：`meeting-notes` 要求缺信息时填 `*(assumed, confirm)*` 并在末尾列出假设。
本项目红线是「不写不确定标注」，纪要是正式文本。**直接冲突，不吸纳。**

### 1.2 `danielmiessler/Fabric`（44,690 stars）

| pattern | 核心资产 | 吸纳判断 |
| --- | --- | --- |
| `summarize_meeting` | 八节结构：OVERVIEW / KEY POINTS / TASKS / DECISIONS / CHALLENGES / TIMELINE / REFERENCES / NEXT STEPS | **部分吸纳**：CHALLENGES（风险与顾虑）与 TIMELINE 是我们没有的独立维度 |
| `summarize_board_meeting` | 董事会正式纪要，含动议原文、提议人、附议人、票数 | **不吸纳**：英美治理语境，中文会议无这类语言标记 |
| `concall_summary` | 财报电话会：管理层评论、分析师问答、隐藏信号、语气分析 | **不吸纳**：投研专业场景，超出定位 |
| `summarize_lecture` | 讲座：主题 + 定义 + 工具清单 + 一句话要点 + 时间戳（HOURS:MINUTES:SECONDS 格式） | **吸纳**：工具清单与一句话要点可补进讲座专属节 |
| `summarize_debate` | 辩论：主要分歧、各方论点、**什么证据能改变其立场** | **吸纳**：「什么证据能改变立场」是研讨型会议的好维度 |
| `summarize_legislation` | 立法提案 | **不吸纳** |

**关键冲突**：`summarize_meeting` 硬性要求「10-20 个要点」「5-10 条决策」「每条恰好 16 个词」，
且缺信息时写 `"No information available"`。这三条**与本轮刚做的「空节整节删除」直接对立**——
它是典型的「配额制 + 占位符」，正是我们要避免的。**不吸纳，仅作反面对照。**

## 二、能吸纳的核心增量（去掉冲突后的净收益）

1. **Orphan 任务**：会上有人提「应该有人去处理××」但没认领的，当前项目直接丢弃。
   吸纳后在待办表下方单列一类，让读者决定「指派或放弃」。这是真实缺口。
2. **Action vs Wish 判别**：`meeting-action-extractor` 给了明确判据——
   真 action 有隐含 owner + 某人同意做的动词；「we should」无承接人是 orphan 不是 action。
   本项目现有「负向过滤」规则意思接近但不够锐利。
3. **会议亚型模板增量**：1:1 / Sprint Planning / Product Review / Stakeholder Sync 四类，
   每类给「追加节」而非新模板。这是低成本的场景变通。
4. **风险与顾虑作为独立维度**（Fabric 的 CHALLENGES）：本项目目前散落在各议题里。
5. **时间线维度**（Fabric 的 TIMELINE）：会上提到的所有截止日期集中一处。
6. **辩论维度「什么证据能改变立场」**：适用研讨交流型会议。
7. **上次纪要批准 + 审批脚注**：正式会议骨架缺失的两块。
8. **一页简报变体**：Issue / Background / Key considerations / Recommendation。

## 三、不吸纳的清单与理由

| 候选 | 理由 |
| --- | --- |
| `*(assumed, confirm)*` 假设标注 | 与「不写不确定标注」红线冲突 |
| `"No information available"` 占位 | 与本轮「空节删除」相反，会诱导编造 |
| 固定条数配额（5-10 条决策等） | 同上，且虚构条数 |
| 恰好 16 词 / 25 词的硬字数 | 中文无对应节奏，且强制长度会损语义 |
| Slack/Calendar/Drive connector 类 | 本机无这些 connector |
| 复盘/活动复盘/会议成本/会议室规范/演讲备注 | 超出会议纪要定位 |
| 动议附议与票数 | 中文商务会议无此语言标记 |

## 四、重构方案（待确认）

**现状**：`SKILL.md` 53884 字节 / 464 行，单文件。吸纳上述 8 项后预计突破 600 行。

**方案 A（推荐）：拆成主文件 + references**

```
skills/meeting-notes-expert/
├── SKILL.md                    主流程与判据（约 320 行）
│   ├── 运行语言 / 触发 / 输入处理
│   ├── 章节取舍（上限非配额）
│   ├── 类型判定 + 性质判定（两级）
│   └── 工作流程 / 质量自检 / 反模式
└── references/
    ├── templates.md            各类型骨架全文（会议四子型/通用/亚型增量）
    ├── extraction.md           待办提取口径（含 Orphan/Action-Wish 判别）
    └── formatting.md           排版与出稿链（article-format）
```

理由：主文件只放「每次都需要的判据」，模板全文按需读。技能加载时主文件轻，出稿时才读模板。

**方案 B：保持单文件**
好处是不拆、加载一次到位；坏处是 600+ 行，且模板与判据混在一起。

## 五、待确认

1. 重构方案选 A（拆分）还是 B（单文件）？
2. 八项增量是否全要，还是有想砍的？
3. 是否引入「风险与顾虑」与「时间线」两个新章节？引入会让决策型会议的章节数从 7 增到 9。
