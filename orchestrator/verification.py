"""独立验证（P8）：子进程检查 + 双门禁。

两部分
------
1. ``run_checks``：把 ``Check`` 交给子进程执行，带超时，比较退出码与可选 stdout 子串，
   返回 ``CheckResult`` 列表。超时或无法执行 → ``inconclusive``，**不会**被算作 pass。
   非只读检查（``read_only=False``）被拒绝执行（拒绝原因写进 ``error``）。
2. 门禁：
   - ``is_verified(candidate, tech_report, align_report)``：仅当两份报告 verdict 均为
     ``pass`` **且**两份报告的 ``reviewed_version_hash`` 都等于候选**当前**哈希时返回 True。
   - ``evaluate_gate`` 返回逐条理由，便于打印"为什么被拒绝"。

另含一个最小化的对齐审查器（``AlignmentReviewer``）与契约漂移检测，用来演示
"把'正确'偷偷换成'快'"必须判 fail。

注意：本模块**不做安全隔离**。子进程与调用者同用户、同文件系统权限；路径白名单在
``isolation`` 模块，二者互不代替。
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .model import (
    AlignmentReport,
    Candidate,
    Check,
    CheckResult,
    Contract,
    ReviewVerdict,
    VerificationReport,
)

__all__ = [
    "GateDecision",
    "ContractDrift",
    "AlignmentReviewer",
    "run_check",
    "run_checks",
    "is_verified",
    "evaluate_gate",
    "detect_contract_drift",
    "checks_from_specs",
]

#: 契约文本中代表"正确性"与"速度"的标记词。
CORRECTNESS_MARKERS = ("正确", "升序", "排序结果", "反例", "相同元素", "重复次数", "多重度")
SPEED_MARKERS = ("速度", "耗时", "计时", "性能", "基准", "更快", "计时器")


# --------------------------------------------------------------------------
# 子进程检查
# --------------------------------------------------------------------------


def run_check(check: Check, default_cwd: str = "", env_extra: Optional[Mapping[str, str]] = None) -> CheckResult:
    """执行单条检查。任何**无法判定**的情况都返回 inconclusive，而不是 pass。"""

    if not check.read_only:
        return CheckResult(
            check_id=check.check_id,
            command=check.display(),
            exit_code=None,
            skipped=True,
            inconclusive_reason="非只读检查被拒绝执行（检查必须声明 read_only=True）",
            expected=_expected_text(check),
        )

    try:
        argv = check.argv()
    except ValueError as exc:
        return CheckResult(
            check_id=check.check_id,
            command=check.display(),
            exit_code=None,
            inconclusive_reason="命令无法解析: %s" % exc,
            expected=_expected_text(check),
        )

    if not argv:
        return CheckResult(
            check_id=check.check_id,
            command=check.display(),
            exit_code=None,
            inconclusive_reason="空命令",
            expected=_expected_text(check),
        )

    cwd = check.cwd or default_cwd or None
    env = None
    if check.environment or env_extra:
        env = dict(os.environ)
        env.update(check.environment)
        if env_extra:
            env.update({str(k): str(v) for k, v in env_extra.items()})

    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=float(check.timeout_seconds),
        )
    except subprocess.TimeoutExpired as exc:
        return CheckResult(
            check_id=check.check_id,
            command=check.display(),
            exit_code=None,
            stdout=_as_text(exc.stdout),
            stderr=_as_text(exc.stderr),
            duration_seconds=time.monotonic() - started,
            timed_out=True,
            inconclusive_reason="超过 %.1fs 超时" % check.timeout_seconds,
            expected=_expected_text(check),
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        return CheckResult(
            check_id=check.check_id,
            command=check.display(),
            exit_code=None,
            duration_seconds=time.monotonic() - started,
            inconclusive_reason="无法执行: %s" % exc,
            expected=_expected_text(check),
        )

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    exit_code = completed.returncode
    expected_exit = int(check.expect_exit)
    problems: List[str] = []
    if exit_code != expected_exit:
        problems.append("退出码 %s != 期望 %s" % (exit_code, expected_exit))
    if check.expect_stdout_contains is not None and check.expect_stdout_contains not in stdout:
        problems.append("stdout 未包含期望子串 %r" % check.expect_stdout_contains)

    # 判定只分两态：期望条件全部满足 -> pass；任一条件不满足 -> fail。
    # ``exit_code`` 字段是**判定用**的规范化值（0=通过、非 0=失败），真实退出码保存在
    # stdout/stderr 与 detail 中，不会被静默改写；期望非 0 退出码时按期望值取反。
    verdict_exit = 0
    if problems:
        if exit_code == expected_exit or exit_code == 0:
            verdict_exit = 1
        else:
            verdict_exit = exit_code
    return CheckResult(
        check_id=check.check_id,
        command=check.display(),
        exit_code=verdict_exit,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=time.monotonic() - started,
        detail="；".join(problems),
        expected=_expected_text(check),
    )


def run_checks(
    checks: Sequence[Check],
    default_cwd: str = "",
    env_extra: Optional[Mapping[str, str]] = None,
) -> List[CheckResult]:
    """顺序执行多条检查并返回全部结果（在本实现中没有并行执行）。"""

    return [run_check(check, default_cwd=default_cwd, env_extra=env_extra) for check in checks]


def checks_from_specs(specs: Iterable[Mapping[str, Any]], default_timeout: float = 20.0) -> List[Check]:
    """从 dict 列表构造 ``Check``，便于宿主或 JSON 契约直接给出检查清单。"""

    checks: List[Check] = []
    for index, spec in enumerate(specs):
        data = dict(spec)
        data.setdefault("id", "check-%d" % (index + 1))
        data.setdefault("timeout_seconds", default_timeout)
        checks.append(Check.from_dict(data))
    return checks


def _expected_text(check: Check) -> str:
    parts = ["exit=%s" % check.expect_exit]
    if check.expect_stdout_contains is not None:
        parts.append("stdout 含 %r" % check.expect_stdout_contains)
    return " ".join(parts)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def summarize_results(results: Sequence[CheckResult]) -> Dict[str, Any]:
    """把检查结果压成 pass/fail/inconclusive 三态判定。"""

    if not results:
        return {"verdict": "inconclusive", "reason": "没有任何检查被执行"}
    inconclusive = [r for r in results if r.inconclusive]
    failed = [r for r in results if r.failed]
    if failed:
        return {
            "verdict": "fail",
            "reason": "有 %d 条检查失败" % len(failed),
            "failed": [r.check_id for r in failed],
        }
    if inconclusive:
        return {
            "verdict": "inconclusive",
            "reason": "有 %d 条检查无法判定（超时或未执行）" % len(inconclusive),
            "inconclusive": [r.check_id for r in inconclusive],
        }
    return {"verdict": "pass", "reason": "全部 %d 条检查通过" % len(results)}


# --------------------------------------------------------------------------
# 门禁
# --------------------------------------------------------------------------


@dataclass
class GateDecision:
    """双门禁判定结果。``allowed=False`` 时 ``reasons`` 逐条说明。"""

    allowed: bool
    candidate_id: str = ""
    candidate_version_hash: str = ""
    reasons: List[str] = field(default_factory=list)
    technical_verdict: str = ""
    alignment_verdict: str = ""
    checks: Dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "candidate_id": self.candidate_id,
            "candidate_version_hash": self.candidate_version_hash,
            "technical_verdict": self.technical_verdict,
            "alignment_verdict": self.alignment_verdict,
            "checks": dict(self.checks),
            "reasons": list(self.reasons),
        }

    def explain(self) -> str:
        head = "允许标记 verified" if self.allowed else "拒绝标记 verified"
        return "%s：%s" % (head, "；".join(self.reasons) if self.reasons else "双审查通过且版本一致")


def evaluate_gate(
    candidate: Candidate,
    technical_report: Optional[VerificationReport],
    alignment_report: Optional[AlignmentReport],
) -> GateDecision:
    """逐条评估门禁。缺少任一报告都直接拒绝。"""

    current = candidate.version_hash
    reasons: List[str] = []
    checks: Dict[str, bool] = {
        "technical_report_present": technical_report is not None,
        "alignment_report_present": alignment_report is not None,
    }

    tech_verdict = technical_report.verdict.value if technical_report else "missing"
    align_verdict = alignment_report.verdict.value if alignment_report else "missing"

    if technical_report is None:
        reasons.append("缺少技术验证报告")
    if alignment_report is None:
        reasons.append("缺少目标对齐报告")

    checks["technical_verdict_pass"] = bool(
        technical_report and technical_report.verdict is ReviewVerdict.PASS
    )
    checks["alignment_verdict_pass"] = bool(
        alignment_report and alignment_report.verdict is ReviewVerdict.PASS
    )
    if technical_report is not None and technical_report.verdict is not ReviewVerdict.PASS:
        reasons.append("技术验证 verdict=%s，不是 pass" % tech_verdict)
    if alignment_report is not None and alignment_report.verdict is not ReviewVerdict.PASS:
        reasons.append("目标对齐 verdict=%s，不是 pass" % align_verdict)

    checks["technical_version_current"] = bool(
        technical_report and technical_report.reviewed_version_hash == current
    )
    checks["alignment_version_current"] = bool(
        alignment_report and alignment_report.reviewed_version_hash == current
    )
    if technical_report is not None and technical_report.reviewed_version_hash != current:
        reasons.append(
            "技术验证针对的版本 %s 与当前候选版本 %s 不一致（候选被修复后需重验）"
            % (_short(technical_report.reviewed_version_hash), _short(current))
        )
    if alignment_report is not None and alignment_report.reviewed_version_hash != current:
        reasons.append(
            "目标对齐针对的版本 %s 与当前候选版本 %s 不一致（候选被修复后需重验）"
            % (_short(alignment_report.reviewed_version_hash), _short(current))
        )

    if technical_report is not None and technical_report.candidate_id != candidate.candidate_id:
        checks["technical_candidate_matches"] = False
        reasons.append(
            "技术验证报告的对象是 %s，不是 %s" % (technical_report.candidate_id, candidate.candidate_id)
        )
    else:
        checks["technical_candidate_matches"] = technical_report is not None
    if alignment_report is not None and alignment_report.candidate_id != candidate.candidate_id:
        checks["alignment_candidate_matches"] = False
        reasons.append(
            "目标对齐报告的对象是 %s，不是 %s" % (alignment_report.candidate_id, candidate.candidate_id)
        )
    else:
        checks["alignment_candidate_matches"] = alignment_report is not None

    allowed = all(checks.values())
    if allowed:
        reasons = ["技术验证 pass、目标对齐 pass，且两份报告均针对当前版本 %s" % _short(current)]
    return GateDecision(
        allowed=allowed,
        candidate_id=candidate.candidate_id,
        candidate_version_hash=current,
        reasons=reasons,
        technical_verdict=tech_verdict,
        alignment_verdict=align_verdict,
        checks=checks,
    )


def is_verified(
    candidate: Candidate,
    technical_report: Optional[VerificationReport],
    alignment_report: Optional[AlignmentReport],
) -> bool:
    """P8 门禁：两份审查都 pass 且都针对当前候选版本，才可标记 verified。"""

    return evaluate_gate(candidate, technical_report, alignment_report).allowed


def _short(value: str) -> str:
    return value[:8] if value else "(空)"


# --------------------------------------------------------------------------
# 契约漂移与对齐审查
# --------------------------------------------------------------------------


@dataclass
class ContractDrift:
    """原始契约与候选所用契约之间的差异。"""

    acceptance_relaxed: bool = False
    acceptance_strengthened: bool = False
    missing_acceptance: List[str] = field(default_factory=list)
    added_acceptance: List[str] = field(default_factory=list)
    goal_changed: bool = False
    constraints_dropped: List[str] = field(default_factory=list)
    constraints_added: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(
            self.acceptance_relaxed
            or self.acceptance_strengthened
            or self.missing_acceptance
            or self.goal_changed
            or self.constraints_dropped
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "acceptance_relaxed": self.acceptance_relaxed,
            "acceptance_strengthened": self.acceptance_strengthened,
            "missing_acceptance": list(self.missing_acceptance),
            "added_acceptance": list(self.added_acceptance),
            "goal_changed": self.goal_changed,
            "constraints_dropped": list(self.constraints_dropped),
            "constraints_added": list(self.constraints_added),
            "notes": list(self.notes),
        }


def _marker_set(texts: Sequence[str], markers: Sequence[str]) -> List[str]:
    blob = " ".join(texts)
    return [m for m in markers if m in blob]


def detect_contract_drift(original: Contract, candidate_contract: Contract) -> ContractDrift:
    """对比两份契约，指出被删除/新增/弱化的验收项与约束。"""

    drift = ContractDrift()
    original_acc = list(original.acceptance)
    candidate_acc = list(candidate_contract.acceptance)

    original_correctness = _marker_set(original_acc, CORRECTNESS_MARKERS)
    candidate_correctness = _marker_set(candidate_acc, CORRECTNESS_MARKERS)
    original_speed = _marker_set(original_acc, SPEED_MARKERS)
    candidate_speed = _marker_set(candidate_acc, SPEED_MARKERS)

    for item in original_acc:
        if item not in candidate_acc:
            drift.missing_acceptance.append(item)
    for item in candidate_acc:
        if item not in original_acc:
            drift.added_acceptance.append(item)

    if original_correctness and not candidate_correctness:
        drift.acceptance_relaxed = True
        drift.notes.append(
            "原验收含正确性标记 %s，候选契约的验收里全部消失，而速度标记 %s 保留/新增"
            % ("/".join(original_correctness), "/".join(candidate_speed) or "无")
        )
    elif candidate_correctness and not original_correctness:
        drift.acceptance_strengthened = True
    if original_speed and not candidate_speed:
        drift.notes.append("原验收中的速度/计时要求被移除")

    if original.goal.strip() != candidate_contract.goal.strip():
        drift.goal_changed = True

    for item in original.constraints:
        if item not in candidate_contract.constraints:
            drift.constraints_dropped.append(item)
    for item in candidate_contract.constraints:
        if item not in original.constraints:
            drift.constraints_added.append(item)

    return drift


class AlignmentReviewer:
    """最小化对齐审查器：机械可比对的部分自动判定，其余必须人工显式给出。

    它只覆盖"验收标准是否被暗中改写"这一类偏差；不覆盖量词、数据划分、评价指标等
    需要人类判断的偏差。用法上，人工把结论通过 ``extra_findings`` / ``force_verdict`` 传入。
    """

    def __init__(self, reviewer_instance: str, model_id: str = "unknown") -> None:
        self.reviewer_instance = reviewer_instance
        self.model_id = model_id
        self.reviewed = 0

    def review(
        self,
        candidate: Candidate,
        original_contract: Contract,
        candidate_contract: Optional[Contract] = None,
        technical_report: Optional[VerificationReport] = None,
        report_id: Optional[str] = None,
        extra_findings: Optional[Sequence[str]] = None,
        force_verdict: Optional[ReviewVerdict] = None,
    ) -> AlignmentReport:
        """产出对齐报告。验收标准被放宽时强制 fail（除非显式传入更强结论）。"""

        self.reviewed += 1
        used_contract = candidate_contract or candidate.contract_snapshot
        drift = detect_contract_drift(original_contract, used_contract)
        findings: List[str] = list(extra_findings or [])
        verdict = ReviewVerdict.PASS
        needs_human_conclusion = False

        if drift.goal_changed:
            findings.append("契约目标文本已被改写：不得用改写后的目标替代用户原始目标")
            verdict = ReviewVerdict.FAIL
        if drift.constraints_dropped:
            findings.append("以下约束在候选所用契约中消失：%s" % "; ".join(drift.constraints_dropped))
            verdict = ReviewVerdict.FAIL
        if drift.acceptance_relaxed:
            findings.append(
                "验收标准被放宽：正确性要求消失，只剩速度/计时要求（禁止以更快替代正确）"
            )
            verdict = ReviewVerdict.FAIL
        if drift.missing_acceptance and not drift.acceptance_relaxed:
            findings.append("以下验收项被删除：%s" % "; ".join(drift.missing_acceptance))
            # 可能只是重新表述：机械比对无法判定，标记为待人工确认。
            needs_human_conclusion = True
        if technical_report is not None and technical_report.verdict is ReviewVerdict.FAIL:
            findings.append("技术验证为 fail，对齐审查不得据此宣称目标达成")
            if verdict is ReviewVerdict.PASS:
                verdict = ReviewVerdict.INCONCLUSIVE
                needs_human_conclusion = False  # 已有明确理由，不需要"缺人工结论"这条噪声

        if needs_human_conclusion:
            if verdict is ReviewVerdict.PASS:
                verdict = ReviewVerdict.INCONCLUSIVE
            if force_verdict is None:
                findings.append(
                    "缺少人工结论：机械比对无法判断重述后的验收项是否等价，"
                    "请用 force_verdict 显式给出 pass/fail/inconclusive"
                )
        if force_verdict is not None:
            forced = force_verdict if isinstance(force_verdict, ReviewVerdict) else ReviewVerdict(str(force_verdict))
            if forced is ReviewVerdict.PASS and (
                drift.acceptance_relaxed or drift.goal_changed or drift.constraints_dropped
            ):
                raise ValueError("验收标准已被削弱，不允许人工覆盖为 pass")
            verdict = forced
        if not findings:
            findings.append("目标、约束与验收标准均未被改写；技术 pass 未替代目标判定")

        return AlignmentReport(
            report_id=report_id or ("align-%s" % candidate.candidate_id),
            candidate_id=candidate.candidate_id,
            reviewed_version_hash=candidate.version_hash,
            reviewer_instance=self.reviewer_instance,
            contract_version_hash=used_contract.version_hash(),
            model_id=self.model_id,
            verdict=verdict,
            drift=[f for f in drift.notes],
            findings=findings,
            acceptance_relaxed=drift.acceptance_relaxed,
        )
