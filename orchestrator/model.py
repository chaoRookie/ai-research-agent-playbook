"""数据模型：契约、预算、候选、检查、审查报告、种子卡。

设计要点
--------
* 每个 ``Candidate`` 有一个稳定的内容哈希 ``version_hash``。它把
  **契约投影**（goal + acceptance + constraints）与
  **候选投影**（claim + artifact + method）一起哈希。
  任何一侧改动都会翻转哈希，因此"放宽验收标准后沿用旧审查"必然失配。
* ``SeedCard`` 在构造时就做有界性与完整性校验：缺来源、缺适用条件、
  缺证据状态、缺下一步检查都直接 ``ValueError``，不会静默通过。
* 所有对象都有 ``to_dict`` / ``from_dict`` 往返，且往返后哈希不变。

纯标准库；不含任何 LLM 调用。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

__all__ = [
    "MODEL_UNKNOWN",
    "hash_text",
    "compute_version_hash",
    "project_contract",
    "project_candidate",
    "Contract",
    "Budget",
    "Candidate",
    "CandidateStatus",
    "Check",
    "CheckResult",
    "VerificationReport",
    "AlignmentReport",
    "ReviewVerdict",
    "Role",
    "SeedCard",
    "EvidenceStatus",
]


# --------------------------------------------------------------------------
# 枚举
# --------------------------------------------------------------------------


class CandidateStatus(str, Enum):
    """候选状态。只有双审查都通过且针对当前版本时才允许 ``VERIFIED``。"""

    PROPOSED = "proposed"
    VERIFIED = "verified"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


class ReviewVerdict(str, Enum):
    """单项审查结论。技术 pass 不自动代表目标对齐 pass。"""

    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class Role(str, Enum):
    """四类角色。同一实例换提示词不构成独立验证。"""

    EXPLORER = "explorer"
    SYNTHESIZER = "synthesizer"
    TECHNICAL_VERIFIER = "technical_verifier"
    ALIGNMENT_REVIEWER = "alignment_reviewer"


class EvidenceStatus(str, Enum):
    """种子卡证据状态。未经验证的种子必须保留 ``PROPOSED`` 标签。"""

    PROPOSED = "proposed"
    SUPPORTED = "supported"
    REFUTED = "refuted"
    UNKNOWN = "unknown"


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _coerce_status(value: Any) -> CandidateStatus:
    if isinstance(value, CandidateStatus):
        return value
    return CandidateStatus(str(_enum_value(value)))


def _coerce_verdict(value: Any) -> ReviewVerdict:
    if isinstance(value, ReviewVerdict):
        return value
    return ReviewVerdict(str(_enum_value(value)))


def _coerce_role(value: Any) -> Role:
    if isinstance(value, Role):
        return value
    return Role(str(_enum_value(value)))


def _coerce_evidence_status(value: Any) -> EvidenceStatus:
    if isinstance(value, EvidenceStatus):
        return value
    return EvidenceStatus(str(_enum_value(value)))


# --------------------------------------------------------------------------
# 哈希
# --------------------------------------------------------------------------


def _canonical(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_canonical(v) for v in value]
    return value


def hash_text(text: str) -> str:
    """对文本做 sha256，返回前 16 位十六进制。"""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _hash_payload(payload: Any) -> str:
    blob = json.dumps(_canonical(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hash_text(blob)


#: 项目里约定的"未测得"占位符；不会因为缺失而被静默当作实测值。
MODEL_UNKNOWN = "unknown"


# --------------------------------------------------------------------------
# 契约与预算
# --------------------------------------------------------------------------


@dataclass
class Budget:
    """调用/并发/时间上限与付费授权。宿主无付费授权时 ``paid_api_authorized`` 必须为 False。"""

    max_calls: int = 8
    max_concurrency: int = 4
    max_minutes: float = 30.0
    paid_api_authorized: bool = False

    def __post_init__(self) -> None:
        if self.max_calls < 0:
            raise ValueError("max_calls 不能为负")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency 至少为 1")
        if self.max_minutes < 0:
            raise ValueError("max_minutes 不能为负")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_calls": self.max_calls,
            "max_concurrency": self.max_concurrency,
            "max_minutes": self.max_minutes,
            "paid_api_authorized": bool(self.paid_api_authorized),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Budget":
        return cls(
            max_calls=int(data.get("max_calls", 8)),
            max_concurrency=int(data.get("max_concurrency", 4)),
            max_minutes=float(data.get("max_minutes", 30.0)),
            paid_api_authorized=bool(data.get("paid_api_authorized", False)),
        )


@dataclass
class Contract:
    """任务契约。验收条件必须能分辨"更快地做错"和"正确地改进"。"""

    goal: str
    acceptance: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    non_goals: List[str] = field(default_factory=list)
    identifier: str = "contract"
    version: int = 1
    budget: Budget = field(default_factory=Budget)

    def __post_init__(self) -> None:
        if not str(self.goal).strip():
            raise ValueError("契约必须有非空 goal")

    def version_hash(self) -> str:
        """契约投影哈希：goal + acceptance + constraints（+ 标识与版本号）。"""

        return _hash_payload(project_contract(self))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.identifier,
            "version": self.version,
            "goal": self.goal,
            "acceptance": list(self.acceptance),
            "constraints": list(self.constraints),
            "non_goals": list(self.non_goals),
            "budget": self.budget.to_dict(),
            "version_hash": self.version_hash(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Contract":
        return cls(
            goal=str(data["goal"]),
            acceptance=[str(x) for x in data.get("acceptance", [])],
            constraints=[str(x) for x in data.get("constraints", [])],
            non_goals=[str(x) for x in data.get("non_goals", [])],
            identifier=str(data.get("id", data.get("identifier", "contract"))),
            version=int(data.get("version", 1)),
            budget=Budget.from_dict(data.get("budget", {})),
        )


def project_contract(contract: Contract) -> Dict[str, Any]:
    """契约中进入版本哈希的最小字段集。"""

    return {
        "goal": contract.goal,
        "acceptance": list(contract.acceptance),
        "constraints": list(contract.constraints),
        "id": contract.identifier,
        "version": contract.version,
    }


def project_candidate(candidate: "Candidate") -> Dict[str, Any]:
    """候选中进入版本哈希的最小字段集。"""

    return {
        "claim": candidate.claim,
        "artifact": candidate.artifact,
        "method": candidate.method,
    }


def _normalize_artifact(artifact: str) -> str:
    """把产物路径规范化后参与哈希：绝对路径与相对路径指向同一文件时哈希一致。"""

    if not artifact:
        return ""
    try:
        return os.path.normcase(os.path.normpath(os.path.abspath(artifact)))
    except Exception:  # pragma: no cover - abspath 几乎不会失败
        return os.path.normcase(os.path.normpath(artifact))


def compute_version_hash(
    contract: Contract,
    claim: str,
    artifact: str,
    method: str,
    contract_version_hash: Optional[str] = None,
) -> str:
    """稳定内容哈希 = 契约投影 + 候选投影。

    任何一侧改动都会翻转哈希，于是"修复候选后旧审查继续有效"与
    "偷偷放宽验收标准后继续使用旧审查"都会在门禁处失配。
    """

    payload: Dict[str, Any] = {
        "contract": project_contract(contract),
        "candidate": {"claim": claim, "artifact": _normalize_artifact(artifact), "method": method},
    }
    if contract_version_hash:
        payload["contract_version_hash"] = contract_version_hash
    return _hash_payload(payload)


# --------------------------------------------------------------------------
# 候选
# --------------------------------------------------------------------------


@dataclass
class Candidate:
    """候选证据卡。``status`` 不得由作者自封为 verified。"""

    candidate_id: str
    contract_id: str
    producer_instance: str
    method: str
    claim: str
    artifact: str
    contract_snapshot: Contract
    status: CandidateStatus = CandidateStatus.PROPOSED
    assumptions: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    next_check: str = ""
    source_ids: List[str] = field(default_factory=list)
    parent_candidate_ids: List[str] = field(default_factory=list)
    contract_version_hash: str = ""

    def __post_init__(self) -> None:
        if not str(self.candidate_id).strip():
            raise ValueError("候选必须有 candidate_id")
        if not str(self.claim).strip():
            raise ValueError("候选必须有非空 claim")
        self.contract_snapshot = _as_contract(self.contract_snapshot)
        self.status = _coerce_status(self.status)
        if not self.contract_version_hash:
            self.contract_version_hash = self.contract_snapshot.version_hash()

    @property
    def version_hash(self) -> str:
        return compute_version_hash(
            self.contract_snapshot,
            self.claim,
            self.artifact,
            self.method,
            self.contract_version_hash,
        )

    def payload(self) -> Dict[str, Any]:
        return {
            "contract": project_contract(self.contract_snapshot),
            "candidate": project_candidate(self),
            "contract_version_hash": self.contract_version_hash,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.candidate_id,
            "contract_id": self.contract_id,
            "producer_instance": self.producer_instance,
            "method": self.method,
            "claim": self.claim,
            "artifact": self.artifact,
            "status": self.status.value,
            "assumptions": list(self.assumptions),
            "limitations": list(self.limitations),
            "next_check": self.next_check,
            "source_ids": list(self.source_ids),
            "parent_candidate_ids": list(self.parent_candidate_ids),
            "contract": self.contract_snapshot.to_dict(),
            "contract_version_hash": self.contract_version_hash,
            "version_hash": self.version_hash,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Candidate":
        contract = _as_contract(data.get("contract", {}))
        return cls(
            candidate_id=str(data["id"]),
            contract_id=str(data.get("contract_id", contract.identifier)),
            producer_instance=str(data.get("producer_instance", "")),
            method=str(data.get("method", "")),
            claim=str(data["claim"]),
            artifact=str(data.get("artifact", "")),
            contract_snapshot=contract,
            status=_coerce_status(data.get("status", CandidateStatus.PROPOSED)),
            assumptions=[str(x) for x in data.get("assumptions", [])],
            limitations=[str(x) for x in data.get("limitations", [])],
            next_check=str(data.get("next_check", "")),
            source_ids=[str(x) for x in data.get("source_ids", [])],
            parent_candidate_ids=[str(x) for x in data.get("parent_candidate_ids", [])],
            contract_version_hash=str(data.get("contract_version_hash", "")),
        )


def _as_contract(value: Any) -> Contract:
    if isinstance(value, Contract):
        return value
    if isinstance(value, Mapping):
        return Contract.from_dict(value)
    raise TypeError("contract_snapshot 必须是 Contract 或 dict")


# --------------------------------------------------------------------------
# 检查与报告
# --------------------------------------------------------------------------


@dataclass
class Check:
    """一条可执行检查。

    ``command`` 可以是 argv 列表，也可以是字符串（按 shell 词法拆分，不启用 shell）。
    ``read_only=False`` 的检查会被 ``verification.run_checks`` 拒绝执行。
    """

    check_id: str
    command: Any
    expect_exit: int = 0
    expect_stdout_contains: Optional[str] = None
    timeout_seconds: float = 20.0
    description: str = ""
    cwd: str = ""
    read_only: bool = True
    environment: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.command, str):
            if not self.command.strip():
                raise ValueError("检查命令不能为空")
        elif isinstance(self.command, (list, tuple)):
            if not self.command:
                raise ValueError("检查 argv 不能为空")
            self.command = [str(x) for x in self.command]
        else:
            raise ValueError("检查命令必须是字符串或 argv 列表")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须为正")

    def argv(self) -> List[str]:
        if isinstance(self.command, str):
            import shlex

            return shlex.split(self.command)
        return list(self.command)

    def display(self) -> str:
        if isinstance(self.command, str):
            return self.command
        return " ".join(self.command)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.check_id,
            "command": self.command if isinstance(self.command, str) else list(self.command),
            "expect_exit": self.expect_exit,
            "expect_stdout_contains": self.expect_stdout_contains,
            "timeout_seconds": self.timeout_seconds,
            "description": self.description,
            "cwd": self.cwd,
            "read_only": bool(self.read_only),
            "environment": dict(self.environment),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Check":
        return cls(
            check_id=str(data["id"]),
            command=data["command"],
            expect_exit=int(data.get("expect_exit", 0)),
            expect_stdout_contains=data.get("expect_stdout_contains"),
            timeout_seconds=float(data.get("timeout_seconds", 20.0)),
            description=str(data.get("description", "")),
            cwd=str(data.get("cwd", "")),
            read_only=bool(data.get("read_only", True)),
            environment={str(k): str(v) for k, v in (data.get("environment") or {}).items()},
        )


@dataclass
class CheckResult:
    """单条检查的执行结果。``passed`` / ``failed`` / ``inconclusive`` 三态互斥。

    ``detail`` 记录"为什么失败"（例如退出码不符、stdout 缺期望子串）；
    ``inconclusive_reason`` 只在**无法判定**时非空（超时、被跳过、无法执行）。
    失败与无法判定是两件事：退出码不符是明确的 fail，不是 inconclusive。
    """

    check_id: str
    command: str
    exit_code: Optional[int]
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False
    detail: str = ""
    skipped: bool = False
    expected: str = ""
    inconclusive_reason: str = ""

    @property
    def inconclusive(self) -> bool:
        return bool(
            self.timed_out
            or self.skipped
            or self.inconclusive_reason
            or self.exit_code is None
        )

    @property
    def passed(self) -> bool:
        return not self.inconclusive and self.exit_code == 0

    @property
    def failed(self) -> bool:
        return not self.inconclusive and self.exit_code != 0

    def status(self) -> str:
        if self.inconclusive:
            return "inconclusive"
        return "pass" if self.passed else "fail"

    def summary(self) -> str:
        if self.inconclusive:
            reason = self.inconclusive_reason or ("超时" if self.timed_out else "未执行")
            return "inconclusive: %s (cmd=%s)" % (reason, self.command)
        suffix = ("；%s" % self.detail) if self.detail else ""
        return "%s: exit=%s%s (cmd=%s)" % (self.status(), self.exit_code, suffix, self.command)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check_id": self.check_id,
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_seconds": self.duration_seconds,
            "timed_out": self.timed_out,
            "detail": self.detail,
            "skipped": self.skipped,
            "expected": self.expected,
            "inconclusive_reason": self.inconclusive_reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CheckResult":
        return cls(
            check_id=str(data.get("check_id", "")),
            command=str(data.get("command", "")),
            exit_code=data.get("exit_code"),
            stdout=str(data.get("stdout", "")),
            stderr=str(data.get("stderr", "")),
            duration_seconds=float(data.get("duration_seconds", 0.0)),
            timed_out=bool(data.get("timed_out", False)),
            detail=str(data.get("detail", data.get("error", ""))),
            skipped=bool(data.get("skipped", False)),
            expected=str(data.get("expected", "")),
            inconclusive_reason=str(data.get("inconclusive_reason", "")),
        )


@dataclass
class VerificationReport:
    """技术验证报告。必须标注被审查的候选版本哈希。"""

    report_id: str
    candidate_id: str
    reviewed_version_hash: str
    reviewer_instance: str
    model_id: str = MODEL_UNKNOWN
    verdict: ReviewVerdict = ReviewVerdict.INCONCLUSIVE
    results: List[CheckResult] = field(default_factory=list)
    notes: str = ""
    limitations: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.verdict = _coerce_verdict(self.verdict)
        self.results = [r if isinstance(r, CheckResult) else CheckResult.from_dict(r) for r in self.results]

    @property
    def passed_checks(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed_checks(self) -> int:
        return sum(1 for r in self.results if r.failed)

    @property
    def inconclusive_checks(self) -> int:
        return sum(1 for r in self.results if r.inconclusive)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.report_id,
            "candidate_id": self.candidate_id,
            "reviewed_version_hash": self.reviewed_version_hash,
            "reviewer_instance": self.reviewer_instance,
            "model_id": self.model_id,
            "verdict": self.verdict.value,
            "results": [r.to_dict() for r in self.results],
            "notes": self.notes,
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VerificationReport":
        return cls(
            report_id=str(data["id"]),
            candidate_id=str(data.get("candidate_id", "")),
            reviewed_version_hash=str(data.get("reviewed_version_hash", "")),
            reviewer_instance=str(data.get("reviewer_instance", "")),
            model_id=str(data.get("model_id", MODEL_UNKNOWN)),
            verdict=_coerce_verdict(data.get("verdict", ReviewVerdict.INCONCLUSIVE)),
            results=[CheckResult.from_dict(r) for r in data.get("results", [])],
            notes=str(data.get("notes", "")),
            limitations=[str(x) for x in data.get("limitations", [])],
        )


@dataclass
class AlignmentReport:
    """目标对齐报告。技术 pass 不自动代表目标 pass。"""

    report_id: str
    candidate_id: str
    reviewed_version_hash: str
    reviewer_instance: str
    contract_version_hash: str = ""
    model_id: str = MODEL_UNKNOWN
    verdict: ReviewVerdict = ReviewVerdict.INCONCLUSIVE
    drift: List[str] = field(default_factory=list)
    findings: List[str] = field(default_factory=list)
    #: 审查者自报"验收标准已被放宽"的标记；为真时不得给出 pass。
    acceptance_relaxed: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        self.verdict = _coerce_verdict(self.verdict)
        if self.acceptance_relaxed and self.verdict is ReviewVerdict.PASS:
            raise ValueError("自报验收标准被放宽的对齐报告不能给 pass")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.report_id,
            "candidate_id": self.candidate_id,
            "reviewed_version_hash": self.reviewed_version_hash,
            "reviewer_instance": self.reviewer_instance,
            "contract_version_hash": self.contract_version_hash,
            "model_id": self.model_id,
            "verdict": self.verdict.value,
            "drift": list(self.drift),
            "findings": list(self.findings),
            "acceptance_relaxed": bool(self.acceptance_relaxed),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AlignmentReport":
        return cls(
            report_id=str(data["id"]),
            candidate_id=str(data.get("candidate_id", "")),
            reviewed_version_hash=str(data.get("reviewed_version_hash", "")),
            reviewer_instance=str(data.get("reviewer_instance", "")),
            contract_version_hash=str(data.get("contract_version_hash", "")),
            model_id=str(data.get("model_id", MODEL_UNKNOWN)),
            verdict=_coerce_verdict(data.get("verdict", ReviewVerdict.INCONCLUSIVE)),
            drift=[str(x) for x in data.get("drift", [])],
            findings=[str(x) for x in data.get("findings", [])],
            acceptance_relaxed=bool(data.get("acceptance_relaxed", False)),
            notes=str(data.get("notes", "")),
        )


# --------------------------------------------------------------------------
# 种子卡
# --------------------------------------------------------------------------


@dataclass
class SeedCard:
    """有界种子卡（P3/P6）。构造时即校验完整性，缺项直接报错。"""

    seed_id: str
    source: str
    condition: str
    evidence_status: EvidenceStatus
    next_check: str
    claim: str = ""
    group: str = ""
    refutes: str = ""
    target_group: str = ""

    def __post_init__(self) -> None:
        missing: List[str] = []
        for name, value in (
            ("seed_id", self.seed_id),
            ("source", self.source),
            ("condition", self.condition),
            ("next_check", self.next_check),
        ):
            if not str(value).strip():
                missing.append(name)
        status = _coerce_evidence_status(self.evidence_status)
        self.evidence_status = status
        if missing:
            raise ValueError(
                "种子卡缺少必填项（来源/适用条件/证据状态/下一步检查）：%s" % ", ".join(missing)
            )
        if status is EvidenceStatus.UNKNOWN:
            raise ValueError("种子卡证据状态不能为 unknown；未验证的种子应标为 proposed")
        if status is EvidenceStatus.REFUTED and not str(self.refutes).strip():
            raise ValueError("被推翻的种子必须写明 refutes（被推翻的主张或受影响候选）")

    def is_proposed(self) -> bool:
        return self.evidence_status is EvidenceStatus.PROPOSED

    def label(self) -> str:
        return self.evidence_status.value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.seed_id,
            "source": self.source,
            "condition": self.condition,
            "evidence_status": self.evidence_status.value,
            "next_check": self.next_check,
            "claim": self.claim,
            "group": self.group,
            "refutes": self.refutes,
            "target_group": self.target_group,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SeedCard":
        return cls(
            seed_id=str(data["id"]),
            source=str(data.get("source", "")),
            condition=str(data.get("condition", "")),
            evidence_status=_coerce_evidence_status(data.get("evidence_status", "proposed")),
            next_check=str(data.get("next_check", "")),
            claim=str(data.get("claim", "")),
            group=str(data.get("group", "")),
            refutes=str(data.get("refutes", "")),
            target_group=str(data.get("target_group", "")),
        )


def seed_limit_violations(
    cards: Sequence[SeedCard], max_per_group: int = 2
) -> List[str]:
    """返回超过"每组最多 N 条"限制的组名，便于演示有界性。"""

    counts: Dict[str, int] = {}
    for card in cards:
        key = card.target_group or card.group or "(未分组)"
        counts[key] = counts.get(key, 0) + 1
    return sorted(k for k, v in counts.items() if v > max_per_group)


def describe_cards(cards: Iterable[SeedCard]) -> List[str]:
    """一行一张卡的可打印摘要，便于演示与人工复查。"""

    lines = []
    for card in cards:
        lines.append(
            "[%s] %s <- %s | 条件: %s | 下一步: %s"
            % (card.label(), card.seed_id, card.source, card.condition, card.next_check)
        )
    return lines
