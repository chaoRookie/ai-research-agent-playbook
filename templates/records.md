# 任务、证据与检查点

可直接复制 YAML 到运行目录。格式是人工/agent 交接约定，不是自动调度协议。

## 任务契约

```yaml
id: sorting-001
version: 1
goal: 返回与输入具有相同元素及重复次数的升序整数列表
constraints: [不修改输入, 不丢失元素]
acceptance:
  - 与可信排序基准比较，包含空列表、重复数、负数及随机输入
  - 正确性通过后，在同一环境与输入集上测量耗时
non_goals: [不为其他数据类型保证正确性]
budget:
  max_calls: 8
  max_concurrency: 4
  max_minutes: 30
  paid_api_authorized: false
```

## 候选证据卡

```yaml
id: candidate-a-v1
contract_version: 1
producer_instance: explorer-a
method: invariant-first
claim: 示例候选，尚未验证
assumptions: [整数列表输入]
status: proposed  # proposed / verified / rejected / inconclusive
source_ids: []
parent_candidate_ids: []
artifact: groups/a/candidate.py
evidence:
  command: python3 groups/a/check.py
  output: groups/a/check-output.txt
  executed: false
limitations: [示例路径需由实际运行产物替换]
next_check: 运行重复元素与逆序输入测试
```

证据定位必须指向实际存在的产物；不存在的测试不能标为 executed。`verified` 只能在当前版本技术验证和对齐审查均通过后使用；需另存两份审查报告，标注实例与候选版本。

## 检查点最小字段

```yaml
contract_version: 1
models: [internal-A]           # 实际使用的模型标识；未知写 unknown，不写估算
reverified_after_model_change: false
consumed: {calls: 3, minutes: 12}
in_flight: []
group_artifacts: {a: [candidate-a-v1], b: [candidate-b-v1]}
seeds_sent: [{seed: seed-1, to: a, status: proposed}]
seeds_retired: []              # 连续 2 轮无新证据的线索
failed_paths: []
pending_verification: [candidate-b-v1]
next_step: 技术验证 candidate-b-v1
```

记录 contract、candidate 的版本或内容哈希，角色实例及模型标识（如可获取）、已消费调用数/时间/实测用量、在途工作、每组已有产物、已发送种子、停用线索、失败路径、待验证候选及下一步。模型更新后读取检查点，不重新分配完整预算；**换了模型必须把 `reverified_after_model_change` 置为 false 并重验受影响结论**，不得沿用旧的通过记录。停止或崩溃后先核对在途结果，避免重复提交同一工作。

外部来源另记 URL/文件标识、获得时间、相关段落和可信度。公开分享前剔除未经授权的个人信息、凭据和私有材料。
