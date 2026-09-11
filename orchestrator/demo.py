"""端到端离线演示：不是「皆大欢喜」的故事，而是把五条不变量逐条顶到边界。

运行：``python3 -m orchestrator demo``（或任意目录下 ``bash demo/run_demo.sh``）

演示的契约：
  目标：返回与输入具有相同元素及重复次数的升序整数列表；仅在正确性通过后，才在
        同一输入集上比较速度。
  验收：候选若在已排序样本上原样返回输入即判错；必须包含反例 ``[2, 1, 2] -> [1, 2, 2]``；
        只有正确性通过后才计时。

十个步骤，每步都可能失败；任何不变量没被守住，脚本以非零码退出。
所有产物写在 run 目录下（默认临时目录；``--run-dir`` 可指定，``run/`` 已在 .gitignore 中）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import textwrap
import time
from typing import Any, Dict, List, Optional

from .budget import BudgetExceeded, Ledger
from .isolation import AccessDenied, AccessPolicy, explorer_policy
from .model import (
    AlignmentReport,
    Budget,
    Candidate,
    CandidateStatus,
    Check,
    CheckResult,
    Contract,
    EvidenceStatus,
    ReviewVerdict,
    Role,
    SeedCard,
    VerificationReport,
)
from .state import RunState, new_run
from .verification import AlignmentReviewer, run_checks, summarize_results

__all__ = ["main", "run_demo", "Demo"]

CONTRACT_ID = "sorting-001"


# --------------------------------------------------------------------------
# 演示用产物源码
# --------------------------------------------------------------------------

BUGGY_CANDIDATE = '''\
"""候选 A-v1（方法：invariant-first / 不变量优先）。

缺陷说明：只在 len<=2 时走真正的排序分支，其余长度直接返回 ``list(x)``。
已排序样本上测试全绿，但反例 [2, 1, 2] 会暴露它没有排序。
"""


def sort_list(values):
    items = list(values)
    if len(items) <= 2:
        ordered = sorted(items)
        return order_only_simple(ordered)
    return items  # 缺陷：非 <=2 的输入原样返回，未排序


def order_only_simple(items):
    """长度 <=2 时的一个「看起来像排序」的原地处理。"""

    if len(items) == 2 and items[0] > items[1]:
        items[0], items[1] = items[1], items[0]
    return list(items)
'''

CORRECT_CANDIDATE = '''\
"""候选 B-v1（方法：measure-first / 测量优先）。

契约要求「先正确、后比较速度」，因此这里先给出可复核的正确实现。
"""


def sort_list(values):
    return sorted(values)
'''

CHECK_HARNESS = '''\
"""独立的复现材料：直接跑候选的 sort_list，覆盖反例与已排序样本。

由技术验证者生成，不使用候选作者的结论。退出码 0 = 全部用例通过。

用法：``python3 check.py [--presort <候选文件>] [<候选文件> ...]``
路径按"当前工作目录"解析；``--presort`` 只检查已排序样本上的表现，
用于演示"A 在已排序样本上全绿、却在反例上失败"。
"""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

#: 演示用的两个候选文件（相对本目录）。
KNOWN = {"a": "a_v1.py", "b": "b_v1.py"}


def load(path):
    spec = importlib.util.spec_from_file_location("candidate_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_presorted(path):
    """只跑已排序样本：缺陷实现（原样返回输入）会在这里全绿。"""

    module = load(path)
    got = module.sort_list([1, 2, 3])
    ok = got == [1, 2, 3]
    print("%s 在已排序样本 [1, 2, 3] 上得到 %s -> %s" % (path, got, "通过" if ok else "失败"))
    return ok


CASES = [
    ("counterexample", [2, 1, 2], [1, 2, 2]),
    ("duplicates", [3, 1, 3, 1, 2], [1, 1, 2, 3, 3]),
    ("negatives", [-1, -5, 0, -5], [-5, -5, -1, 0]),
    ("already_sorted", [1, 2, 3], [1, 2, 3]),
    ("empty", [], []),
]

if "--presort" in sys.argv:
    index = sys.argv.index("--presort")
    targets = sys.argv[index + 1:] or [os.path.join("groups", "a", KNOWN["a"])]
    sys.exit(0 if all(check_presorted(t) for t in targets) else 1)

# 每个目标单独记账：A 的失败不得污染 B 的结论。
per_target = {}
for target in sys.argv[1:]:
    module = load(os.path.join(HERE, target))
    failures = []
    for name, payload, expected in CASES:
        snapshot = list(payload)
        got = module.sort_list(payload)
        if list(payload) != snapshot:
            failures.append("%s (%s) 修改了输入 %s" % (target, name, snapshot))
        if list(got) != expected:
            failures.append("%s (%s) 输入 %s 得到 %s，期望 %s" % (target, name, payload, got, expected))
    per_target[target] = failures
    print("%s: 运行 %d 个用例 -> %s"
          % (target, len(CASES), "正确" if not failures else "存在 %d 处失败" % len(failures)))

# 只有在两个已知候选都在命令行里时才打印对照反例，避免污染单候选检查。
if len(sys.argv) > 2 and all(KNOWN[k] in " ".join(sys.argv) for k in KNOWN):
    print("未排序反例 [2, 1, 2]：A 得 %s；B 得 %s"
          % (load(os.path.join(HERE, KNOWN["a"])).sort_list([2, 1, 2]),
             load(os.path.join(HERE, KNOWN["b"])).sort_list([2, 1, 2])))

# 退出码只反映**本次命令行给出的目标**，避免一个坏候选拖垮对好候选的验证。
failed_targets = [t for t in sys.argv[1:] if per_target.get(t)]
for target in failed_targets:
    for line in per_target[target]:
        print("FAIL " + line)
if failed_targets:
    print("失败目标: %s" % ", ".join(failed_targets))
    sys.exit(1)
print("全部用例通过")
'''

ALIGNMENT_STUB = '''\
"""目标对齐审查的人工结论占位：机械比对无法判断「重述后的验收项」是否等价。

审查者必须显式写下结论，不得默认通过。演示中 B-v1 的对齐结论为 pass，
依据是：目标、约束、验收标准均未被改写，反例要求仍在，且速度比较仍排在正确性之后。
"""

CONCLUSION = "pass"
EVIDENCE = "contract.json#acceptance 与 templates/prompts.md 的对齐审查者小节"
'''


def _pkg_block() -> str:
    return textwrap.dedent(
        '''
        import json
        import sys


        def load(path):
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)


        def run(scenario):
            data = load(scenario["payload"])
            module = load(data["candidate"])
            from orchestrator.model import Contract
            from orchestrator.verification import AlignmentReviewer

            contract = Contract.from_dict(data["original_contract"])
            if scenario["kind"] == "relaxed":
                tampered = Contract.from_dict(data["relaxed_contract"])
            else:
                from orchestrator.state import load_contract

                tampered = load_contract(scenario["candidate_contract"])
            candidate = {
                "id": "B-v1",
                "contract_id": contract.identifier,
                "producer_instance": "explorer-b",
                "method": "measure-first",
                "claim": "对整数列表返回升序列表",
                "artifact": data["candidate"],
                "contract_snapshot": contract,
                "contract_version_hash": contract.version_hash(),
            }
            from orchestrator.model import Candidate as CandidateModel

            cand = CandidateModel(**candidate)
            reviewer = AlignmentReviewer("alignment-reviewer-1")
            report = reviewer.review(cand, contract, candidate_contract=tampered)
            out = {
                "candidate": data["candidate"],
                "by": "alignment-reviewer-1",
                "sort_list_ok": module.sort_list([2, 1, 2]) == [1, 2, 2],
                "verdict": report.verdict.value,
                "acceptance_relaxed": report.acceptance_relaxed,
                "drift": list(report.drift),
                "findings": list(report.findings),
                "evidence": "contract.json + 本目录 alignment_note.py",
            }
            return out


        if __name__ == "__main__":
            print(json.dumps(run(json.loads(sys.argv[1])), ensure_ascii=False, sort_keys=True))
        '''
    ).strip()


# --------------------------------------------------------------------------
# 演示骨架
# --------------------------------------------------------------------------


class Demo:
    """带编号、断言与失败汇总的演示脚手架。"""

    def __init__(self, run_dir: str, keep: bool = False) -> None:
        self.run_dir = os.path.abspath(run_dir)
        self.keep = keep
        self.failures: List[str] = []
        self.checks_run = 0
        self.step_index = 0
        self.ledger: Optional[Ledger] = None

    # -- 输出 ------------------------------------------------------------
    def step(self, title: str) -> None:
        self.step_index += 1
        print("")
        print("=" * 72)
        print("步骤 %d. %s" % (self.step_index, title))
        print("=" * 72)

    def say(self, message: str) -> None:
        for line in str(message).splitlines() or [""]:
            print("    " + line)

    def assert_that(self, label: str, condition: bool, detail: str = "") -> bool:
        self.checks_run += 1
        mark = "PASS" if condition else "FAIL"
        suffix = ("  <- " + detail) if detail else ""
        print("    [断言 %s] %s%s" % (mark, label, suffix))
        if not condition:
            self.failures.append(label + ((" | " + detail) if detail else ""))
        return bool(condition)

    def expect(self, label: str, expected: Any, actual: Any) -> bool:
        return self.assert_that(
            label, expected == actual, "期望 %r，实际 %r" % (expected, actual)
        )

    # -- 路径 ------------------------------------------------------------
    def path(self, *parts: str) -> str:
        return os.path.join(self.run_dir, *parts)

    def write(self, relative: str, content: str) -> str:
        target = self.path(relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(content)
        return target


def _rel(run_dir: str, path: str) -> str:
    return os.path.relpath(path, run_dir)


def build_contract(budget: Budget) -> Contract:
    return Contract(
        goal="返回与输入具有相同元素及重复次数的升序整数列表；仅在正确性通过后，才在同一输入集上比较速度",
        acceptance=[
            "候选在已排序样本上原样返回输入时判为错误",
            "必须给出反例 [2, 1, 2] -> [1, 2, 2]",
            "只有全部正确性检查通过后，才在同一环境与输入集上测量耗时",
        ],
        constraints=["不修改输入列表", "不丢失元素重复次数", "不做任何网络或付费 API 调用"],
        non_goals=["不为非整数类型保证正确性", "不与官方系统比较"],
        identifier=CONTRACT_ID,
        version=1,
        budget=budget,
    )


def build_relaxed_contract(contract: Contract) -> Contract:
    """被悄悄放宽的契约：正确性要求消失，只剩"更快"和计时。"""

    return Contract(
        goal="在同一输入集上返回更快的排序实现（性能优先）",
        acceptance=[
            "在同一输入集上测量耗时并报告最快实现",
            "计时器覆盖所有样本，速度提升即视为改进",
        ],
        constraints=["不做任何网络或付费 API 调用"],
        non_goals=["不为非整数类型保证正确性"],
        identifier=CONTRACT_ID,
        version=2,
        budget=contract.budget,
    )


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def run_demo(run_dir: Optional[str] = None, keep: bool = False, quiet: bool = False) -> int:
    created_temp = False
    if run_dir is None:
        base = tempfile.mkdtemp(prefix="orchestrator-demo-")
        run_dir = os.path.join(base, "run")
        created_temp = True
    if os.path.exists(run_dir) and not keep:
        # 每次演示从干净的 run 目录开始，避免旧检查点造成假阳性
        shutil.rmtree(run_dir, ignore_errors=True)
    os.makedirs(run_dir, exist_ok=True)

    demo = Demo(run_dir, keep=keep)
    print("=" * 72)
    print("ai-research-orchestration 参考编排器 · 离线演示")
    print("本演示不调用任何 LLM API；所有命令为本地子进程，全部产物位于：")
    print("  %s" % demo.run_dir)
    print("=" * 72)

    exit_code = 1
    try:
        state = _step_1_contract_and_budget(demo)
        _step_2_explorers(state, demo)
        _step_3_isolation(state, demo)
        _step_4_synthesis(state, demo)
        _step_5_candidates(state, demo)
        _step_6_technical_verification(state, demo)
        _step_7_naive_promotion(state, demo)
        _step_8_alignment(state, demo)
        _step_9_model_change(state, demo)
        exit_code = _step_10_final(state, demo)
    except Exception as exc:  # 任何未预期异常都算演示失败
        import traceback

        demo.say("未预期异常，演示失败：")
        traceback.print_exc()
        demo.failures.append("unexpected exception: %s" % exc)
        exit_code = 1
    finally:
        if created_temp and demo.failures:
            print("")
            print("（演示失败，保留运行目录以便复查：%s）" % demo.run_dir)
        elif created_temp and not demo.keep:
            shutil.rmtree(os.path.dirname(demo.run_dir), ignore_errors=True)

    print("")
    print("=" * 72)
    if demo.failures:
        print("演示结论：失败（%d/%d 条断言未通过）" % (len(demo.failures), demo.checks_run))
        for item in demo.failures:
            print("  - " + item)
        print("退出码 1")
        return 1
    print("演示结论：成功（%d 条断言全部通过）" % demo.checks_run)
    print("注意：演示成功只说明五条不变量在本例中被执行；不证明多 agent 优于单 agent。")
    print("退出码 0")
    return 0


# -- 步骤 1 -----------------------------------------------------------------
def _step_1_contract_and_budget(demo: Demo) -> RunState:
    demo.step("契约与预算落盘；账本在开工前准入")
    budget = Budget(max_calls=8, max_concurrency=2, max_minutes=30.0, paid_api_authorized=False)
    contract = build_contract(budget)
    state = new_run(demo.run_dir, contract, budget=budget, model_id="scripted-fixture-v1")
    demo.say("契约 goal: %s" % contract.goal)
    demo.say("契约验收: %s" % " / ".join(contract.acceptance))
    demo.say("预算: max_calls=%d, max_concurrency=%d, max_minutes=%.0f, paid_api_authorized=%s"
             % (budget.max_calls, budget.max_concurrency, budget.max_minutes, budget.paid_api_authorized))
    demo.assert_that("contract.json 已落盘", os.path.exists(state.contract_path), state.contract_path)
    demo.assert_that("checkpoint.json 已落盘", os.path.exists(state.checkpoint_path), state.checkpoint_path)

    ledger = state.ledger
    demo.ledger = ledger
    seq_a = ledger.admit("explorer-a 首轮探索", calls=1, concurrency=1)
    demo.say("准入 explorer-a：占用第 %d 次调用" % seq_a)
    seq_b = ledger.admit("explorer-b 首轮探索", calls=1, concurrency=1)
    demo.say("准入 explorer-b：占用第 %d 次调用" % seq_b)
    demo.expect("两次探索各计一次调用", 2, ledger.calls_used)
    demo.expect("峰值并发为 2", 2, ledger.peak_concurrency)

    # 付费调用必须被拒
    paid_denied = False
    paid_reason = ""
    try:
        ledger.admit("付费模型调用", calls=1, concurrency=1, paid_api=True)
    except BudgetExceeded as exc:
        paid_denied = True
        paid_reason = str(exc)
    demo.assert_that("未授权付费调用被拒绝", paid_denied, paid_reason)

    # 超出并发必须被拒
    conc_denied = False
    conc_reason = ""
    try:
        ledger.admit("第三个并发探索者", calls=1, concurrency=1)
    except BudgetExceeded as exc:
        conc_denied = True
        conc_reason = str(exc)
    demo.assert_that("超过 max_concurrency 的准入被拒绝", conc_denied, conc_reason)
    demo.expect("被拒绝的准入次数", 2, ledger.admissions_refused)

    ledger.release("explorer-a 首轮探索")
    ledger.release("explorer-b 首轮探索")
    demo.say("两次探索完成后释放并发槽位：在途 %d / 上限 %d"
             % (ledger.active_concurrency, budget.max_concurrency))
    state.save(note="步骤1：契约与预算")
    return state


# -- 步骤 2 -----------------------------------------------------------------
def _step_2_explorers(state: RunState, demo: Demo) -> None:
    demo.step("两个探索者各自写入自己的组目录")
    a_dir = demo.path("groups", "a")
    b_dir = demo.path("groups", "b")
    os.makedirs(a_dir, exist_ok=True)
    os.makedirs(b_dir, exist_ok=True)

    a_artifact = demo.write("groups/a/candidate.py", BUGGY_CANDIDATE)
    b_artifact = demo.write("groups/b/candidate.py", CORRECT_CANDIDATE)
    demo.write("groups/a/notes.md", "# explorer-a\n\n方法：不变量优先。已排序样本上自测全绿。\n")
    demo.write("groups/b/notes.md", "# explorer-b\n\n方法：测量优先。先固定正确性夹具，再谈速度。\n")
    # 复现夹具属于验证者，放在 run 根目录（不属于任何探索组），候选仍留在各自组目录。
    harness = demo.write("check.py", CHECK_HARNESS)
    aliases = {}
    for name, target in (("a_v1.py", a_artifact), ("b_v1.py", b_artifact)):
        link = demo.path(name)
        if os.path.islink(link) or os.path.exists(link):
            os.unlink(link)
        os.symlink(target, link)
        aliases[name] = link

    for policy in (
        explorer_policy(demo.run_dir, "explorer-a", "a"),
        explorer_policy(demo.run_dir, "explorer-b", "b"),
    ):
        demo.say("%s 白名单: %s" % (policy.instance_id, ", ".join(policy.allowed_grants())))

    demo.assert_that("A 的产物存在", os.path.exists(a_artifact))
    demo.assert_that("B 的产物存在", os.path.exists(b_artifact))
    demo.assert_that("复现夹具存在", os.path.exists(harness))
    demo.assert_that("候选别名指向各自组目录（夹具无需跨组读取）",
                     os.path.realpath(aliases["b_v1.py"]) == os.path.realpath(b_artifact))
    demo.say("A-v1 的缺陷：只在 len<=2 时真排序，其余长度原样返回输入。")


# -- 步骤 3 -----------------------------------------------------------------
def _step_3_isolation(state: RunState, demo: Demo) -> None:
    demo.step("隔离真的会咬人：A 读 B 的目录被拒绝（断言，不是打印）")
    policy_a = explorer_policy(demo.run_dir, "explorer-a", "a")
    policy_a.grant("evidence")

    decision = policy_a.check(os.path.join("groups", "b", "candidate.py"))
    demo.say("A 请求 groups/b/candidate.py -> 允许=%s，原因=%s" % (decision.allowed, decision.reason))
    demo.assert_that("A 读取 B 组目录被拒绝", not decision.allowed, decision.reason)
    demo.expect("拒绝类型为未授权白名单", "not-granted", decision.kind)

    denied = False
    denied_message = ""
    try:
        policy_a.read(os.path.join("groups", "b", "notes.md"))
    except AccessDenied as exc:
        denied = True
        denied_message = str(exc)
    demo.assert_that("read() 抛 AccessDenied 而不是返回内容", denied, denied_message)

    # A 读自己的目录是允许的（避免"一刀切拒绝"这种假隔离）
    own = policy_a.check(os.path.join("groups", "a", "candidate.py"))
    demo.assert_that("A 读自己组目录被允许", own.allowed, own.reason)

    # .. 逃逸
    traversal = policy_a.check(os.path.join("groups", "a", "..", "..", "..", "etc", "passwd"))
    demo.say("A 请求 ../../../etc/passwd -> 允许=%s，原因=%s" % (traversal.allowed, traversal.reason))
    demo.assert_that(".. 逃逸被拒绝", not traversal.allowed, traversal.reason)

    # 绝对路径逃逸
    absolute = policy_a.check(os.path.abspath(os.path.join(demo.run_dir, "..", "outside.txt")))
    demo.assert_that("绝对路径逃逸被拒绝", not absolute.allowed, absolute.reason)
    demo.expect("拒绝类型为绝对路径逃逸", "absolute-escape", absolute.kind)

    # 符号链接逃逸
    outside_root = tempfile.mkdtemp(prefix="orchestrator-outside-")
    secret = os.path.join(outside_root, "secret.txt")
    with open(secret, "w", encoding="utf-8") as handle:
        handle.write("run 目录之外的秘密\n")
    link = demo.path("escape-link.txt")
    if os.path.islink(link) or os.path.exists(link):
        os.unlink(link)
    os.symlink(secret, link)
    symlink = policy_a.check("escape-link.txt")
    demo.say("A 请求符号链接 escape-link.txt -> 允许=%s，原因=%s" % (symlink.allowed, symlink.reason))
    demo.assert_that("符号链接逃逸被拒绝", not symlink.allowed, symlink.reason)
    demo.expect("拒绝类型为符号链接逃逸", "symlink-escape", symlink.kind)

    # grant 本身也拒绝越界登记
    grant_rejected = False
    try:
        policy_a.grant(os.path.join("..", "..", "etc"))
    except ValueError as exc:
        grant_rejected = True
        demo.say("grant('../../etc') 被拒绝：%s" % exc)
    demo.assert_that("grant 拒绝越界路径登记", grant_rejected)
    shutil.rmtree(outside_root, ignore_errors=True)


# -- 步骤 4 -----------------------------------------------------------------
def _step_4_synthesis(state: RunState, demo: Demo) -> None:
    demo.step("综合：新实例读两组目录并产出有界种子卡；缺证据状态的卡被拒")
    synthesizer = AccessPolicy(demo.run_dir, "synthesizer-1", Role.SYNTHESIZER, can_write=True)
    synthesizer.grant("contract.json")
    synthesizer.grant(os.path.join("groups", "a"))
    synthesizer.grant(os.path.join("groups", "b"))
    synthesizer.write_scopes = ["synthesis"]
    demo.say("综合者白名单: %s" % ", ".join(synthesizer.allowed_grants()))

    read_ok = True
    for relative in (
        os.path.join("groups", "a", "notes.md"),
        os.path.join("groups", "b", "notes.md"),
    ):
        try:
            synthesizer.read(relative)
            demo.say("读取成功: %s" % relative)
        except AccessDenied as exc:
            read_ok = False
            demo.say("读取失败: %s" % exc)
    demo.assert_that("综合者可以读两组产物（交叉授粉 P6 的前提）", read_ok)

    forbidden = synthesizer.check("verification")
    demo.assert_that("综合者读无关目录仍被拒绝", not forbidden.allowed, forbidden.reason)

    # 缺证据状态 -> 构造即失败
    rejected = False
    reason = ""
    try:
        SeedCard(
            seed_id="seed-bad-1",
            source="groups/a/notes.md#L1",
            condition="仅当输入长度 > 2 时",
            evidence_status="",  # type: ignore[arg-type]
            next_check="在 [2, 1, 2] 上重跑",
        )
    except ValueError as exc:
        rejected = True
        reason = str(exc)
    demo.say("构造缺证据状态的种子卡 -> %s" % reason)
    demo.assert_that("缺证据状态的种子卡被拒绝构造", rejected, reason)

    rejected_unknown = False
    try:
        SeedCard("seed-bad-2", "groups/a/notes.md#L1", "任意输入", EvidenceStatus.UNKNOWN, "重跑")
    except ValueError as exc:
        rejected_unknown = True
        reason = str(exc)
    demo.assert_that("证据状态 unknown 的种子卡被拒绝", rejected_unknown, reason)

    seeds = [
        SeedCard(
            seed_id="seed-a-1",
            source="groups/a/notes.md#L4",
            condition="长度 <=2 的输入上，A 的分支看起来正确",
            evidence_status=EvidenceStatus.PROPOSED,
            next_check="在反例 [2, 1, 2] 上执行 A-v1",
            claim="小样本上的通过不代表排序正确",
            group="a",
            target_group="a",
        ),
        SeedCard(
            seed_id="seed-b-1",
            source="groups/b/check.py#CASES",
            condition="任何整数列表输入",
            evidence_status=EvidenceStatus.PROPOSED,
            next_check="用独立夹具复现 B-v1 的全部用例",
            claim="正确性夹具可作为速度比较的前置门槛",
            group="b",
            target_group="b",
        ),
    ]
    for seed in seeds:
        state.add_seed(seed)
        demo.say("种子卡: %s" % seed.to_dict())
    demo.assert_that("未验证种子保持 proposed 标签", all(s.is_proposed() for s in state.seeds))

    # 有界性：每组最多 2 张
    overflow = list(seeds) + [
        SeedCard("seed-a-2", "groups/a/notes.md#L9", "任意输入", EvidenceStatus.PROPOSED, "重跑",
                 target_group="a"),
        SeedCard("seed-a-3", "groups/a/notes.md#L10", "任意输入", EvidenceStatus.PROPOSED, "重跑",
                 target_group="a"),
    ]
    from .model import seed_limit_violations

    within_limit = seed_limit_violations(seeds, max_per_group=2)
    violations = seed_limit_violations(overflow, max_per_group=2)
    demo.say("每组上限检查：合法集合 -> %s；超出集合 -> %s" % (within_limit, violations))
    demo.assert_that("合法种子集合不触发上限", within_limit == [])
    demo.assert_that("每组最多 2 条种子的上限可被机械检出", violations == ["a"], str(violations))
    demo.assert_that("写入 run 目录的种子只有 2 张（超限集合只用于演示检查器）",
                     len(state.seeds) == 2, str(len(state.seeds)))

    synth_path = demo.write(
        "synthesis/seeds.json",
        json.dumps([s.to_dict() for s in state.seeds], ensure_ascii=False, indent=2) + "\n",
    )
    demo.assert_that("种子已写入综合目录", os.path.exists(synth_path))
    state.save(note="步骤4：综合与种子")


# -- 步骤 5 -----------------------------------------------------------------
def _step_5_candidates(state: RunState, demo: Demo) -> None:
    demo.step("两个候选：A-v1 是经典缺陷实现，B-v1 真的正确")
    contract = state.contract
    a_path = demo.path("groups", "a", "candidate.py")
    b_path = demo.path("groups", "b", "candidate.py")

    a_v1 = Candidate(
        candidate_id="A-v1",
        contract_id=contract.identifier,
        producer_instance="explorer-a",
        method="invariant-first",
        claim="对任意长度输入返回升序列表（自称）",
        artifact=a_path,
        contract_snapshot=contract,
        assumptions=["输入为整数列表"],
        limitations=["未在长度 >2 的逆序输入上测试"],
        next_check="运行反例 [2, 1, 2]",
    )
    b_v1 = Candidate(
        candidate_id="B-v1",
        contract_id=contract.identifier,
        producer_instance="explorer-b",
        method="measure-first",
        claim="对任意整数列表返回同元素同重复次数的升序列表",
        artifact=b_path,
        contract_snapshot=contract,
        assumptions=["输入为整数列表"],
        limitations=["未处理不可比较类型"],
        next_check="在独立夹具下复现全部用例",
    )
    state.add_candidate(a_v1)
    state.add_candidate(b_v1)

    for candidate in (a_v1, b_v1):
        demo.say("%s: 状态=%s 版本哈希=%s 产物=%s"
                 % (candidate.candidate_id, candidate.status.value, candidate.version_hash,
                    _rel(demo.run_dir, candidate.artifact)))
    demo.assert_that("候选初始状态为 proposed",
                     a_v1.status is CandidateStatus.PROPOSED and b_v1.status is CandidateStatus.PROPOSED)
    demo.assert_that("两个候选的版本哈希不同", a_v1.version_hash != b_v1.version_hash)

    # 任何内容改动都要翻转哈希（修复候选 → 旧审查失效）
    repaired = Candidate(
        candidate_id="A-v1",
        contract_id=contract.identifier,
        producer_instance="explorer-a",
        method="invariant-first",
        claim="对任意长度输入返回升序列表（自称）",
        artifact=a_path,
        contract_snapshot=contract,
        limitations=["修复：去掉 len<=2 特例"],
    )
    demo.say("对 A-v1 声明做一处修复（limitations 变更不参与哈希）：%s vs %s"
             % (a_v1.version_hash, repaired.version_hash))
    hash_on_claim_change = Candidate(
        candidate_id="A-v1",
        contract_id=contract.identifier,
        producer_instance="explorer-a",
        method="invariant-first",
        claim="对任意长度输入返回升序列表（自称，已修复）",
        artifact=a_path,
        contract_snapshot=contract,
    )
    demo.assert_that("claim 改动会翻转版本哈希", hash_on_claim_change.version_hash != a_v1.version_hash)
    state.save(note="步骤5：候选")


# -- 步骤 6 -----------------------------------------------------------------
def _step_6_technical_verification(state: RunState, demo: Demo) -> None:
    demo.step("技术验证：A-v1 必须在反例上失败，B-v1 通过")
    checks = [
        Check(
            check_id="counterexample-and-invariants",
            command="python3 check.py a_v1.py b_v1.py",
            expect_exit=0,
            expect_stdout_contains="全部用例通过",
            timeout_seconds=30.0,
            description="用独立夹具跑反例 [2, 1, 2] 与已排序样本",
            cwd=demo.run_dir,
        ),
        Check(
            check_id="already-sorted-sample-only",
            command="python3 check.py --presort a_v1.py",
            expect_exit=0,
            expect_stdout_contains="在已排序样本 [1, 2, 3] 上得到 [1, 2, 3]",
            timeout_seconds=30.0,
            description="单独确认 A-v1 在已排序样本上确实是绿的（这是缺陷能溜过自测的原因）",
            cwd=demo.run_dir,
        ),
    ]
    state.add_checks(checks)
    for check in checks:
        demo.say("检查 %s: %s" % (check.check_id, check.display()))

    started = time.monotonic()
    for check in checks:
        result = run_checks([check])[0]
        demo.say("%s -> %s" % (check.check_id, result.summary()))
        if result.stdout.strip():
            demo.say("stdout: %s" % result.stdout.strip().replace("\n", " | "))
        if result.stderr.strip():
            demo.say("stderr: %s" % result.stderr.strip().replace("\n", " | "))
    elapsed = (time.monotonic() - started) / 60.0
    state.ledger.record_elapsed(elapsed)
    demo.say("检查总耗时（实测）：%.3f 秒" % (elapsed * 60))

    shared_results: List[CheckResult] = []
    for check in checks:
        shared_results.extend(run_checks([check]))
    summary = summarize_results(shared_results)
    demo.say("汇总：%s（%s）" % (summary["verdict"], summary["reason"]))
    demo.assert_that("含 A-v1 的共享夹具整体判 fail", summary["verdict"] == "fail", summary["reason"])

    a_result = run_checks(
        [
            Check(
                check_id="a-counterexample",
                command="python3 check.py a_v1.py",
                expect_exit=0,
                timeout_seconds=30.0,
                cwd=demo.run_dir,
            )
        ]
    )[0]
    b_result = run_checks(
        [
            Check(
                check_id="b-counterexample",
                command="python3 check.py b_v1.py",
                expect_exit=0,
                timeout_seconds=30.0,
                cwd=demo.run_dir,
            )
        ]
    )[0]
    demo.say("A-v1 检查: %s" % a_result.summary())
    if a_result.stderr.strip():
        demo.say("A-v1 失败行: %s" % a_result.stderr.strip().splitlines()[-1])
    demo.say("B-v1 检查: %s" % b_result.summary())
    demo.assert_that("A-v1 在反例检查上失败", a_result.failed, a_result.summary())
    demo.assert_that("B-v1 在反例检查上通过", b_result.passed, b_result.summary())

    a_v1 = state.candidates["A-v1"]
    b_v1 = state.candidates["B-v1"]
    a_report = VerificationReport(
        report_id="tech-a-v1",
        candidate_id="A-v1",
        reviewed_version_hash=a_v1.version_hash,
        reviewer_instance="technical-verifier-1",
        model_id=state.model_id,
        verdict=ReviewVerdict.FAIL,
        results=[a_result],
        notes="反例 [2, 1, 2] -> 实际返回输入本身，未排序",
        limitations=["仅覆盖整数列表"],
    )
    b_report = VerificationReport(
        report_id="tech-b-v1",
        candidate_id="B-v1",
        reviewed_version_hash=b_v1.version_hash,
        reviewer_instance="technical-verifier-1",
        model_id=state.model_id,
        verdict=ReviewVerdict.PASS,
        results=[b_result],
        notes="反例与全部用例通过；输入未被修改",
        limitations=["未覆盖非整数类型"],
    )
    state.record_verification(a_report)
    state.record_verification(b_report)
    for report in (a_report, b_report):
        demo.say("技术报告 %s: verdict=%s 版本=%s 通过=%d 失败=%d"
                 % (report.report_id, report.verdict.value, report.reviewed_version_hash[:8],
                    report.passed_checks, report.failed_checks))
    demo.assert_that("技术验证：A-v1 为 fail", a_report.verdict is ReviewVerdict.FAIL)
    demo.assert_that("技术验证：B-v1 为 pass", b_report.verdict is ReviewVerdict.PASS)
    demo.assert_that("技术报告均标注被审查的候选版本",
                     a_report.reviewed_version_hash == a_v1.version_hash
                     and b_report.reviewed_version_hash == b_v1.version_hash)
    state.save(note="步骤6：技术验证")


# -- 步骤 7 -----------------------------------------------------------------
def _step_7_naive_promotion(state: RunState, demo: Demo) -> None:
    demo.step("天真的提升尝试：门禁必须拒绝")
    a_v1 = state.candidates["A-v1"]
    b_v1 = state.candidates["B-v1"]

    # 尝试 1：只有一份"通过"的技术审查，就宣称 A-v1 verified
    forged = VerificationReport(
        report_id="tech-a-v1-forged-pass",
        candidate_id="A-v1",
        reviewed_version_hash=a_v1.version_hash,
        reviewer_instance="explorer-a（作者自称）",
        model_id=state.model_id,
        verdict=ReviewVerdict.PASS,
        results=[],
        notes="作者自测通过",
    )
    state.record_verification(forged)
    state.alignment_reports.pop("A-v1", None)
    decision = state.gate("A-v1")
    demo.say("仅有单份通过的技术审查 -> %s" % decision.explain())
    demo.assert_that("单份审查不足以上升为 verified", not decision.allowed, decision.explain())
    demo.assert_that("拒绝理由指出缺少目标对齐报告",
                     any("缺少目标对齐" in r for r in decision.reasons), str(decision.reasons))
    ok, message = state.promote_if_verified("A-v1")
    demo.assert_that("promote_if_verified 拒绝并保持 proposed",
                     (not ok) and state.candidates["A-v1"].status is CandidateStatus.PROPOSED, message)

    # 恢复 A-v1 的真实技术报告（fail）
    state.verification_reports["A-v1"] = VerificationReport(
        report_id="tech-a-v1",
        candidate_id="A-v1",
        reviewed_version_hash=a_v1.version_hash,
        reviewer_instance="technical-verifier-1",
        model_id=state.model_id,
        verdict=ReviewVerdict.FAIL,
        results=[],
        notes="反例失败",
    )

    # 尝试 2：用过期/错配的版本哈希
    stale_align = AlignmentReport(
        report_id="align-b-v1-stale",
        candidate_id="B-v1",
        reviewed_version_hash="deadbeefdeadbeef",
        reviewer_instance="alignment-reviewer-1",
        model_id=state.model_id,
        verdict=ReviewVerdict.PASS,
        findings=["（伪造的通过）"],
    )
    state.record_alignment(stale_align)
    stale_decision = state.gate("B-v1")
    demo.say("技术 pass + 版本错配的对齐 pass -> %s" % stale_decision.explain())
    demo.assert_that("版本哈希错配时门禁拒绝", not stale_decision.allowed, stale_decision.explain())

    # 尝试 3：候选修复后旧审查失效
    repaired_candidate = Candidate(
        candidate_id="B-v1",
        contract_id=b_v1.contract_id,
        producer_instance=b_v1.producer_instance,
        method=b_v1.method,
        claim=b_v1.claim + "（已修复边界处理）",
        artifact=b_v1.artifact,
        contract_snapshot=state.contract,
    )
    state.candidates["B-v1"] = repaired_candidate
    repaired_decision = state.gate("B-v1")
    demo.say("B-v1 修复后沿用旧对齐报告 -> %s" % repaired_decision.explain())
    demo.assert_that("修复候选后旧审查自动失效", not repaired_decision.allowed, repaired_decision.explain())
    demo.assert_that("门禁理由指出版本不一致",
                     any("不一致" in r for r in repaired_decision.reasons), str(repaired_decision.reasons))
    state.candidates["B-v1"] = b_v1  # 撤销这次"修复"，后续继续用原始 B-v1
    state.save(note="步骤7：天真的提升尝试被拒")


# -- 步骤 8 -----------------------------------------------------------------
def _step_8_alignment(state: RunState, demo: Demo) -> None:
    demo.step("目标对齐：B-v1 通过；把「正确」换成「快」的契约必须失败")
    contract = state.contract
    b_v1 = state.candidates["B-v1"]
    reviewer = AlignmentReviewer("alignment-reviewer-1", model_id=state.model_id)

    alignment = reviewer.review(b_v1, contract, report_id="align-b-v1")
    state.record_alignment(alignment)
    demo.write(
        "alignment/note.md",
        "结论: %s\n依据: %s\n" % (alignment.verdict.value, "; ".join(alignment.findings)),
    )
    demo.say("B-v1 对齐报告: verdict=%s 版本=%s" % (alignment.verdict.value, alignment.reviewed_version_hash[:8]))
    for finding in alignment.findings:
        demo.say("  · " + finding)
    demo.assert_that("B-v1 目标对齐 pass", alignment.verdict is ReviewVerdict.PASS)
    demo.assert_that("B-v1 双门禁允许", state.gate("B-v1").allowed)
    ok, message = state.promote_if_verified("B-v1")
    demo.assert_that("B-v1 被标记 verified", ok and state.candidates["B-v1"].status is CandidateStatus.VERIFIED, message)

    # 被悄悄放宽的契约
    relaxed = build_relaxed_contract(contract)
    tampered_alignment = reviewer.review(
        Candidate(
            candidate_id="B-speed-only",
            contract_id=relaxed.identifier,
            producer_instance="explorer-b",
            method="measure-first",
            claim="同一输入集上更快的排序实现",
            artifact=demo.path("groups", "b", "candidate.py"),
            contract_snapshot=relaxed,
        ),
        contract,
        candidate_contract=relaxed,
        report_id="align-relaxed",
    )
    for finding in tampered_alignment.findings:
        demo.say("放宽契约后的对齐审查: " + finding)
    demo.say("放宽契约后的对齐 verdict=%s acceptance_relaxed=%s"
             % (tampered_alignment.verdict.value, tampered_alignment.acceptance_relaxed))
    demo.assert_that("把正确性换成速度的契约判 fail",
                     tampered_alignment.verdict is ReviewVerdict.FAIL, tampered_alignment.verdict.value)
    demo.assert_that("契约漂移被标记为 acceptance_relaxed", tampered_alignment.acceptance_relaxed)
    demo.assert_that("验收标准被放宽时，机械审查不允许人工覆盖为 pass",
                     _forced_pass_is_rejected(reviewer, b_v1, contract, relaxed))

    # 真正危险的组合：候选**自己就用放宽后的契约**，且技术验证通过 —— 门禁仍必须拒绝。
    relaxed_candidate = Candidate(
        candidate_id="B-speed-only",
        contract_id=relaxed.identifier,
        producer_instance="explorer-b",
        method="measure-first",
        claim="同一输入集上更快的排序实现",
        artifact=demo.path("groups", "b", "candidate.py"),
        contract_snapshot=relaxed,
    )
    state.add_candidate(relaxed_candidate)
    assert tampered_alignment.candidate_id == relaxed_candidate.candidate_id
    state.record_verification(
        VerificationReport(
            report_id="tech-b-speed-only",
            candidate_id="B-speed-only",
            reviewed_version_hash=relaxed_candidate.version_hash,
            reviewer_instance="technical-verifier-1",
            model_id=state.model_id,
            verdict=ReviewVerdict.PASS,
            results=[
                CheckResult(
                    check_id="b-speed-only-timing",
                    command="python3 check.py b_v1.py",
                    exit_code=0,
                    stdout="运行 5 个用例 -> 正确\n",
                    detail="",
                    expected="exit=0",
                )
            ],
            notes="技术层面确实更快且未崩溃（这正是危险之处）",
        )
    )
    state.record_alignment(tampered_alignment)
    relaxed_decision = state.gate("B-speed-only")
    demo.say("候选自带放宽契约 + 技术 pass -> %s" % relaxed_decision.explain())
    demo.assert_that("技术 pass 无法替代目标对齐", not relaxed_decision.allowed,
                     relaxed_decision.explain())
    promoted, message = state.promote_if_verified("B-speed-only")
    demo.assert_that("放宽契约的候选最终未被标为 verified",
                     (not promoted) and relaxed_candidate.status is CandidateStatus.PROPOSED, message)
    demo.assert_that("放宽契约的对齐报告不会提升任何候选",
                     not _promote_with_report(state, "B-v1", tampered_alignment))
    demo.say("B-v1 的当前门禁（原契约，不受影响）: %s" % state.gate("B-v1").explain())

    # 两个候选都走完双审查：A-v1 的技术验证是 fail，因此对齐审查不得给它 pass。
    a_alignment = reviewer.review(state.candidates["A-v1"], contract, report_id="align-a-v1",
                                  technical_report=state.verification_reports["A-v1"])
    state.record_alignment(a_alignment)
    demo.say("A-v1 对齐报告: verdict=%s（技术验证 fail，对齐不得据此宣称目标达成）"
             % a_alignment.verdict.value)
    for finding in a_alignment.findings:
        demo.say("  · " + finding)
    state.save(note="步骤8：目标对齐与契约篡改")


def _forced_pass_is_rejected(reviewer: AlignmentReviewer, candidate: Candidate, contract: Contract,
                             relaxed: Contract) -> bool:
    try:
        reviewer.review(candidate, contract, candidate_contract=relaxed, force_verdict=ReviewVerdict.PASS)
    except ValueError:
        return True
    return False


def _promote_with_report(state: RunState, candidate_id: str, report: AlignmentReport) -> bool:
    """临时把某个对齐报告挂到候选上做门禁判定，然后恢复。"""

    original = state.alignment_reports.get(candidate_id)
    state.alignment_reports[candidate_id] = report
    try:
        ok, _ = state.promote_if_verified(candidate_id)
        return ok
    finally:
        if original is None:
            state.alignment_reports.pop(candidate_id, None)
        else:
            state.alignment_reports[candidate_id] = original


# -- 步骤 9 -----------------------------------------------------------------
def _step_9_model_change(state: RunState, demo: Demo) -> None:
    demo.step("模型变更：标记、恢复、拒绝静默沿用旧证据")
    before = _ledger_snapshot(state)
    verified_before = state.verified_candidates()
    demo.say("变更前已验证候选: %s（模型 %s）" % (verified_before, state.model_id))
    demo.assert_that("B-v1 在变更前是 verified", verified_before == ["B-v1"], str(verified_before))

    checkpoint_path = state.checkpoint(note="步骤9：模型变更之前")
    first_bytes = os.path.getsize(checkpoint_path)
    affected = state.mark_model_change("scripted-fixture-v2", reason="演示：宿主切换到新模型")
    demo.say("mark_model_change -> 模型 %s；需要重新验证: %s" % (state.model_id, affected))
    demo.assert_that("模型变更把已验证候选列入重新验证", affected == ["B-v1"], str(affected))
    demo.assert_that("模型变更后 reverification_required 为真", state.reverification_required)
    demo.assert_that("模型变更不会自动提升任何候选",
                     state.candidates["B-v1"].status is CandidateStatus.VERIFIED
                     and state.needs_reverification_for("B-v1"))
    demo.assert_that("模型变更后未做重验时 reverified_after_model_change 为假",
                     not state.reverified_after_model_change)
    state.save(note="步骤9：模型变更")

    resumed = RunState.load(demo.run_dir)
    demo.say("RunState.load(%s) 恢复成功" % demo.run_dir)
    demo.assert_that("检查点为非空文件", first_bytes > 0, "%d 字节" % first_bytes)
    demo.assert_that("恢复后候选数量一致", len(resumed.candidates) == len(state.candidates),
                     "%d vs %d" % (len(resumed.candidates), len(state.candidates)))
    demo.assert_that("恢复后模型 id 被保留", resumed.model_id == state.model_id, resumed.model_id)
    demo.assert_that("恢复后模型历史被保留", len(resumed.model_history) == len(state.model_history))
    demo.assert_that("恢复后 reverification_required 被保留", resumed.reverification_required)
    demo.assert_that("恢复后 reverified_after_model_change 被保留",
                     resumed.reverified_after_model_change == state.reverified_after_model_change,
                     str(resumed.reverified_after_model_change))
    demo.assert_that("恢复后仍要求 B-v1 重新验证", resumed.needs_reverification_for("B-v1"))
    demo.assert_that("恢复后 B-v1 不再被当作已验证（模型变更前证据不得静默沿用）",
                     "B-v1" not in resumed.verified_candidates()
                     or resumed.needs_reverification_for("B-v1"))
    after = _ledger_snapshot(resumed)
    demo.assert_that(
        "恢复后账本计数与保存前一致（时间只要求单调不减）",
        all(after[k] == before[k] for k in ("calls_used", "peak_concurrency", "admissions_refused"))
        and after["elapsed_minutes"] >= before["elapsed_minutes"],
        "保存前 %s / 恢复后 %s" % (before, after),
    )
    demo.assert_that("恢复后候选版本哈希不变",
                     resumed.candidates["B-v1"].version_hash == state.candidates["B-v1"].version_hash)
    demo.assert_that("恢复后种子卡往返一致",
                     [s.to_dict() for s in resumed.seeds] == [s.to_dict() for s in state.seeds])
    demo.assert_that("checkpoint.json 是 JSON 且无 PyYAML 依赖",
                     _is_json_file(resumed.checkpoint_path))

    resumed.clear_reverification(["B-v1"], model_id="scripted-fixture-v2")
    demo.say("用新模型重跑后显式清除标记: reverified_after_model_change=%s"
             % resumed.reverified_after_model_change)
    demo.assert_that("显式清除后 reverified_after_model_change 为真",
                     resumed.reverified_after_model_change)
    demo.assert_that("清除后不再要求 B-v1 重验", not resumed.needs_reverification_for("B-v1"))
    resumed.save(note="步骤9：重验完成")

    # 步骤 10 继续用恢复出来（而不是内存里那个）的状态，证明续跑用的是磁盘证据
    state.__dict__.update({k: v for k, v in resumed.__dict__.items() if not k.startswith("_")})


def _ledger_snapshot(state: RunState) -> Dict[str, Any]:
    return {
        "calls_used": state.ledger.calls_used,
        "peak_concurrency": state.ledger.peak_concurrency,
        "admissions_refused": state.ledger.admissions_refused,
        "elapsed_minutes": round(state.ledger.elapsed_minutes, 6),
    }


def _is_json_file(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            json.load(handle)
        return True
    except (OSError, ValueError):
        return False


# -- 步骤 10 ----------------------------------------------------------------
def _step_10_final(state: RunState, demo: Demo) -> int:
    demo.step("用量账本与最终结论")
    summary = state.ledger.usage_summary()
    for line in summary.to_lines():
        demo.say(line)
    demo.assert_that("调用数不超过 max_calls", summary.calls_used <= summary.max_calls,
                     "%d/%d" % (summary.calls_used, summary.max_calls))
    demo.assert_that("峰值并发不超过 max_concurrency",
                     summary.peak_concurrency <= summary.max_concurrency)
    demo.assert_that("token/费用被明确标为未知", len(summary.unknown) >= 2, str(summary.unknown))
    demo.assert_that("实测字段只含可测项", set(summary.measured) == {
        "calls_used", "peak_concurrency", "minutes_used", "admissions_refused"
    }, str(sorted(summary.measured)))

    lines = ["# 最终报告", ""]
    for candidate_id in sorted(state.candidates):
        candidate = state.candidates[candidate_id]
        tech = state.verification_reports.get(candidate_id)
        align = state.alignment_reports.get(candidate_id)
        decision = state.gate(candidate_id)
        lines.append("- %s 状态=%s 版本=%s" % (candidate_id, candidate.status.value,
                                               candidate.version_hash[:8]))
        lines.append("  技术验证: %s（%s）" % (tech.verdict.value if tech else "缺失",
                                              tech.reviewed_version_hash[:8] if tech else "-"))
        lines.append("  目标对齐: %s（%s）" % (align.verdict.value if align else "缺失",
                                              align.reviewed_version_hash[:8] if align else "-"))
        lines.append("  门禁: %s" % decision.explain())
    lines.append("- 用量: 调用 %d/%d，峰值并发 %d，耗时 %.3f 分钟；token/费用未知"
                 % (summary.calls_used, summary.max_calls, summary.peak_concurrency, summary.minutes_used))
    lines.append("- 未决项: A-v1 未修复；B-v1 的速度比较尚未执行（正确性已通过）")
    final_path = demo.write("final.md", "\n".join(lines) + "\n")
    demo.say("最终报告已写入: %s" % _rel(demo.run_dir, final_path))

    verdict_a = state.gate("A-v1")
    verdict_b = state.gate("B-v1")
    demo.say("A-v1 最终门禁: 允许=%s（%s）" % (verdict_a.allowed, verdict_a.reasons[:1]))
    demo.say("B-v1 最终门禁: 允许=%s（%s）" % (verdict_b.allowed, verdict_b.reasons[:1]))
    demo.assert_that("A-v1 最终没有被标记 verified",
                     state.candidates["A-v1"].status is not CandidateStatus.VERIFIED)
    demo.assert_that("B-v1 最终为 verified 且证据针对当前版本", verdict_b.allowed)
    demo.assert_that("B-v1 已重验后标记为 reverified_after_model_change",
                     state.reverified_after_model_change)
    demo.assert_that("run 目录已生成全部约定产物", all(
        os.path.exists(demo.path(*parts))
        for parts in (
            ("contract.json",),
            ("checkpoint.json",),
            ("groups", "a", "candidate.py"),
            ("groups", "b", "candidate.py"),
            ("synthesis", "seeds.json"),
            ("alignment", "note.md"),
            ("final.md",),
        )
    ))

    artifact_lines = _artifact_inventory(demo.run_dir)
    demo.say("演示产物清单:")
    for line in artifact_lines:
        demo.say("  " + line)
    return 0


def _artifact_inventory(run_dir: str) -> List[str]:
    out: List[str] = []
    for root, dirs, files in os.walk(run_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("__pycache__"))
        for name in sorted(files):
            path = os.path.join(root, name)
            if not os.path.exists(path):
                continue  # 悬空符号链接不计入产物清单
            relative = os.path.relpath(path, run_dir)
            try:
                size = os.path.getsize(path)
            except OSError:  # pragma: no cover
                size = -1
            out.append("%-42s %6d 字节" % (relative, size))
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m orchestrator demo",
        description="离线演示五条编排不变量（不调用任何 LLM）。",
    )
    parser.add_argument("--run-dir", default=None, help="运行目录；默认使用临时目录")
    parser.add_argument("--keep", action="store_true", help="保留 run 目录（默认临时目录会被清理）")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return run_demo(run_dir=args.run_dir, keep=args.keep)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
