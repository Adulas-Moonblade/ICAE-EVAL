# 实现改动、运行与数据口径

## 相对原始 `main` 的主要改动

`git diff main...course` 显示本分支新增约 3,700 行、修改 38 个文件。下面按实验功能概括，不把通用工程改动误写成研究条件。

| 组件 | 改动及作用 |
| --- | --- |
| `run.sh`、`harness/course_experiment.py` | 新增 `--course` 单任务八组入口；分开执行需求与编码阶段，记录批次、每组状态、预算、评测和替换历史。 |
| `harness/codex_runner.py`、`harness/docker_env.py`、任务提示模板 | 增加容器内 Codex CLI 执行、事件轨迹与 token 用量采集；开发容器含 Codex，评测沿用原基础镜像。 |
| `harness/course_oracle.py`、`user_agent/` | 增加按组和 `append_id` 隔离的 Oracle relay、预算与 C 组请求结构校验；保留原始与服务日志，只将公开回答、Fallback 状态及剩余额度返回代理。 |
| `harness/course_report.py` | 导出八组表、配对差值、诊断、文件清单与关键轨迹；核对评测、Oracle 计数和 token 数据。 |
| 评测与配置相关文件 | 接入 Codex 与 OpenAI 兼容的 critic 配置，处理容器网络、代理回退和超时。 |

具体实现应以当前源码和 `git diff main...course` 为准。上述变更让实验条件可运行、可核对；不能把分支所有改动都解释为性能变化的原因。

## 运行和信息流

典型正式命令：

```bash
./run.sh --course --repo realcode@005 \
  --model-name Codex-config \
  --codex-model gpt-6-sol --reasoning-effort medium \
  --user-model-name Deepseek-V4.1-Flash
```

每组先按条件处理需求，然后从新工作区启动编码，最后执行公开、隐藏和增强测试。B/C 的提问经 relay 转发给 Oracle；relay 保存原始响应，服务日志是构造编码提示词中问答的可信来源。若需求阶段摘要与服务日志的措辞不同，可出现 `oracle_log_mismatch=true`：此字段提示审计差异，不表示编码未收到服务端原始问答。发生基础设施重试时，新尝试得到新 `append_id`；正式结果只计入选中的尝试。

一组结果可通过 `append_id` 找到原始轨迹和评测输出。`results/course_runs/<batch_id>/summary.json` 是正式批次索引。导出命令为：

```bash
.venv/bin/python -m harness.course_report --run-dir results/course_runs/<batch_id>
```

`results/course_exports/<batch_id>/` 包含 `runs.csv`、`pairs.csv`、`diagnostics.json`、`manifest.json`、`summary.json` 及关键日志快照。导出不会覆盖已存在的目录。生成代码和逐用例 `outcap_*` 不在导出包中，深入复查仍需 `results/<append_id>/`。`results/` 已被 Git 忽略，因此 Git 提交只保存文档和程序，不自动保存实验数据。

## 指标定义及为什么需要解释

| 指标 | 本实验口径 | 解读边界 |
| --- | --- | --- |
| Overall Pass Rate | `(hidden_passed + enhanced_passed) / (hidden_total + enhanced_total)`，逐用例合并后计算 | **不含公开测试**；不是两个百分比的简单平均。缺失评测留空；生成完成但没有测试入口且记录了错误时按零处理。 |
| 公开通过数 | `public_passed/public_total` | 单列，用于确认公开样例执行情况；本批全部通过，不能据此断言隐藏约束满足。 |
| 约束覆盖率 | Oracle 服务日志中 `triggers_hit` 的不同隐藏约束 ID 数 ÷ 该任务 Oracle 数据中的隐藏约束总数 | 仅 B/C 适用；表示问答触达的约束，不表示代码正确实现，也不包括未触发但代理自行推断的约束。 |
| Fallback Rate | 被标记 `fallback_triggered` 的次数 ÷ 已使用 Oracle 轮次 | 仅 B/C 适用；Fallback 占预算。`parse_error` 单独记录，不自动等于 Fallback。 |
| 提问预算与用量 | B/C 为 Oracle 请求上限 3 或 8；A′ 为自行作答条目上限 | A′ 与 B/C 的 `actual_questions` 含义不同，不能合并成“外部提问数”。 |
| Codex token | 需求阶段加编码阶段的 Codex `turn.completed` 用量，包含已计入的缓存输入 token | **不含 Oracle 模型 token**，也不是账单费用。阶段用量与总用量须使用同一口径。 |
| 时间 | 需求阶段加编码阶段耗时 | 包含正常 Oracle 等待；不含容器准备、最终评测及基础设施重试时间。 |
| `study_ready` | 批次完整、质量数据可用、状态可接受且没有导出诊断问题 | 是数据完整性门槛，不是成绩高低或实验无偏的证明。 |

正式对照只纳入八组完整且 `study_ready=True` 的批次。`pairs.csv` 的差值为右组减左组。`manifest.json` 列出已归档文件及缺失文件，`diagnostics.json` 说明不一致或不可用于正式比较的原因。不要把缺失值当零，也不要把单组重跑批次作为第九组统计。
