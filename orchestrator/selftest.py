"""离线自检：用普通断言函数覆盖五条不变量。

运行：``python3 -m orchestrator selftest``（失败时退出 1 并列出失败项）

不使用 pytest 或任何第三方框架：每个测试是普通函数，注册在 ``TESTS`` 里，
``main()`` 逐个执行并打印 pass/fail 汇总。所有测试都用临时目录，互不干扰，
不访问网络，不调用 LLM。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from typing import Callable, Dict, List, Optional, Tuple

from .budget import BudgetExceeded, Ledger
from .isolation import AccessDenied, AccessPolicy, explorer_policy, synthesizer_policy
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
    compute_version_hash,
    seed_limit_violations,
)
from .roles import TemplateError, load_template, render_prompt, resolve_role, split_sections
from .state import RunState, StateError, load_contract, new_run
from .verification import (
    AlignmentReviewer,
    detect_contract_drift,
    evaluate_gate,
    is_verified,
    run_checks,
    summarize_results,
)

__all__ = ["TESTS", "main", "run_all", "run_sample_checks"]

TESTS: List[Tuple[str, Callable[[], None]]] = []


def test(func: Callable[[], None]) -> Callable[[], None]:
    TESTS.append((func.__name__, func))
    return func


# --------------------------------------------------------------------------
# 公共夹具
# --------------------------------------------------------------------------


def sample_contract(**overrides) -> Contract:
    data = {
        "goal": "返回与输入具有相同元素及重复次数的升序整数列表；仅在正确性通过后，才在同一输入集上比较速度",
        "acceptance": [
            "候选在已排序样本上原样返回输入时判为错误",
            "必须给出反例 [2, 1, 2] -> [1, 2, 2]",
            "只有全部正确性检查通过后，才在同一环境与输入集上测量耗时",
        ],
        "constraints": ["不修改输入列表", "不丢失元素重复次数"],
        "identifier": "sorting-001",
        "version": 1,
    }
    data.update(overrides)
    return Contract(**data)


def sample_candidate(contract: Optional[Contract] = None, artifact: str = "groups/a/candidate.py",
                     claim: str = "对任意整数列表返回升序列表", method: str = "invariant-first",
                     candidate_id: str = "A-v1") -> Candidate:
    contract = contract or sample_contract()
    return Candidate(
        candidate_id=candidate_id,
        contract_id=contract.identifier,
        producer_instance="explorer-a",
        method=method,
        claim=claim,
        artifact=artifact,
        contract_snapshot=contract,
    )


class TempDir:
    """上下文管理器：临时目录，退出即清理。"""

    def __enter__(self) -> str:
        self.path = tempfile.mkdtemp(prefix="orchestrator-selftest-")
        return self.path

    def __exit__(self, exc_type, exc, tb) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def sample_checks(cwd: str) -> List[Check]:
    return [
        Check(check_id="ok", command=[sys.executable, "-c", "print('ok')"],
              expect_stdout_contains="ok", cwd=cwd),
        Check(check_id="fail", command=[sys.executable, "-c", "import sys;sys.exit(1)"], cwd=cwd),
    ]


def run_sample_checks(cwd: str) -> List[CheckResult]:
    """给 demo/selftest 共用的小夹具：一条通过、一条失败。"""

    return run_checks(sample_checks(cwd))


# --------------------------------------------------------------------------
# 不变量的 1：角色隔离（P5）
# --------------------------------------------------------------------------


@test
def test_isolation_denies_path_outside_grant() -> None:
    with TempDir() as root:
        os.makedirs(os.path.join(root, "groups", "a"))
        os.makedirs(os.path.join(root, "groups", "b"))
        with open(os.path.join(root, "groups", "b", "candidate.py"), "w", encoding="utf-8") as handle:
            handle.write("print('b')\n")
        policy = explorer_policy(root, "explorer-a", "a")
        decision = policy.check(os.path.join("groups", "b", "candidate.py"))
        assert not decision.allowed, "A 不应能读 B 组"
        assert decision.kind == "not-granted", decision.kind
        try:
            policy.read(os.path.join("groups", "b", "candidate.py"))
        except AccessDenied as exc:
            assert "explorer-a" in str(exc)
        else:
            raise AssertionError("read() 应当抛 AccessDenied")


@test
def test_isolation_allows_granted_path() -> None:
    with TempDir() as root:
        os.makedirs(os.path.join(root, "groups", "a"))
        with open(os.path.join(root, "groups", "a", "notes.md"), "w", encoding="utf-8") as handle:
            handle.write("hello")
        policy = explorer_policy(root, "explorer-a", "a")
        assert policy.read(os.path.join("groups", "a", "notes.md")) == "hello"
        assert policy.check(os.path.join("groups", "a", "candidate.py")).allowed
        assert not policy.check("unlisted.txt").allowed, "未登记路径必须默认拒绝"


@test
def test_isolation_blocks_parent_traversal() -> None:
    with TempDir() as root:
        policy = explorer_policy(root, "explorer-a", "a")
        decision = policy.check(os.path.join("groups", "a", "..", "..", "..", "etc", "passwd"))
        assert not decision.allowed
        assert decision.kind == "traversal", decision.kind


@test
def test_isolation_blocks_absolute_path_escape() -> None:
    with TempDir() as root:
        policy = explorer_policy(root, "explorer-a", "a")
        decision = policy.check(os.path.join(root, "..", "outside.txt"))
        assert not decision.allowed
        assert decision.kind == "absolute-escape", decision.kind


@test
def test_isolation_blocks_symlink_escape() -> None:
    with TempDir() as root, TempDir() as outside:
        target = os.path.join(outside, "secret.txt")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("secret")
        link = os.path.join(root, "escape.txt")
        os.symlink(target, link)
        policy = explorer_policy(root, "explorer-a", "a")
        decision = policy.check("escape.txt")
        assert not decision.allowed, "符号链接必须按目标判定"
        assert decision.kind == "symlink-escape", decision.kind
        try:
            policy.read("escape.txt")
        except AccessDenied:
            pass
        else:
            raise AssertionError("符号链接逃逸应抛 AccessDenied")


@test
def test_isolation_blocks_symlinked_directory_chain() -> None:
    with TempDir() as root, TempDir() as outside:
        with open(os.path.join(outside, "data.txt"), "w", encoding="utf-8") as handle:
            handle.write("data")
        os.symlink(outside, os.path.join(root, "linked-dir"))
        policy = explorer_policy(root, "explorer-a", "a")
        decision = policy.check(os.path.join("linked-dir", "data.txt"))
        assert not decision.allowed, decision.kind
        assert decision.kind == "symlink-escape", decision.kind


@test
def test_isolation_grant_rejects_escaping_paths() -> None:
    with TempDir() as root:
        policy = AccessPolicy(root, "explorer-a", Role.EXPLORER)
        for bad in ("../etc", os.path.join("..", "..", "etc"), "/etc/passwd"):
            try:
                policy.grant(bad)
            except ValueError:
                continue
            raise AssertionError("grant 应拒绝越界路径: %s" % bad)
        policy.grant(os.path.join("groups", "a"))
        assert "groups/a" in policy.allowed_grants()


@test
def test_isolation_write_scope_enforced() -> None:
    with TempDir() as root:
        policy = explorer_policy(root, "explorer-a", "a")
        policy.write(os.path.join("groups", "a", "note.txt"), "x")
        assert os.path.exists(os.path.join(root, "groups", "a", "note.txt"))
        try:
            policy.write("verification/report.json", "{}")
        except AccessDenied:
            pass
        else:
            raise AssertionError("写范围外必须抛 AccessDenied")
        read_only = AccessPolicy(root, "verifier-1", Role.TECHNICAL_VERIFIER)
        read_only.grant("groups/a")
        try:
            read_only.write(os.path.join("groups", "a", "x.txt"), "x")
        except AccessDenied:
            pass
        else:
            raise AssertionError("未授予写权限时必须拒绝写入")


@test
def test_isolation_audit_trail_records_decisions() -> None:
    with TempDir() as root:
        policy = explorer_policy(root, "explorer-a", "a")
        policy.check(os.path.join("groups", "a", "candidate.py"))
        policy.check(os.path.join("groups", "b", "candidate.py"))
        assert len(policy.audit) == 2
        assert policy.audit[0]["allowed"] is True
        assert policy.audit[1]["allowed"] is False


# --------------------------------------------------------------------------
# 不变量 2：预算账本
# --------------------------------------------------------------------------


@test
def test_budget_admits_until_max_calls_then_refuses() -> None:
    ledger = Ledger(Budget(max_calls=2, max_concurrency=4, max_minutes=30, paid_api_authorized=False))
    ledger.record_call("a")
    ledger.record_call("b")
    assert ledger.calls_remaining == 0
    try:
        ledger.admit("c")
    except BudgetExceeded as exc:
        assert exc.dimension == "max_calls", exc.dimension
        assert "调用预算不足" in str(exc)
    else:
        raise AssertionError("超出 max_calls 必须被拒绝")
    assert ledger.calls_used == 2, "被拒绝的准入不得计入调用"
    assert ledger.admissions_refused == 1


@test
def test_budget_enforces_concurrency_before_work() -> None:
    ledger = Ledger(Budget(max_calls=8, max_concurrency=2))
    ledger.admit("a")
    ledger.admit("b")
    try:
        ledger.admit("c")
    except BudgetExceeded as exc:
        assert exc.dimension == "max_concurrency", exc.dimension
    else:
        raise AssertionError("超过并发上限必须被拒绝")
    assert ledger.peak_concurrency == 2
    ledger.release("a")
    ledger.admit("d")  # 释放后可以继续
    assert ledger.peak_concurrency == 2


@test
def test_budget_refuses_unauthorized_paid_api() -> None:
    ledger = Ledger(Budget(max_calls=8, paid_api_authorized=False))
    try:
        ledger.admit("paid-model", paid_api=True)
    except BudgetExceeded as exc:
        assert exc.dimension == "paid_api", exc.dimension
        assert "未授权付费" in str(exc)
    else:
        raise AssertionError("未授权付费调用必须被拒绝")
    authorized = Ledger(Budget(max_calls=8, paid_api_authorized=True))
    assert authorized.admit("paid-model", paid_api=True) == 1


class FakeClock:
    """可控时钟：minutes 表示"已经过去多少分钟"。"""

    def __init__(self, minutes: float = 0.0) -> None:
        self.minutes = float(minutes)

    def __call__(self) -> float:
        return self.minutes * 60.0

    def advance(self, minutes: float) -> None:
        self.minutes += float(minutes)


@test
def test_budget_time_limit_refuses_new_work() -> None:
    clock = FakeClock()
    ledger = Ledger(Budget(max_calls=8, max_minutes=10), clock=clock)
    assert ledger.admit("early") == 1, "时间未到时应当准入"
    clock.advance(11)
    assert ledger.minutes_used > 10, ledger.minutes_used
    try:
        ledger.admit("late")
    except BudgetExceeded as exc:
        assert exc.dimension == "max_minutes", exc.dimension
    else:
        raise AssertionError("时间上限已到时必须拒绝新工作")
    assert ledger.calls_used == 1, "被时间上限拒绝的准入不得计入调用"


@test
def test_budget_summary_separates_measured_from_unknown() -> None:
    ledger = Ledger(Budget(max_calls=4, max_concurrency=2, max_minutes=5))
    ledger.record_call("a", minutes=1.5)
    summary = ledger.usage_summary()
    assert summary.calls_used == 1 and summary.calls_remaining == 3
    assert summary.peak_concurrency == 1
    assert abs(summary.minutes_used - 1.5) < 0.05, summary.minutes_used
    assert summary.unknown, "必须列出未知项"
    assert any("token" in item for item in summary.unknown)
    assert any("费用" in item for item in summary.unknown)
    assert "calls_used" in summary.measured
    assert "token" not in " ".join(summary.measured)


@test
def test_budget_ledger_round_trips_through_checkpoint() -> None:
    with TempDir() as root:
        contract = sample_contract()
        state = new_run(root, contract, model_id="fixture-v1")
        state.ledger.record_call("explorer-a")
        state.ledger.record_call("explorer-b")
        state.save()
        resumed = RunState.load(root)
        assert resumed.ledger.calls_used == 2
        assert resumed.ledger.budget.max_calls == contract.budget.max_calls
        assert resumed.ledger.budget.paid_api_authorized is False


# --------------------------------------------------------------------------
# 不变量 3：种子交换（P3/P6）
# --------------------------------------------------------------------------


@test
def test_seed_card_requires_all_bounded_fields() -> None:
    for kwargs in (
        {"source": ""},
        {"condition": ""},
        {"next_check": ""},
        {"seed_id": ""},
    ):
        data = {
            "seed_id": "seed-1",
            "source": "groups/a/notes.md#L1",
            "condition": "任意整数列表",
            "evidence_status": EvidenceStatus.PROPOSED,
            "next_check": "在 [2, 1, 2] 上重跑",
        }
        data.update(kwargs)
        try:
            SeedCard(**data)
        except ValueError:
            continue
        raise AssertionError("缺项种子卡必须被拒绝: %s" % kwargs)


@test
def test_seed_card_rejects_missing_or_unknown_evidence_status() -> None:
    for status in ("", "unknown", EvidenceStatus.UNKNOWN):
        try:
            SeedCard("seed-x", "src", "cond", status, "next")
        except ValueError:
            continue
        raise AssertionError("证据状态缺失或 unknown 必须被拒绝: %r" % (status,))


@test
def test_seed_card_refuted_requires_refutation_target() -> None:
    try:
        SeedCard("seed-r", "src", "cond", EvidenceStatus.REFUTED, "next")
    except ValueError:
        pass
    else:
        raise AssertionError("refuted 种子必须写明被推翻对象")
    card = SeedCard("seed-r", "src", "cond", EvidenceStatus.REFUTED, "next", refutes="A-v1")
    assert "A-v1" in card.to_dict()["refutes"]


@test
def test_proposed_seed_stays_labeled_proposed_after_restatement() -> None:
    card = SeedCard(
        "seed-p",
        "groups/a/notes.md#L1",
        "长度 <=2 的输入",
        EvidenceStatus.PROPOSED,
        "在反例上执行 A-v1",
        claim="小样本通过不代表排序正确",
    )
    assert card.is_proposed()
    assert card.label() == "proposed"
    assert card.to_dict()["evidence_status"] == "proposed"
    again = SeedCard.from_dict(card.to_dict())
    assert again.evidence_status is EvidenceStatus.PROPOSED, "往返不得提升证据状态"


@test
def test_seed_bounded_per_group() -> None:
    cards = [
        SeedCard("s1", "src", "cond", EvidenceStatus.PROPOSED, "next", target_group="a"),
        SeedCard("s2", "src", "cond", EvidenceStatus.PROPOSED, "next", target_group="a"),
        SeedCard("s3", "src", "cond", EvidenceStatus.PROPOSED, "next", target_group="a"),
        SeedCard("s4", "src", "cond", EvidenceStatus.PROPOSED, "next", target_group="b"),
    ]
    assert seed_limit_violations(cards, max_per_group=2) == ["a"]
    assert seed_limit_violations(cards[:2], max_per_group=2) == []


@test
def test_synthesizer_can_read_both_groups_but_not_unrelated_dirs() -> None:
    with TempDir() as root:
        os.makedirs(os.path.join(root, "groups", "a"))
        os.makedirs(os.path.join(root, "groups", "b"))
        os.makedirs(os.path.join(root, "alignment"))
        policy = synthesizer_policy(root, "synthesizer-1", ["a", "b"])
        assert policy.check(os.path.join("groups", "a", "notes.md")).allowed
        assert policy.check(os.path.join("groups", "b", "notes.md")).allowed
        assert not policy.check(os.path.join("alignment", "note.md")).allowed


# --------------------------------------------------------------------------
# 不变量 4：独立验证（P8）
# --------------------------------------------------------------------------


@test
def test_checks_capture_exit_codes_and_stdout() -> None:
    with TempDir() as root:
        results = run_sample_checks(root)
        by_id: Dict[str, CheckResult] = {r.check_id: r for r in results}
        assert by_id["ok"].passed, by_id["ok"].summary()
        assert by_id["fail"].failed, by_id["fail"].summary()
        assert "退出码" in by_id["fail"].detail
        assert summarize_results(results)["verdict"] == "fail"


@test
def test_checks_mark_timeout_and_skipped_as_inconclusive() -> None:
    with TempDir() as root:
        timeout = Check(check_id="slow", command=[sys.executable, "-c", "import time;time.sleep(5)"],
                        timeout_seconds=1, cwd=root)
        result = run_checks([timeout])[0]
        assert result.inconclusive and result.timed_out
        assert not result.passed, "超时不得算通过"
        non_readonly = Check(check_id="writes", command=[sys.executable, "-c", "pass"], cwd=root,
                            read_only=False)
        skipped = run_checks([non_readonly])[0]
        assert skipped.inconclusive and skipped.skipped
        assert summarize_results([result])["verdict"] == "inconclusive"


@test
def test_check_requires_expected_substring() -> None:
    with TempDir() as root:
        check = Check(check_id="substr", command=[sys.executable, "-c", "print('hello')"],
                      expect_stdout_contains="goodbye", cwd=root)
        result = run_checks([check])[0]
        assert result.failed, result.summary()
        assert "未包含" in result.detail


@test
def test_gate_requires_both_reports() -> None:
    candidate = sample_candidate()
    tech = VerificationReport("tech", candidate.candidate_id, candidate.version_hash, "verifier-1",
                              verdict=ReviewVerdict.PASS)
    decision = evaluate_gate(candidate, tech, None)
    assert not decision.allowed
    assert any("缺少目标对齐报告" in r for r in decision.reasons)
    assert not is_verified(candidate, tech, None)


@test
def test_gate_requires_both_verdicts_pass() -> None:
    candidate = sample_candidate()
    tech = VerificationReport("tech", candidate.candidate_id, candidate.version_hash, "verifier-1",
                              verdict=ReviewVerdict.FAIL)
    align = AlignmentReport("align", candidate.candidate_id, candidate.version_hash, "reviewer-1",
                            verdict=ReviewVerdict.PASS)
    assert not is_verified(candidate, tech, align)
    inconclusive = AlignmentReport("align2", candidate.candidate_id, candidate.version_hash,
                                   "reviewer-1", verdict=ReviewVerdict.INCONCLUSIVE)
    good_tech = VerificationReport("tech2", candidate.candidate_id, candidate.version_hash,
                                   "verifier-1", verdict=ReviewVerdict.PASS)
    assert not is_verified(candidate, good_tech, inconclusive)
    passing = AlignmentReport("align3", candidate.candidate_id, candidate.version_hash, "reviewer-1",
                              verdict=ReviewVerdict.PASS)
    assert is_verified(candidate, good_tech, passing)


@test
def test_gate_rejects_stale_version_hash() -> None:
    candidate = sample_candidate()
    stale = "0" * 16
    tech = VerificationReport("tech", candidate.candidate_id, stale, "verifier-1", verdict=ReviewVerdict.PASS)
    align = AlignmentReport("align", candidate.candidate_id, candidate.version_hash, "reviewer-1",
                            verdict=ReviewVerdict.PASS)
    decision = evaluate_gate(candidate, tech, align)
    assert not decision.allowed
    assert any("不一致" in r for r in decision.reasons), decision.reasons


@test
def test_repaired_candidate_invalidates_previous_reviews() -> None:
    contract = sample_contract()
    candidate = sample_candidate(contract)
    tech = VerificationReport("tech", candidate.candidate_id, candidate.version_hash, "verifier-1",
                              verdict=ReviewVerdict.PASS)
    align = AlignmentReport("align", candidate.candidate_id, candidate.version_hash, "reviewer-1",
                            verdict=ReviewVerdict.PASS)
    assert is_verified(candidate, tech, align)
    repaired = sample_candidate(contract, claim="对任意整数列表返回升序列表（已修复边界）")
    assert repaired.version_hash != candidate.version_hash
    assert not is_verified(repaired, tech, align), "修复后旧审查必须失效"


@test
def test_gate_rejects_report_for_other_candidate() -> None:
    candidate = sample_candidate()
    other = sample_candidate(candidate_id="B-v1")
    tech = VerificationReport("tech", "B-v1", other.version_hash, "verifier-1", verdict=ReviewVerdict.PASS)
    align = AlignmentReport("align", candidate.candidate_id, candidate.version_hash, "reviewer-1",
                            verdict=ReviewVerdict.PASS)
    decision = evaluate_gate(candidate, tech, align)
    assert not decision.allowed
    assert any("对象是 B-v1" in r for r in decision.reasons), decision.reasons


# --------------------------------------------------------------------------
# 不变量 5：检查点与模型变更（P7）
# --------------------------------------------------------------------------


@test
def test_version_hash_covers_contract_and_candidate() -> None:
    contract = sample_contract()
    candidate = sample_candidate(contract)
    changed_claim = sample_candidate(contract, claim="另一个主张")
    changed_method = sample_candidate(contract, method="measure-first")
    changed_contract = sample_contract(acceptance=["只要更快就算成功"])

    assert candidate.version_hash != changed_claim.version_hash
    assert candidate.version_hash != changed_method.version_hash
    assert candidate.version_hash != sample_candidate(changed_contract).version_hash
    assert candidate.version_hash != compute_version_hash(contract, candidate.claim,
                                                          "groups/z/candidate.py", candidate.method)
    assert len(candidate.version_hash) == 16 and candidate.version_hash == sample_candidate(contract).version_hash


@test
def test_model_change_forces_reverification_and_never_promotes() -> None:
    with TempDir() as root:
        state = new_run(root, sample_contract(), model_id="fixture-v1")
        candidate = state.add_candidate(sample_candidate())
        state.record_verification(
            VerificationReport("tech", candidate.candidate_id, candidate.version_hash, "verifier-1",
                               model_id="fixture-v1", verdict=ReviewVerdict.PASS)
        )
        state.record_alignment(
            AlignmentReport("align", candidate.candidate_id, candidate.version_hash, "reviewer-1",
                            model_id="fixture-v1", verdict=ReviewVerdict.PASS)
        )
        ok, message = state.promote_if_verified(candidate.candidate_id)
        assert ok, message
        assert state.candidates["A-v1"].status is CandidateStatus.VERIFIED

        affected = state.mark_model_change("fixture-v2", reason="宿主换模型")
        assert affected == ["A-v1"], affected
        assert state.reverification_required
        assert state.needs_reverification_for("A-v1")
        assert not state.reverified_after_model_change
        assert state.model_history[-1]["from"] == "fixture-v1"
        assert state.model_history[-1]["to"] == "fixture-v2"
        assert state.candidates["A-v1"].status is CandidateStatus.VERIFIED, "模型变更不得改写状态字段"


@test
def test_checkpoint_round_trips_through_fresh_load() -> None:
    with TempDir() as root:
        contract = sample_contract()
        state = new_run(root, contract, model_id="fixture-v1")
        candidate = state.add_candidate(sample_candidate(contract))
        state.add_checks([Check(check_id="ok", command=[sys.executable, "-c", "print(1)"])])
        state.record_verification(
            VerificationReport("tech", candidate.candidate_id, candidate.version_hash, "verifier-1",
                               verdict=ReviewVerdict.PASS,
                               results=[CheckResult("ok", "python3 -c print(1)", 0, stdout="1\n")])
        )
        state.record_alignment(
            AlignmentReport("align", candidate.candidate_id, candidate.version_hash, "reviewer-1",
                            verdict=ReviewVerdict.PASS)
        )
        state.add_seed(SeedCard("s1", "src", "cond", EvidenceStatus.PROPOSED, "next"))
        ok, message = state.promote_if_verified("A-v1")
        assert ok, message
        assert state.verified_candidates() == ["A-v1"]
        state.sent_seeds = ["s1"]
        state.in_flight = [{"instance": "explorer-a", "scope": "groups/a"}]
        state.mark_model_change("fixture-v2")
        path = state.save(note="selftest")
        assert os.path.getsize(path) > 0

        resumed = RunState.load(root)
        assert resumed.contract.version_hash() == contract.version_hash()
        assert set(resumed.candidates) == {"A-v1"}
        assert resumed.candidates["A-v1"].version_hash == candidate.version_hash
        assert os.path.isabs(resumed.candidates["A-v1"].artifact)
        assert resumed.model_id == "fixture-v2"
        assert resumed.model_history == state.model_history
        assert resumed.reverification_required and resumed.needs_reverification_for("A-v1")
        assert resumed.sent_seeds == ["s1"] and resumed.in_flight == state.in_flight
        assert [s.to_dict() for s in resumed.seeds] == [s.to_dict() for s in state.seeds]
        assert resumed.verification_reports["A-v1"].verdict is ReviewVerdict.PASS
        assert resumed.alignment_reports["A-v1"].verdict is ReviewVerdict.PASS
        assert resumed.gate("A-v1").allowed, "版本一致时门禁仍应允许"
        resumed.clear_reverification(["A-v1"], model_id="fixture-v2")
        assert resumed.reverified_after_model_change
        assert not resumed.needs_reverification_for("A-v1")
        payload = json.loads(open(path, "r", encoding="utf-8").read())
        assert payload["schema_version"] == 1
        assert "verification_reports" in payload and "ledger" in payload


@test
def test_checkpoint_without_yaml_dependency() -> None:
    with TempDir() as root:
        state = new_run(root, sample_contract())
        fingerprint = "yaml" not in "".join(
            name.lower() for name in sys.modules if "yaml" in name.lower()
        )
        assert fingerprint, "本包不得依赖 PyYAML"
        source_files = []
        package_dir = os.path.dirname(os.path.abspath(__file__))
        for name in sorted(os.listdir(package_dir)):
            if name.endswith(".py"):
                source_files.append(os.path.join(package_dir, name))
        banned = (
            "import " + "yaml",
            "import " + "numpy",
            "import " + "pandas",
            "import " + "requests",
            "import " + "pytest",
        )
        for path in source_files:
            text = open(path, "r", encoding="utf-8").read()
            for needle in banned:
                assert needle not in text, "%s 包含被禁止的导入: %s" % (path, needle)
        assert os.path.exists(state.checkpoint_path)


@test
def test_load_missing_or_broken_checkpoint_raises() -> None:
    with TempDir() as root:
        try:
            RunState.load(root)
        except StateError as exc:
            assert "找不到检查点" in str(exc)
        else:
            raise AssertionError("缺失检查点必须抛 StateError")
        with open(os.path.join(root, "checkpoint.json"), "w", encoding="utf-8") as handle:
            handle.write("{不是 JSON")
        try:
            RunState.load(root)
        except StateError as exc:
            assert "无法解析" in str(exc)
        else:
            raise AssertionError("损坏检查点必须抛 StateError")


@test
def test_load_contract_reads_json_and_reports_missing_file() -> None:
    with TempDir() as root:
        contract = sample_contract()
        path = os.path.join(root, "contract.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(contract.to_dict(), handle, ensure_ascii=False)
        loaded = load_contract(path)
        assert loaded.version_hash() == contract.version_hash()
        try:
            load_contract(os.path.join(root, "nope.json"))
        except StateError as exc:
            assert "找不到契约文件" in str(exc)
        else:
            raise AssertionError("缺失契约必须抛 StateError")


# --------------------------------------------------------------------------
# 数据模型与模板
# --------------------------------------------------------------------------


@test
def test_candidate_round_trip_preserves_hash_and_status() -> None:
    candidate = sample_candidate()
    candidate.status = CandidateStatus.INCONCLUSIVE
    data = candidate.to_dict()
    again = Candidate.from_dict(data)
    assert again.version_hash == candidate.version_hash
    assert again.status is CandidateStatus.INCONCLUSIVE
    assert again.to_dict() == data


@test
def test_contract_round_trip_and_budget_validation() -> None:
    contract = sample_contract(budget=Budget(max_calls=8, max_concurrency=4, max_minutes=30))
    again = Contract.from_dict(contract.to_dict())
    assert again.version_hash() == contract.version_hash()
    for bad in ({"max_calls": -1}, {"max_concurrency": 0}, {"max_minutes": -1}):
        kwargs = {"max_calls": 1, "max_concurrency": 1, "max_minutes": 1}
        kwargs.update(bad)
        try:
            Budget(**kwargs)
        except ValueError:
            continue
        raise AssertionError("非法预算必须被拒绝: %s" % bad)


@test
def test_candidate_requires_claim_and_id() -> None:
    for kwargs in ({"candidate_id": ""}, {"claim": "  "}):
        try:
            sample_candidate(**kwargs)
        except ValueError:
            continue
        raise AssertionError("非法候选必须被拒绝: %s" % kwargs)


@test
def test_contract_drift_detection() -> None:
    original = sample_contract()
    relaxed = Contract(
        goal="更快地排序",
        acceptance=["在同一输入集上测量耗时并报告最快实现"],
        constraints=["不修改输入列表"],
        identifier=original.identifier,
    )
    drift = detect_contract_drift(original, relaxed)
    assert drift.acceptance_relaxed
    assert drift.goal_changed
    assert drift.constraints_dropped == ["不丢失元素重复次数"]
    assert drift.missing_acceptance
    clean = detect_contract_drift(original, Contract.from_dict(original.to_dict()))
    assert not clean.any, clean.to_dict()


@test
def test_alignment_reviewer_fails_relaxed_acceptance() -> None:
    original = sample_contract()
    relaxed = Contract(goal="更快地排序", acceptance=["报告最快实现"], identifier=original.identifier)
    reviewer = AlignmentReviewer("reviewer-1")
    report = reviewer.review(sample_candidate(original), original, candidate_contract=relaxed)
    assert report.verdict is ReviewVerdict.FAIL
    assert report.acceptance_relaxed
    assert report.findings
    try:
        reviewer.review(sample_candidate(original), original, candidate_contract=relaxed,
                        force_verdict=ReviewVerdict.PASS)
    except ValueError:
        pass
    else:
        raise AssertionError("放宽验收标准后不得被人工覆盖为 pass")


@test
def test_alignment_report_cannot_claim_pass_while_relaxed() -> None:
    try:
        AlignmentReport("align", "A-v1", "hash", "reviewer-1", verdict=ReviewVerdict.PASS,
                        acceptance_relaxed=True)
    except ValueError:
        pass
    else:
        raise AssertionError("自报放宽却给 pass 的报告必须被拒绝")


@test
def test_role_prompts_render_from_template() -> None:
    text = load_template()
    sections = split_sections(text)
    assert "探索者" in " ".join(sections)
    for role, parameters in (
        (Role.EXPLORER, {"method": "invariant-first", "contract": "contract.json", "seeds": "无",
                         "budget": "1 次调用"}),
        (Role.SYNTHESIZER, {"contract": "contract.json", "records": "groups/", "budget": "1 次调用",
                            "seed_limit": 2}),
        (Role.TECHNICAL_VERIFIER, {"contract": "contract.json", "candidate": "groups/a/candidate.py",
                                   "evidence": "groups/b/check.py", "budget": "1 次调用"}),
        (Role.ALIGNMENT_REVIEWER, {"contract": "contract.json", "candidate": "groups/a/candidate.py",
                                   "verification": "verification/", "budget": "1 次调用"}),
    ):
        rendered = render_prompt(role, parameters)
        assert rendered.text and "{" not in rendered.text, "渲染后不应残留占位符: %s" % rendered.text[:80]
        assert rendered.section_title
        for value in parameters.values():
            assert str(value) in rendered.text or str(value) == "2", rendered.text[:120]


@test
def test_role_prompt_missing_parameter_raises_template_error() -> None:
    try:
        render_prompt(Role.EXPLORER, {"contract": "c", "seeds": "无", "budget": "1"})
    except TemplateError as exc:
        assert "method" in str(exc)
    else:
        raise AssertionError("缺占位符必须抛 TemplateError")


@test
def test_role_template_degrades_gracefully() -> None:
    try:
        render_prompt("不存在的角色", {})
    except TemplateError as exc:
        assert "未知角色" in str(exc)
    else:
        raise AssertionError("未知角色必须抛 TemplateError")
    try:
        render_prompt(Role.EXPLORER, {"method": "m"}, template_text="没有二级标题的文本")
    except TemplateError as exc:
        assert "小节" in str(exc)
    else:
        raise AssertionError("无法解析的模板必须抛 TemplateError")
    assert resolve_role("explorer") is Role.EXPLORER
    assert resolve_role("technical_verifier") is Role.TECHNICAL_VERIFIER


@test
def test_isolation_and_gate_end_to_end_without_llm() -> None:
    """把隔离与门禁串起来跑一遍，但只做一次真实子进程检查。"""

    with TempDir() as root:
        state = new_run(root, sample_contract(), model_id="fixture-v1")
        candidate_dir = os.path.join(root, "groups", "a")
        os.makedirs(candidate_dir, exist_ok=True)
        artifact = os.path.join(candidate_dir, "candidate.py")
        with open(artifact, "w", encoding="utf-8") as handle:
            handle.write("def sort_list(values):\n    return sorted(values)\n")
        runner = os.path.join(root, "run_check.py")
        with open(runner, "w", encoding="utf-8") as handle:
            handle.write(
                "import importlib.util, sys\n"
                "spec = importlib.util.spec_from_file_location('m', sys.argv[1])\n"
                "module = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(module)\n"
                "sys.exit(0 if module.sort_list([2, 1, 2]) == [1, 2, 2] else 1)\n"
            )
        candidate = state.add_candidate(sample_candidate(artifact=artifact))
        check = Check(
            check_id="counterexample",
            command=[sys.executable, runner, artifact],
            cwd=root,
        )
        result = run_checks([check])[0]
        assert result.passed, result.summary()
        state.record_verification(
            VerificationReport("tech", candidate.candidate_id, candidate.version_hash, "verifier-1",
                               verdict=ReviewVerdict.PASS, results=[result])
        )
        assert not is_verified(candidate, state.verification_reports["A-v1"], None)
        reviewer = AlignmentReviewer("reviewer-1")
        state.record_alignment(reviewer.review(candidate, state.contract))
        ok, message = state.promote_if_verified("A-v1")
        assert ok, message
        assert state.verified_candidates() == ["A-v1"]
        policy = explorer_policy(root, "explorer-a", "a")
        assert policy.check(os.path.join("groups", "a", "candidate.py")).allowed
        assert not policy.check(os.path.join("groups", "b", "candidate.py")).allowed


# --------------------------------------------------------------------------
# 运行器
# --------------------------------------------------------------------------


def run_all(quiet: bool = False) -> int:
    failures: List[Tuple[str, str]] = []
    started = time.monotonic()
    if not quiet:
        print("运行 %d 条离线断言（纯标准库，无网络，无 LLM 调用）" % len(TESTS))
        print("-" * 68)
    for name, func in TESTS:
        began = time.monotonic()
        try:
            func()
        except Exception:
            detail = traceback.format_exc(limit=3).strip().splitlines()
            failures.append((name, " | ".join(detail[-2:])))
            print("[FAIL] %-58s %.3fs" % (name, time.monotonic() - began))
            if quiet:
                print("       " + "\n       ".join(detail))
        else:
            if not quiet:
                print("[PASS] %-58s %.3fs" % (name, time.monotonic() - began))
    total = len(TESTS)
    elapsed = time.monotonic() - started
    print("-" * 68)
    if failures:
        print("自检失败：%d/%d 条断言未通过（耗时 %.2fs）" % (len(failures), total, elapsed))
        for name, detail in failures:
            print("  - %s: %s" % (name, detail))
        return 1
    print("自检通过：%d/%d 条断言全部通过（耗时 %.2fs）" % (total, total, elapsed))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    quiet = False
    args = list(argv if argv is not None else sys.argv[1:])
    if "--quiet" in args:
        quiet = True
    return run_all(quiet=quiet)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
