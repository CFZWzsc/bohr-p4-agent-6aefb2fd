---
name: research-analysis-pipeline
description: Use when a research data-analysis case gives raw tabular data and asks for an end-to-end ingest-clean-analyze-report workflow with checkpoints, provenance, or resumable stages; run scripts/run_skill.py for staged artifacts and a result.json.
---

# 从原始数据到结论的多步分析流水线

## 用途与适用条件

在需要「一条命令跑完但从不清空中间证据」的任务中使用：原始表摄入、画像、清洗、统计、图表规格、报告生成，每一步落盘并记录输入输出哈希，可在同一输入上复跑并跳过已完成阶段。

不适用：需要人工判断的研究设计、机器学习建模、外部数据库抽取、自由文本综述。

## 输入

- `case.json`：`case_id`、`output_contract`；可选 `group_column`、`value_column`、`alpha`、`resume`。
- 数据：CSV/TSV，自动识别编码与分隔符。
- 工作区 `/app/work` 保存 `stages/*.json`、`pipeline_state.json`；`/app/output/result.json` 与 `report.md` 为最终产物。

## 处理流程（对应评分维度）

1. **ingest（任务完成度）**：定位数据文件，计算 SHA-256，记录编码、分隔符、行数；无文件则终止并说明。
2. **profile（正确性）**：逐列类型、缺失、去重后取值数；识别分组列与数值列，记录推断依据。
3. **clean（正确性）**：仅剔除目标数值缺失或不可解析的行，完全不重复、不填补；输出清洗前后行数与剔除原因分布。
4. **stats（正确性）**：描述统计（n、均值、SD、中位数、Q1/Q3）；两组 Welch t 检验，多组单因素方差分析；样本量不足或方差为零时不出 p 值；效应量给 Cohen d 或 eta²。
5. **figure_spec（完整性）**：输出图型、编码、轴标题、单位、图注骨架与配色，不伪造 PNG。
6. **report（可读性）**：Markdown 报告中的每个数字都来自 `stages/*.json`，不手写与产物不一致的结论；附限制与复跑命令。
7. **断点与失败传播（可靠体验）**：每阶段写 `status`、`input_hash`、`output_hash`；上游失败则下游标 `blocked`，不静默继续；相同输入复跑时已完成阶段标 `skipped_resume`。

## 输出契约

| 字段 | 含义 |
|-|-|
| `case_id` | 与输入一致 |
| `output_contract` 各字段 | 命中别名时填真实值，否则 `null` + `contract_notes` |
| `status` | `success` / `partial` / `insufficient_input` / `failed` |
| `result` | `stages`、`artifacts`、`dataset`、`statistics`、`report`、`checks` |
| `evidence` | 输入哈希、阶段产物路径与哈希、规则版本 |
| `uncertainty` | 自动列推断、未做多重比较校正、无位图 |
| `errors` / `stats` | 真实错误与计数 |

## 异常处理表

| 情况 | 处置 | 状态 |
|-|-|-|
| case.json 缺失/非法 | 记录错误，终止 | `failed` |
| 无数据文件 | ingest 失败，下游 blocked | `insufficient_input` |
| 缺少分组列 | 仅单变量描述，标记降级 | `partial` |
| 组样本量 < 3 | 不检验，报告描述统计 | `partial` |
| 清洗后无有效行 | 终止后续统计 | `insufficient_input` |
| 阶段写盘失败 | 记录错误并停止下游 | `failed` |
| 相同输入复跑 | 已完成阶段 `skipped_resume` | `success` |

## 限制

- 自动列推断可能误选 ID 或时间列，需在 `dataset.inference` 复核。
- 只实现两组 Welch t 与多组单因素方差分析，不做非参数检验、配对检验或多重比较校正。
- 图表阶段只产出规格，不生成位图；报告为 Markdown，不含期刊排版。

## 最小调用示例（示例，非官方案例结果）

```bash
python /skills/weekly-skill/scripts/run_skill.py --case /input/case.json --out /app/output/result.json --work /app/work
```

示意输入 `{"case_id":"synthetic-demo","output_contract":{"stages":"array","report":"string"}}`；示意输出保留 `case_id`、`stages`、`report`、`status`，无数据时为 `insufficient_input`。仅为接口示例。


## 交付保证与评分对齐

调用顺序（固定）：

1. 先运行包内脚本（若存在）：`python scripts/run_skill.py`（默认读 `/input/case.json`，写 `/app/output/result.json`）；路径不同时用 `--case` / `--output` 指定。
2. 再完成脚本无法自动完成的实质工作（译文、论述、图表、修订、结论等），把结果写回 `result.json` 对应字段。
3. 最后自检：`case_id` 与输入一致；`output_contract` 每个字段都存在；`status` 按契约取 `success`/`partial`/`insufficient_input`/`failed`（缺输入或证据不足时用 `insufficient_input`/`partial`）；无 `pending` 占位；数字、引用、来源可逐条回溯。

运行保证：

- 输入缺失、JSON 损坏、字段异常、输出目录不存在时，仍须写出结构化 `result.json` 并正常结束，不得崩溃或非零退出。
- 不联网、不伪造数据、不声称平台评分或官方案例结果。
- 交付物必须是可直接编辑与复核的成品，而不是流程说明。

评分对齐（本题五个维度）：

- 实际效果（40）：完整产出任务要求的最终成品，覆盖全部输入条目，可直接进入修改流程。
- 使用增益（25）：体现相对基础 Agent 的可验证改进（结构化、术语/数字/引用核对、可编辑性）。
- 创新增益（15）：保留方法学增量（分块、对齐、二次校验、版本比对等），并用结果证明其价值。
- 可靠体验（10）：异常输入有明确降级与说明，不同长度/文体/重复运行下稳定产出。
- 生态贡献（10）：用途、调用、输入输出、限制、示例、维护信息齐全，资源可被他人复用。

## 最终交付强制要求

1. **调用 Agent 必须交付成品，不是流程说明**：脚本产出只是索引、核对或规格骨架；调用 Agent 必须在同一 `result.json` 中补齐本题要求的最终产物（正文/译文/审稿意见/综述/分析结论/图表源文件与导出件/幻灯片页内容与讲稿/实验方案等）。交付字段为空、只有标题骨架，或含 `pending`、`TODO` 之类占位，一律视为未完成。
2. **必须覆盖全部输入条目**：输入中的每个章节、问题、文献、图、页面、指标都要有对应条目，不得只处理示例或首条；确实缺失的条目写入 `uncertainty` / `limitations` 并说明原因，不得静默跳过。
3. **必须自检后再交付**：`case_id` 与输入一致；`output_contract` 每个字段都存在；状态值取本题契约规定的枚举；数字、单位、引用、来源可逐条回溯；结果内部无自相矛盾。
4. **参数等价**：`python scripts/run_skill.py --case <输入> --out <输出>` 与 `--output <输出>` 等价；默认 `/input/case.json` → `/app/output/result.json`。
5. **平台用 3 个固定案例分别运行**：逻辑要由输入驱动，不针对某一份示例硬编码；字段命名差异走别名匹配与显式降级，并在 `uncertainty` 记录假设。
