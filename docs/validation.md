# 本版本验证记录

只记录**实际执行过**的检查及其结果；没有跑过的结论不写在这里。

## 验证环境

仓库根目录为 `ai-research-agent-playbook/`，macOS，无虚拟环境、无 pip 安装、无网络访问、
无 API key。`orchestrator/` 只导入标准库与包内模块。主解释器是 `python3` = Python 3.12.0
（`/usr/local/bin/python3`），另在 Python 3.13.7 下各跑了一遍 selftest 与 demo。
**未在 3.9 / 3.10 / 3.11 上实测**：代码只用了这些版本都存在的语法与标准库，
但"应当可用"不等于"已实测可用"。

## 实际执行的命令与结果

| 命令 | 结果 |
|---|---|
| `python3 -m orchestrator selftest` | 自检通过：45/45 条断言全部通过（约 1.1s），退出码 0 |
| `python3 -m orchestrator demo` | 演示成功：81 条断言全部通过，退出码 0（十个步骤全跑完） |
| `bash demo/run_demo.sh` | 同一演示的薄包装，从仓库根目录运行，退出码 0 |
| `python3 -m orchestrator --help` | 列出 6 个子命令与示例，退出码 0 |
| `python3 -m orchestrator frobnicate` | argparse 报 invalid choice，退出码 2 |
| `python3 -m orchestrator run` / `status`（缺必需参数） | 退出码 2 |
| `run --contract run/demo/contract.json --run-dir run/manual --model-id host-model-x` | 写出 contract.json 与 checkpoint.json，退出码 0 |
| `status --run run/manual` | 打印候选、门禁、用量（实测与未知分开），退出码 0 |
| `checkpoint --run run/manual --write` | 原子刷新检查点，退出码 0 |
| `prompt --role <explorer/synthesizer/technical_verifier/alignment_reviewer> --method m` | 四类角色提示词均从 `templates/prompts.md` 渲染成功 |
| `ORCHESTRATOR_TEMPLATE=/nonexistent/prompts.md ... prompt --role explorer --method m` | 报"角色模板无法读取"，退出码 3 |
| 需求中给出的 `grep -rn "^import \|^from " orchestrator/ \| grep -v ...` | 无输出，即无第三方导入 |

演示中被真正执行的子进程检查：`python3 check.py a_v1.py b_v1.py` 在 A-v1 上报 3 处失败
（含 `[2, 1, 2] -> [2, 1, 2]` 反例），在 B-v1 上通过；`python3 check.py --presort a_v1.py`
在 A-v1 上通过——即"缺陷实现能溜过已排序样本自测"的那一步。
`python3 -m orchestrator prompt` 的退出码 3 与未知子命令的 2 都已实测。

## 它证明了什么

1. **角色隔离**：未登记路径默认拒绝；`..`、绝对路径、指向 run 目录之外的符号链接均被拒，
   拒绝原因被打印且被断言（不是只打印）。`grant()` 也拒绝越界登记。
2. **预算账本**：`admit()` 在开工前检查调用数、并发、时间上限；未授权付费调用被拒；
   超限准入抛 `BudgetExceeded` 且不产生副作用（被拒的准入不计入调用数）。
3. **种子交换**：缺来源/适用条件/证据状态/下一步检查的种子卡构造即失败；`proposed`
   种子往返序列化后仍是 `proposed`；每组上限 2 条可被机械检出。
4. **独立验证**：仅当技术验证与目标对齐都为 `pass` **且**两份报告的
   `reviewed_version_hash` 都等于候选当前哈希，`is_verified` 才为真。只给一份审查、
   版本错配、候选被修复、把"正确"换成"快"，都会在门禁处被拒并给出具体理由。
5. **检查点**：契约、预算、候选与版本哈希、检查、报告、种子、模型标识与
   `reverified_after_model_change` 全部落盘；`RunState.load` 恢复后哈希与状态不变；
   `mark_model_change` 只置标记并把已验证据列入待重验，绝不自动提升候选。

## 它**不**证明什么

- 不调用任何 LLM API，也不测量 token 或费用：这两项在 `usage_summary` 里明确标为 `unknown`，
  只报告实测到的调用数、峰值并发与耗时。
- 不证明多 agent 优于单 agent：没有相同成本、相同验收条件下的单 agent 对照实验。
- 不复现 OpenAI 的系统；这只是本仓库提出的方法整理加一个参考实现。
- 不提供任何性能对比：演示中根本没执行 B-v1 的速度测量（契约只守住"先正确才有资格计时"），
  "谁更快"未知。
- **九模式的全部参数（并发 4、最多 8 次调用、每组 2 条线索、每轮时长等）都是作者建议的默认值，
  没有做过最优性实验**；它们是可调整的起点，不是经过验证的最优配置。
- 中文角色提示词是**约定**，不是访问控制：`AccessPolicy` 只检查经过其接口的操作，真实边界取决于宿主权限。

## 已知局限与未完全强制处

- **没有真正的并发**：`Ledger` 只记录 `admit()`/`release()` 配对与峰值，`run_checks`
  顺序执行；并发安全依赖调用方。
- **子进程检查不是安全沙箱**：检查与调用者同用户、同文件系统权限，默认继承环境变量；
  `read_only` 只是声明，本包拒绝未声明的检查，但**无法**阻止一条只读检查内部自己写文件。
- **隔离只是路径白名单**：拦不住同进程内绕过本模块的直接文件 API 调用，也不防御 TOCTOU
  （解析与打开之间存在时间窗）；符号链接按解析后的目标判定。
- **对齐审查只覆盖"验收标准被改写"这一偏差**：量词、数据划分、评价指标仍需人工判断；
  机械判定为 `inconclusive` 时要求显式人工结论，而人工结论本身不被验证。
- **门禁只约束编排器自己的标记动作**：手工改写报告里的 verdict 不会被发现（无签名/哈希绑定）。
- **模型变更后的重验是标记而非执行**：`gate()` / `promote_if_verified()` 未将待重验清单纳入判定，不能单独防止沿用旧报告；宿主须检查清单并确认实际重验。
- **文件格式**：本包不依赖 PyYAML；`contract.yaml` / `checkpoint.yaml` 是与
  `docs/minimal-setup.md` 目录约定同名的**别名文件，内容同样是 JSON**。
- **一处偏离**：演示的复现夹具 `check.py` 放在 run 根目录而非某个组目录，以免验证者夹具
  被迫跨组读取；候选本体仍留在 `groups/a/`、`groups/b/`。

## 2026-09-12 文档与 skill 增量复核

在 Python 3.12.0 下重新执行 selftest（45/45）与 demo（81/81），均退出 0。
技能 quick_validate 校验通过；所有 Markdown 相对链接均指向存在的文件或目录。
使用新增 `templates/contract.example.json` 在临时目录完成 run、status、checkpoint 与
technical_verifier prompt，均退出 0；未调用任何模型或付费 API。
此次只修改技能、说明与示例契约，未改变 Python 编排器行为，也未新增效能结论。

## 如何重新验证

```bash
cd ai-research-agent-playbook
python3 -m orchestrator selftest                          # 45 条断言，失败退出非零
python3 -m orchestrator demo --run-dir run/demo --keep    # 十个步骤 + 81 条断言，产物可复查
python3 -m orchestrator --help                            # 子命令与退出码约定
grep -rn "^import \|^from " orchestrator/                 # 逐行确认无第三方依赖
```

`run/` 已在 `.gitignore` 中；`--keep` 会保留 `contract.json`、`checkpoint.json`、
`groups/`、`synthesis/`、`alignment/`、`final.md` 以便人工核对。
