"""ai-research-orchestration 的可运行参考实现（纯标准库，无网络，无 LLM 调用）。

本包不"思考"：它机械地强制执行编排不变量，使人类或 agent 宿主无法静默绕过。

- 角色隔离（P5）：``isolation.AccessPolicy``，默认拒绝的路径白名单。
- 预算账本（契约）：``budget.Ledger``，在开工前而非完工后拒绝超预算。
- 种子交换（P3/P6）：``model.SeedCard``，有界且必须携带来源、适用条件、证据状态、下一步检查。
- 独立验证（P8）：``verification.is_verified``，技术验证与目标对齐双通过，且必须针对当前候选版本。
- 检查点（P7）：``state.RunState``，进程退出后可恢复；模型变更会强制重新验证。

本包不做的事：不调用任何 LLM API，不测量也不推断 token/费用（一律记为 unknown），
不证明"多 agent 优于单 agent"，也不是安全沙箱。
"""

from .budget import BudgetExceeded, Ledger, UsageSummary
from .isolation import AccessDenied, AccessPolicy
from .model import (
    AlignmentReport,
    Budget,
    Candidate,
    CandidateStatus,
    Check,
    CheckResult,
    Contract,
    ReviewVerdict,
    Role,
    SeedCard,
    VerificationReport,
)
from .state import RunState, StateError
from .verification import (
    AlignmentReviewer,
    ContractDrift,
    GateDecision,
    evaluate_gate,
    is_verified,
    run_checks,
)

__all__ = [
    "AccessDenied",
    "AccessPolicy",
    "AlignmentReport",
    "AlignmentReviewer",
    "Budget",
    "BudgetExceeded",
    "Candidate",
    "CandidateStatus",
    "Check",
    "CheckResult",
    "Contract",
    "ContractDrift",
    "GateDecision",
    "Ledger",
    "ReviewVerdict",
    "Role",
    "RunState",
    "SeedCard",
    "StateError",
    "UsageSummary",
    "VerificationReport",
    "evaluate_gate",
    "is_verified",
    "run_checks",
]

__version__ = "1.0.0"
