"""检查点与恢复（P7）：进程退出后必须能续跑，且模型变更不得静默提升旧证据。

要点
----
* 全部状态用 **JSON** 落盘（本包不依赖 PyYAML）。写入使用"临时文件 + ``os.replace``"，
  原子替换，不会留下半截检查点。
* 检查点会记录：契约、预算与已消费调用/时间/实测用量、候选及其版本哈希、检查、
  技术验证与对齐报告、种子卡、在途工作、**角色实例的模型标识**，以及
  ``reverification_required`` / ``reverified_after_model_change`` 显式标记。
* 产物路径在保存时会被改写为**相对 run_dir** 的形式（若在 run_dir 内），避免把
  机器相关的绝对路径写进检查点，同时保证 ``version_hash`` 往返后不变。
* ``mark_model_change`` 只置标记与记账，**不会**把任何候选提升为 verified。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .budget import Ledger
from .model import (
    MODEL_UNKNOWN,
    AlignmentReport,
    Budget,
    Candidate,
    CandidateStatus,
    Check,
    Contract,
    SeedCard,
    VerificationReport,
)
from .verification import evaluate_gate

__all__ = ["StateError", "RunState", "new_run", "load_contract", "CHECKPOINT_NAME", "CONTRACT_NAME", "utc_now"]

CHECKPOINT_NAME = "checkpoint.json"
CONTRACT_NAME = "contract.json"
#: 与 docs/minimal-setup.md 中的目录约定保持同名；内容为 JSON（本包不依赖 PyYAML）。
CONTRACT_ALIAS_NAME = "contract.yaml"
CHECKPOINT_ALIAS_NAME = "checkpoint.yaml"


class StateError(RuntimeError):
    """检查点缺失、损坏或版本不兼容。"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _atomic_write(path: str, text: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory, prefix=".tmp-", suffix=".json", delete=False
    )
    try:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(handle.name, path)
    except BaseException:
        try:
            handle.close()
        except Exception:  # pragma: no cover
            pass
        if os.path.exists(handle.name):
            os.unlink(handle.name)
        raise


@dataclass
class RunState:
    """一次运行的完整可落盘状态。"""

    run_dir: str
    contract: Contract
    metadata: Dict[str, Any] = field(default_factory=dict)
    ledger: Ledger = field(default_factory=lambda: Ledger(Budget()))
    candidates: Dict[str, Candidate] = field(default_factory=dict)
    checks: Dict[str, Check] = field(default_factory=dict)
    verification_reports: Dict[str, VerificationReport] = field(default_factory=dict)
    alignment_reports: Dict[str, AlignmentReport] = field(default_factory=dict)
    seeds: List[SeedCard] = field(default_factory=list)
    needs_reverification: List[str] = field(default_factory=list)
    reverification_required: bool = False
    reverified_after_model_change: bool = False
    applied_models: List[str] = field(default_factory=list)
    model_history: List[Dict[str, Any]] = field(default_factory=list)
    in_flight: List[Dict[str, Any]] = field(default_factory=list)
    sent_seeds: List[str] = field(default_factory=list)
    disabled_leads: List[str] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.run_dir = os.path.abspath(os.path.expanduser(str(self.run_dir)))
        os.makedirs(self.run_dir, exist_ok=True)
        self.metadata.setdefault("run_id", os.path.basename(self.run_dir) or "run")
        self.metadata.setdefault("contract_id", self.contract.identifier)
        self.metadata.setdefault("contract_version", self.contract.version)
        self.metadata.setdefault("created_at", utc_now())
        self.metadata.setdefault("updated_at", self.metadata["created_at"])
        self.metadata.setdefault("schema_version", 1)
        model_id = str(self.metadata.setdefault("model_id", MODEL_UNKNOWN))
        if model_id not in self.applied_models:
            self.applied_models.append(model_id)

    # -- 基本属性 --------------------------------------------------------
    @property
    def contract_path(self) -> str:
        return os.path.join(self.run_dir, CONTRACT_NAME)

    @property
    def checkpoint_path(self) -> str:
        return os.path.join(self.run_dir, CHECKPOINT_NAME)

    @property
    def model_id(self) -> str:
        return str(self.metadata.get("model_id", MODEL_UNKNOWN))

    # -- 增删 ------------------------------------------------------------
    def add_candidate(self, candidate: Candidate) -> Candidate:
        self.candidates[candidate.candidate_id] = candidate
        return candidate

    def add_checks(self, checks: Sequence[Check]) -> None:
        for check in checks:
            self.checks[check.check_id] = check

    def record_verification(self, report: VerificationReport) -> VerificationReport:
        self.verification_reports[report.candidate_id] = report
        return report

    def record_alignment(self, report: AlignmentReport) -> AlignmentReport:
        self.alignment_reports[report.candidate_id] = report
        return report

    def add_seed(self, seed: SeedCard) -> SeedCard:
        self.seeds.append(seed)
        return seed

    # -- 门禁 ------------------------------------------------------------
    def gate(self, candidate_id: str):
        """对指定候选做双门禁判定（缺少候选时报错）。"""

        candidate = self.candidates.get(candidate_id)
        if candidate is None:
            raise StateError("候选不存在: %s" % candidate_id)
        return evaluate_gate(
            candidate,
            self.verification_reports.get(candidate_id),
            self.alignment_reports.get(candidate_id),
        )

    def promote_if_verified(self, candidate_id: str) -> Tuple[bool, str]:
        """仅在门禁允许时把候选标为 verified；否则保持原状态并给出理由。"""

        candidate = self.candidates.get(candidate_id)
        if candidate is None:
            raise StateError("候选不存在: %s" % candidate_id)
        decision = self.gate(candidate_id)
        if not decision.allowed:
            return False, decision.explain()
        candidate.status = CandidateStatus.VERIFIED
        self.reverified_after_model_change = self.reverified_after_model_change or bool(
            self.reverification_required
        )
        return True, "已标记 verified：%s" % decision.explain()

    def verified_candidates(self) -> List[str]:
        return sorted(cid for cid, c in self.candidates.items() if c.status is CandidateStatus.VERIFIED)

    def needs_reverification_for(self, candidate_id: str) -> bool:
        if candidate_id in self.needs_reverification:
            return True
        if not self.reverification_required:
            return False
        # 模型变更后，所有"曾经 verified"的候选都需要重验；标记清除后即视为已重验。
        return candidate_id in self.verified_candidates()

    # -- 模型变更 --------------------------------------------------------
    def mark_model_change(self, model_id: str, reason: str = "") -> List[str]:
        """切换宿主模型。返回需要重新验证的候选 id 列表（不提升任何候选）。"""

        model_id = str(model_id or MODEL_UNKNOWN)
        previous = self.model_id
        affected = sorted(set(self.verified_candidates()) | set(self.needs_reverification))
        self.metadata["model_id"] = model_id
        if model_id not in self.applied_models:
            self.applied_models.append(model_id)
        self.metadata["updated_at"] = utc_now()
        self.reverification_required = True
        self.needs_reverification = sorted(set(self.needs_reverification) | set(affected))
        self.model_history.append(
            {
                "from": previous,
                "to": model_id,
                "at": utc_now(),
                "reason": reason,
                "affected_candidates": affected,
                "note": "旧证据不会自动视为新模型的验证结果；需按同一检查点重新验证",
            }
        )
        return affected

    def clear_reverification(self, candidate_ids: Optional[Sequence[str]] = None, model_id: str = "") -> List[str]:
        """显式清除重验标记（表示已用新模型重跑必要检查）。"""

        targets = list(candidate_ids or self.needs_reverification)
        for candidate_id in targets:
            if candidate_id in self.needs_reverification:
                self.needs_reverification.remove(candidate_id)
        if model_id:
            self.metadata["model_id"] = str(model_id)
        if not self.needs_reverification:
            self.reverification_required = False
        self.reverified_after_model_change = True
        self.metadata["updated_at"] = utc_now()
        return targets

    def mark_reverified_after_model_change(self) -> None:
        self.reverified_after_model_change = True

    # -- 序列化 ----------------------------------------------------------
    def _artifact_out(self, path: str) -> str:
        """把 run_dir 内的绝对产物路径改写为相对路径，使检查点与机器无关。"""

        if not path:
            return path
        absolute = os.path.abspath(path)
        run_real = os.path.realpath(self.run_dir)
        path_real = os.path.realpath(absolute)
        if path_real == run_real or path_real.startswith(run_real + os.sep):
            return os.path.relpath(path_real, run_real)
        return absolute

    def _artifact_in(self, path: str) -> str:
        if not path:
            return path
        if os.path.isabs(path):
            return path
        return os.path.normpath(os.path.join(self.run_dir, path))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": 1,
            "run_dir": self.run_dir,
            "metadata": dict(self.metadata),
            "contract": self.contract.to_dict(),
            "ledger": self.ledger.to_dict(),
            "candidates": {cid: self._candidate_out(c) for cid, c in sorted(self.candidates.items())},
            "checks": {cid: c.to_dict() for cid, c in sorted(self.checks.items())},
            "verification_reports": {
                cid: r.to_dict() for cid, r in sorted(self.verification_reports.items())
            },
            "alignment_reports": {cid: r.to_dict() for cid, r in sorted(self.alignment_reports.items())},
            "seeds": [s.to_dict() for s in self.seeds],
            "reverification_required": bool(self.reverification_required),
            "reverified_after_model_change": bool(self.reverified_after_model_change),
            "needs_reverification": list(self.needs_reverification),
            "applied_models": list(self.applied_models),
            "model_history": [dict(h) for h in self.model_history],
            "in_flight": [dict(x) for x in self.in_flight],
            "sent_seeds": list(self.sent_seeds),
            "disabled_leads": list(self.disabled_leads),
            "failures": list(self.failures),
        }

    def _candidate_out(self, candidate: Candidate) -> Dict[str, Any]:
        data = candidate.to_dict()
        data["artifact"] = self._artifact_out(candidate.artifact)
        data["contract"]["budget"] = candidate.contract_snapshot.budget.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], run_dir: Optional[str] = None) -> "RunState":
        """从检查点重建状态。字段逐个还原，不重跑 ``__init__`` 的默认值填充。"""

        if "contract" not in data:
            raise StateError("检查点缺少 contract 字段")
        resolved_run_dir = os.path.abspath(
            os.path.expanduser(run_dir or str(data.get("run_dir") or "."))
        )
        state = object.__new__(cls)
        state.run_dir = resolved_run_dir
        state.contract = Contract.from_dict(data["contract"])
        state.metadata = dict(data.get("metadata", {}))
        state.ledger = Ledger.from_dict(data.get("ledger", {}))
        state.candidates = {}
        state.checks = {}
        state.verification_reports = {}
        state.alignment_reports = {}
        state.seeds = [SeedCard.from_dict(s) for s in data.get("seeds", [])]
        state.needs_reverification = [str(x) for x in data.get("needs_reverification", [])]
        state.reverification_required = bool(data.get("reverification_required", False))
        state.reverified_after_model_change = bool(
            data.get("reverified_after_model_change", False)
        )
        state.applied_models = [str(x) for x in data.get("applied_models", [])]
        state.model_history = [dict(h) for h in data.get("model_history", [])]
        state.in_flight = [dict(x) for x in data.get("in_flight", [])]
        state.sent_seeds = [str(x) for x in data.get("sent_seeds", [])]
        state.disabled_leads = [str(x) for x in data.get("disabled_leads", [])]
        state.failures = [str(x) for x in data.get("failures", [])]
        state.metadata.setdefault("run_id", os.path.basename(resolved_run_dir) or "run")
        state.metadata.setdefault("contract_id", state.contract.identifier)
        state.metadata.setdefault("contract_version", state.contract.version)
        state.metadata.setdefault("model_id", MODEL_UNKNOWN)

        for cid, raw in data.get("candidates", {}).items():
            payload = dict(raw)
            payload["artifact"] = state._artifact_in(str(payload.get("artifact", "")))
            state.candidates[str(cid)] = Candidate.from_dict(payload)
        for cid, raw in data.get("checks", {}).items():
            state.checks[str(cid)] = Check.from_dict(raw)
        for cid, raw in data.get("verification_reports", {}).items():
            state.verification_reports[str(cid)] = VerificationReport.from_dict(raw)
        for cid, raw in data.get("alignment_reports", {}).items():
            state.alignment_reports[str(cid)] = AlignmentReport.from_dict(raw)
        return state

    # -- 落盘 ------------------------------------------------------------
    def save_contract(self) -> str:
        text = json.dumps(self.contract.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        _atomic_write(self.contract_path, text)
        # 同名的别名文件（内容同样是 JSON；目录约定见 docs/minimal-setup.md）
        alias = os.path.join(self.run_dir, CONTRACT_ALIAS_NAME)
        _atomic_write(alias, text)
        return self.contract_path

    def checkpoint(self, note: str = "") -> str:
        self.metadata["updated_at"] = utc_now()
        if note:
            self.metadata["note"] = note
        payload = self.to_dict()
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        _atomic_write(self.checkpoint_path, text)
        _atomic_write(os.path.join(self.run_dir, CHECKPOINT_ALIAS_NAME), text)
        return self.checkpoint_path

    def save(self, note: str = "") -> str:
        self.save_contract()
        return self.checkpoint(note=note)

    @classmethod
    def load(cls, run_dir: str) -> "RunState":
        """从磁盘读取检查点。缺失或损坏时抛 ``StateError``（消息具体）。"""

        run_dir = os.path.abspath(os.path.expanduser(str(run_dir)))
        path = os.path.join(run_dir, CHECKPOINT_NAME)
        if not os.path.exists(path):
            alias = os.path.join(run_dir, CHECKPOINT_ALIAS_NAME)
            if os.path.exists(alias):
                path = alias
            else:
                raise StateError("找不到检查点: %s" % path)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            raise StateError("检查点无法解析 (%s): %s" % (path, exc))
        if not isinstance(data, dict):
            raise StateError("检查点内容不是对象: %s" % path)
        state = cls.from_dict(data, run_dir=run_dir)
        state.metadata["loaded_from"] = path
        return state

    @classmethod
    def resume(cls, run_dir: str) -> "RunState":
        """恢复：等价于 ``load``，但会标注"从磁盘恢复"。"""

        state = cls.load(run_dir)
        state.metadata["resumed_at"] = utc_now()
        return state

    # -- 展示 ------------------------------------------------------------
    def status_lines(self) -> List[str]:
        lines = [
            "run_dir: %s" % self.run_dir,
            "run_id: %s | 契约版本 %s | 模型 %s"
            % (self.metadata.get("run_id"), self.metadata.get("contract_version"), self.model_id),
            "候选 %d 个，种子 %d 张，技术报告 %d 份，对齐报告 %d 份"
            % (
                len(self.candidates),
                len(self.seeds),
                len(self.verification_reports),
                len(self.alignment_reports),
            ),
        ]
        for cid, candidate in sorted(self.candidates.items()):
            lines.append(
                "  - %s [%s] v=%s %s"
                % (cid, candidate.status.value, candidate.version_hash[:8], candidate.claim[:48])
            )
        lines.extend(self.ledger.usage_summary().to_lines())
        if self.reverification_required:
            lines.append(
                "模型变更后需要重新验证: %s"
                % (", ".join(self.needs_reverification) if self.needs_reverification else "(无记录)")
            )
        else:
            lines.append("模型变更后需要重新验证: 否")
        lines.append("reverified_after_model_change: %s" % self.reverified_after_model_change)
        return lines


def new_run(run_dir: str, contract: Contract, budget: Optional[Budget] = None, model_id: str = MODEL_UNKNOWN) -> RunState:
    """创建一个新运行目录与初始状态（只写契约与初始检查点）。"""

    effective_budget = budget or contract.budget
    state = RunState(
        run_dir=run_dir,
        contract=contract,
        metadata={"model_id": model_id, "run_id": os.path.basename(os.path.abspath(run_dir))},
        ledger=Ledger(effective_budget),
    )
    state.save(note="initial")
    return state


def load_contract(path: str) -> Contract:
    """从 JSON 文件读取契约（本包不依赖 PyYAML；请提供 JSON）。"""

    full = os.path.abspath(os.path.expanduser(str(path)))
    try:
        with open(full, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise StateError("找不到契约文件: %s" % full)
    except (OSError, ValueError) as exc:
        raise StateError("契约无法解析为 JSON (%s): %s" % (full, exc))
    if not isinstance(data, dict):
        raise StateError("契约内容必须是 JSON 对象: %s" % full)
    return Contract.from_dict(data)
