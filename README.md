# AI Research Agent Playbook

**让多个 agent 走不同的路，让结论经得起独立检查。**

面向复杂研究任务的中文多智能体编排 skill：九个操作模式、四类角色提示词、预算约束、证据模板、4 个并发槽位下的完整运行配方，以及一个**不带任何 LLM 依赖、可离线跑通**的编排器——它用代码强制执行角色隔离、预算账本、交叉授粉限额和双重审查门。

来源是 OpenAI 2026 年 9 月公开的万级 agent 攻坚流程。本仓库只提炼**工作是怎么被组织的**，不讨论那个数学结论本身。

> Inspired by OpenAI's published research workflow, this Chinese-language skill turns multi-agent exploration into a bounded, auditable process. It ships role prompts, evidence records, a small-team execution recipe, and a stdlib-only orchestrator that mechanically enforces isolation and verification gates. The orchestrator calls no LLM API — it enforces the invariants, your host supplies the thinking. All parameters are author-proposed defaults, not official settings or proven optima.

## 快速使用

**当 skill 用。** 把仓库克隆到支持 skills 的工具的技能目录，并把目录命名为 `ai-research-orchestration`：

```bash
git clone https://github.com/chaoRookie/ai-research-agent-playbook.git \
  ~/.codex/skills/ai-research-orchestration
```

如果目标目录已经存在，先检查已有内容，再自行合并或更新。其他工具使用各自配置的 skill 目录。也可以直接把 [SKILL.md](SKILL.md) 及其相对链接文件作为项目说明使用。

**当编排器用。** 无需安装、无第三方依赖、不联网：

```bash
python3 -m orchestrator selftest   # 校验五条不变量，全绿才退出 0
python3 -m orchestrator demo       # 端到端演示：隔离被拒、候选被否、审查门拒绝晋升
python3 -m orchestrator --help     # run / status / checkpoint / prompt
```

示例请求：

> 使用 ai-research-orchestration，比较两条研究路径。最多 8 次调用、4 个并发槽位，不调用付费 API。先固定成功标准，再安排独立探索、综合、技术验证和目标对齐审查。证据不足时明确写出待验证项。

从 [小规模运行配方](docs/minimal-setup.md) 开始。它使用独立会话或宿主已有 agent 工具，无额外 API 依赖。

## 这个编排器做什么、不做什么

它**不**调用任何模型，因此它不会思考，也不假装会。它做的是把方法论里那些“靠自觉”的约束变成会报错的代码：

| 被强制的不变量 | 违反时的行为 |
|---|---|
| 角色隔离（P5） | 探索组 A 读 `groups/b/` 直接被拒，含 `..`、绝对路径和符号链接逃逸 |
| 预算账本 | 超出 `max_calls` / 并发 / 时间 / 未授权付费时，在准入前拒绝 |
| 交叉授粉限额（P3/P6） | 种子卡缺证据状态或超过上限即构造失败；`proposed` 不得升格 |
| 独立验证（P8） | 只有技术验证**和**对齐审查都 pass、且都针对当前候选版本哈希，才允许标 `verified` |
| 检查点（P7） | 换模型后旧证据被标记为需重验，不会静默沿用 |

已知局限写在 [验证记录](docs/validation.md) 里，包括：路径白名单不是安全沙箱、子进程检查与主进程同权限、无真实并发、也没有任何“多 agent 优于单 agent”的性能对照。

## 九模式速查

| 模式 | 用途 |
|---|---|
| P1 双向对冲 | 同时寻找支持证据与反例 |
| P2 难度阶梯 | 先验证保留核心障碍的简化问题 |
| P3 种子注入 | 复用带条件与证据的中间结果 |
| P4 动态再配置 | 按新增有效证据调整资源 |
| P5 分组隔离 | 保留不同方法路径 |
| P6 交叉授粉 | 有界交换线索并保留分歧 |
| P7 底座热升级 | 换模型时保留检查点与预算记录 |
| P8 验证闭环 | 分别检查技术正确性和目标一致性 |
| P9 溯源护栏 | 保留来源和派生关系 |

## 文件入口

- [SKILL.md](SKILL.md)：技能入口、九模式参数、规模降级映射、七条反模式与停止条件。
- [orchestrator/](orchestrator/)：stdlib-only 编排器，`python3 -m orchestrator demo` 可直接跑通。
- [角色提示词](templates/prompts.md)：探索、综合、技术验证、目标对齐。
- [记录模板](templates/records.md)：任务契约、候选证据、检查点。
- [最小配置](docs/minimal-setup.md)：调用数、并发数、目录与验收示例。
- [来源说明](references/source-notes.md)：原文事实、公开信息缺口与本仓库建议的界线。
- [验证记录](docs/validation.md)：本版本实测范围及局限。

## 来源与适用范围

参考 [OpenAI: On the Navier–Stokes Millennium Prize Problem](https://openai.com/index/navier-stokes-solution/)。这是独立的方法论整理，与 OpenAI 无隶属或背书关系。九模式分类、四类角色、降级映射与全部参数默认值由本仓库提出，**不构成对原系统的复现**，原文也未披露分组策略、提示词、交换频率或预算规则。

页面公开的规模（峰值约 10,000 并发 agent、270 万条消息、约 1,300 亿输出 token、约 88 小时出结果加 17 小时形式化验证）是本仓库的类比参照，不是可复现的目标，更不是推荐规模。

重点是 AI 使用方法，不展开数学问题的结论评判。本版本未取得交接中提及的原仓库压缩包，因此不声称继承旧提交历史。

## License

[MIT](LICENSE) · Copyright © 2026 chaoRookie。许可证覆盖本仓库原创内容；链接所指向的第三方材料遵循其各自条款。

## Topics

`ai4science` · `multi-agent` · `llm-agents` · `agent-orchestration` · `openai` · `prompt-engineering` · `research-automation`
