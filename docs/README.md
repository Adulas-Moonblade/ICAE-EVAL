# Course 实验文档

本文档记录 `course` 分支在五个任务上的八组对照实验。结果快照截至 2026-11-19；每个任务只纳入一个正式八组批次。`results/` 被 Git 忽略，文中的批次路径指向本地数据，发布文档时应同时保存对应的导出包。

| 文档 | 用途 |
| --- | --- |
| [实验设计](experiment-design.md) | 研究问题、八组条件、对照关系和实验边界 |
| [实现与数据流程](implementation-and-data.md) | 相对原始 `main` 的改动、运行过程、数据来源与核验 |
| [结果与讨论](results-and-discussion.md) | 正式批次的数值、任务差异、成本与局限 |

## 阅读和复核顺序

1. 先读实验设计，明确 A、A′、B、C、D 各自改变的条件。
2. 再读实现与数据流程，了解 Oracle 回答如何进入编码，以及指标口径。
3. 最后读结果与讨论；所有百分比均可从各批次 `runs.csv` 和 `summary.json` 复核。

指标必须与结果一起解释。尤其不能把公开测试并入 Overall，也不能把 Codex token 当作包含 Oracle 的总模型开销。`constraint_coverage_pct` 反映 Oracle 服务日志命中的隐藏约束 ID，与实现通过率是两类指标。
