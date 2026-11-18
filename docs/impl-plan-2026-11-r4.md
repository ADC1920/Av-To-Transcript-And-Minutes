# 实施计划：纪要输出范式八项改进（2026-11-18）

> 依据：`docs/landscape-2026-11-r4.md` 的 P0/P1/P2 八项改进清单。
> 用户裁决：八项全做。
> 状态：已落档并实施，回滚点见第五节。

## 一、目标

把纪要的输出范式从「文档形态只有、数据形态缺失」补到两者并存：

- 行动项有状态、有原文锚点、能跨会跟踪
- 软承诺从自由文本升为类型字段
- 语义层拆一项最便宜的先机检
- 文档级完整性状态进导出结构
- 通用总结补两条可机检项
- 正式会议行动项回指动议

## 二、影响范围

| 文件 | 改动 | 破坏性 |
|---|---|---|
| `skills/meeting-notes-expert/SKILL.md` | 待办表七列、状态五态与滑期约定、锚点写法、软承诺字段、通用总结机检口径、正式会议动议列 | 是（模板变化） |
| `skills/meeting-notes-expert/references/minutes-schema.json` | `action_items` 加 `status`/`strength`/`source`；`decisions` 改对象数组加 `source`；顶层加 `document_status`/`processing_warnings` | 是（decisions 从字符串数组变对象数组） |
| `skills/meeting-notes-expert/scripts/validate_minutes.py` | 七列解析、状态枚举、出处解析、软承诺强度、文档状态、通用总结时间戳与术语机检、正式会议动议、说话人归属 WARN、`export_structured` 补字段 | 是 |
| `skills/meeting-notes-expert/scripts/fix_minutes_docx.py` | 七列列宽 | 否（加一项） |
| `skills/meeting-notes-expert/references/golden/cases/*` | 9 个用例的 expected 全部补状态列与出处 | 是 |
| `tools/minutes_eval.py` | 读新字段；新增行动项出处覆盖率指标 | 否 |
| `tools/meeting_index.py` | 加 `action_items` 表与跨会未完成项查询 | 否（新表） |
| `README.md` | 待办七列与导出字段说明 | 否 |
| `skills/meeting-notes-expert/CHANGELOG.md` | v1.13.0 段与破坏性说明 | 否 |

## 三、关键设计取舍

### 3.1 决策锚点只进 JSON，不进 docx 正文

SKILL.md 既有口径是「决策用肯定句式，不标状态、不附依据、不展开备选方案」。
在决策条目后加「（据 00:35 主持人发言）」就是附依据。冲突按优先级裁决：
用户要求「挂原文锚点」（当次指令）与技能默认（不附依据）之间，取**分层方案**——

- docx 正文：决策保持原样，不加任何锚点
- JSON 导出：`decisions` 从字符串数组变成 `{text, source}` 对象数组，`source` 由纪要解析时的
  「决策出处」区或人工补录提供，缺省为空字符串

这样文档形态不受破坏，数据形态拿到锚点。行动项例外：待办表本来就是工作表，
锚点以备注列「出处：00:35 主持人」形式呈现，人可见、机器可解析。

### 3.2 待办表七列而非八列

加「状态」后七列：序号 / 待办事项 / 责任人/责任部门 / 完成时间 / 优先级 / 状态 / 备注。
出处不独立成列，写进备注列前缀——八列在 A4 竖向（可用宽 15.92cm）会把每列压到 2cm 以下，
内容频繁换行，可读性明显下降。备注列本就是自由文本列，承载出处不破坏语义。

列宽：1.0 / 4.9 / 1.9 / 2.1 / 1.3 / 1.5 / 3.2 = 15.9cm。

### 3.3 状态五态与滑期约定

取值：未开始 / 进行中 / 受阻 / 完成 / 取消。缺失填「待定」（与本项目既有「待定」口径一致）。

滑期约定写进 SKILL.md 而非校验器：同一项滑期两次，必须在会上改责任人、改日期或改范围，
不许悄悄顺延。这条是流程约束，校验器无法判断「第几次滑期」——没有历史状态表之前不硬判。

### 3.4 跨会跟踪落 meeting_index 而非新工具

`tools/meeting_index.py` 已有 `meetings` 表与 `action_item_count` 列，加一张 `action_items`
表（meeting_id、task、owner、due、status、strength）即可查询未完成项，不新建工具。
出稿时的「自动带入」由查询函数提供，SKILL.md 说明调用方式，不在技能里写死路径。

### 3.5 说话人归属机检只出 WARN

判据：行动项的出处引文能定位到原文某段，而该段的说话人与责任人不同 → WARN。
误报来源明确（同一人跨段发言、责任人由他人口头指派），所以不 FAIL。
无源材料或无出处时不判。

### 3.6 通用总结两条机检的严格度

- 播客章节时间戳：FAIL。原文时刻能精确匹配，错了就是错。
- 讲座术语定义：WARN。判据是「X 是指」「所谓 X」类定义句式出现在原文，误报率高于时间戳。

## 四、迁移路径

1. 旧纪要（六列表格）：校验器按表头识别，六列时状态列缺失 → WARN「建议补状态列」，
   不 FAIL。这样历史归档件重跑不会集体报错。
2. JSON 消费方：`decisions` 从 `["文本"]` 变 `[{text, source}]`。`export_structured`
   保持向后兼容——同时输出 `decisions`（对象数组）与新增的 `decision_texts`（纯字符串数组），
   旧消费方改读后者即可。
3. 索引库：`action_items` 表用 `CREATE TABLE IF NOT EXISTS`，旧库自动升级，无需迁移脚本。
4. 技能副本：改完仓库件后同步 `~/.zcode/skills/meeting-notes-expert/`，旧副本备份到 `.tmp/`。

## 五、验证与回滚

### 验证

1. `python validate_minutes.py --selftest` 全 PASS（含新增断言）
2. `python golden_cases.py` 9/9 PASS
3. `python -m py_compile` 全部改动脚本
4. 反向用例：状态写「已完成一半」→FAIL；备注无出处→WARN；软承诺强度写非法值→FAIL
5. `tools/minutes_quality.py --selftest`、`tools/minutes_eval.py --selftest` PASS
6. `tests/test_tools_units.py` 全 PASS
7. 真实纪要导出：`--export` 后确认 `status`/`processing_warnings`/`source` 三字段在位
8. docx 出稿：七列表格列宽不截断，渲染目检

### 回滚

- 回滚点：本次改动前的 `116cb7b`（`fix/meeting-notes-expert` 当前 HEAD）
- 单仓回滚：`git checkout 116cb7b -- skills/meeting-notes-expert tools README.md`
- 索引库回滚：`action_items` 表可整表 DROP，不影响既有 `meetings`/`segments`
- 技能副本回滚：从 `.tmp/meeting-notes-expert-backup-*` 恢复

## 六、不做

- 不把 `decisions` 一次切成统一 Intent 数组（见 r4 档案第七节）
- 不在校验器判断「第几次滑期」（无历史状态表，硬判必误报）
- 不给待办表加第八列出处（见 3.2）
- 不做实体表与向量检索（见 r4 档案第七节）
