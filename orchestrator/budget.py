"""预算账本：在**准入之前**执行 max_calls / max_concurrency / max_minutes / 付费授权。

关键性质
--------
* ``admit()`` 在任何工作开始之前调用；拒绝时抛出 ``BudgetExceeded``，附带具体原因。
* 未授权付费 API 时，任何声称需要付费调用的准入都会被拒绝。
* 时间上限只在调用之间检查（同一时刻只能比较已记录的耗时），因此它是"拒发新工作"，
  不是抢占式中断。这一点在 ``usage_summary`` 里以 ``measured=False`` 诚实标注。
* ``usage_summary`` 区分 **measured**（本进程实测到的调用数、峰值并发、耗时）与
  **unknown**（token、费用：本包从不调用 LLM，也不推断这些值）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from .model import Budget

__all__ = ["BudgetExceeded", "UsageSummary", "Ledger"]

#: 时间基准占位：Ledger 未显式记录 ``elapsed_minutes`` 时用它表示"尚未校准"。
_UNSET = object()


class BudgetExceeded(RuntimeError):
    """超预算准入被拒绝。消息里带具体维度与当前值。"""

    def __init__(self, dimension: str, message: str, state: Optional[Mapping[str, Any]] = None):
        super().__init__(message)
        self.dimension = dimension
        self.state: Dict[str, Any] = dict(state or {})


@dataclass
class UsageSummary:
    """用量摘要：实测与未知严格分开，不把估算写成实测。"""

    max_calls: int
    calls_used: int
    calls_remaining: int
    max_concurrency: int
    peak_concurrency: int
    max_minutes: float
    minutes_used: float
    minutes_remaining: float
    paid_api_authorized: bool
    measured: Dict[str, Any] = field(default_factory=dict)
    unknown: List[str] = field(default_factory=list)
    admissions_refused: int = 0
    in_flight: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "budget": {
                "max_calls": self.max_calls,
                "max_concurrency": self.max_concurrency,
                "max_minutes": self.max_minutes,
                "paid_api_authorized": self.paid_api_authorized,
            },
            "usage": {
                "calls_used": self.calls_used,
                "calls_remaining": self.calls_remaining,
                "peak_concurrency": self.peak_concurrency,
                "minutes_used": round(self.minutes_used, 3),
                "minutes_remaining": round(self.minutes_remaining, 3),
            },
            "measured": dict(self.measured),
            "unknown": list(self.unknown),
            "admissions_refused": self.admissions_refused,
            "in_flight": self.in_flight,
        }

    def to_lines(self) -> List[str]:
        """人类可读摘要；明确标出哪些是实测、哪些是未知。"""

        lines = [
            "调用: %d/%d 已用（剩余 %d）" % (self.calls_used, self.max_calls, self.calls_remaining),
            "并发: 峰值 %d / 上限 %d（在途 %d）" % (self.peak_concurrency, self.max_concurrency, self.in_flight),
            "时间: %.3f/%.1f 分钟 已用（剩余 %.3f）"
            % (self.minutes_used, self.max_minutes, self.minutes_remaining),
            "付费 API 授权: %s" % ("是" if self.paid_api_authorized else "否"),
            "实测 (measured): %s"
            % ", ".join("%s=%s" % (k, v) for k, v in sorted(self.measured.items())),
            "未知 (unknown): %s" % ("; ".join(self.unknown) if self.unknown else "无"),
            "被拒绝的准入次数: %d" % self.admissions_refused,
        ]
        return lines


class Ledger:
    """确定性预算账本。时间可通过注入 ``clock`` 变为可测试。"""

    #: 费用/token 类字段一律记为未知；本包不调用 LLM，也不做估算。
    UNMEASURABLE = (
        "token 用量（本包不调用 LLM，未测量）",
        "费用（本包不调用 LLM，未测量）",
    )

    def __init__(
        self,
        budget: Budget,
        calls_used: int = 0,
        elapsed_minutes: Any = _UNSET,
        peak_concurrency: int = 0,
        admissions_refused: int = 0,
        in_flight: int = 0,
        clock: Any = None,
    ) -> None:
        self.budget = budget
        self.calls_used = int(calls_used)
        self._clock = clock or time.monotonic
        # 未显式给出耗时时，以"现在"为基准，于是 minutes_used 从 0 开始增长。
        # 显式给出（例如从检查点恢复）时，基准前移，使 minutes_used 等于该值。
        self._base = self._clock() - (0.0 if elapsed_minutes is _UNSET else float(elapsed_minutes)) * 60.0
        self.peak_concurrency = int(peak_concurrency)
        self.admissions_refused = int(admissions_refused)
        self.in_flight = int(in_flight)
        self._active = 0
        self._active_labels: List[str] = []

    # -- 状态 ------------------------------------------------------------
    @property
    def calls_remaining(self) -> int:
        return max(0, self.budget.max_calls - self.calls_used)

    @property
    def active_concurrency(self) -> int:
        return self._active + self.in_flight

    @property
    def elapsed_minutes(self) -> float:
        return max(0.0, (self._clock() - self._base) / 60.0)

    @property
    def minutes_used(self) -> float:
        return self.elapsed_minutes

    # -- 准入 ------------------------------------------------------------
    def preflight(self, calls: int = 1, concurrency: int = 1) -> Optional[str]:
        """检查是否可准入；可准入返回 ``None``，否则返回具体拒绝原因。"""

        if calls < 1:
            return "calls 必须 >= 1"
        if self.calls_used + calls > self.budget.max_calls:
            return "调用预算不足：已用 %d/%d，本次需要 %d" % (
                self.calls_used,
                self.budget.max_calls,
                calls,
            )
        if self.active_concurrency + concurrency > self.budget.max_concurrency:
            return "并发槽位不足：在途 %d + 本次 %d > 上限 %d" % (
                self.active_concurrency,
                concurrency,
                self.budget.max_concurrency,
            )
        if self.minutes_used > self.budget.max_minutes:
            return "时间上限已到：已用 %.3f 分钟 > 上限 %.1f 分钟" % (
                self.minutes_used,
                self.budget.max_minutes,
            )
        return None

    def admit(
        self,
        scope: str,
        calls: int = 1,
        concurrency: int = 1,
        paid_api: bool = False,
        minutes: float = 0.0,
    ) -> int:
        """准入一次工作；成功返回本次占用的调用序号（从 1 开始）。

        拒绝时抛 ``BudgetExceeded``，且**不产生任何副作用**。
        """

        if paid_api and not self.budget.paid_api_authorized:
            self.admissions_refused += 1
            raise BudgetExceeded(
                "paid_api",
                "未授权付费 API：契约 paid_api_authorized=false，拒绝 %s 的付费调用" % scope,
                self.snapshot(),
            )
        reason = self.preflight(calls=calls, concurrency=concurrency)
        if reason is not None:
            self.admissions_refused += 1
            raise BudgetExceeded(
                _dimension_of(reason),
                "拒绝准入 %s：%s" % (scope, reason),
                self.snapshot(),
            )
        self.calls_used += calls
        self._active += 1
        self._active_labels.append(scope)
        self.peak_concurrency = max(self.peak_concurrency, self.active_concurrency)
        if minutes:
            self.record_elapsed(minutes)
        return self.calls_used

    def release(self, scope: str = "") -> None:
        """结束一次已准入的工作，释放并发槽位。"""

        self._active = max(0, self._active - 1)
        if self._active_labels:
            if scope and scope in self._active_labels:
                self._active_labels.remove(scope)
            else:
                self._active_labels.pop()

    def record_call(self, scope: str, minutes: float = 0.0) -> int:
        """便捷方法：准入并立即释放（顺序执行场景）。"""

        seq = self.admit(scope, minutes=minutes)
        self.release(scope)
        return seq

    def record_elapsed(self, minutes: float) -> None:
        """记录外部实测耗时（例如子进程检查的墙钟时间）：把时间基准前移。"""

        self._base -= max(0.0, float(minutes)) * 60.0

    # -- 汇总 ------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        return {
            "calls_used": self.calls_used,
            "max_calls": self.budget.max_calls,
            "peak_concurrency": self.peak_concurrency,
            "max_concurrency": self.budget.max_concurrency,
            "minutes_used": round(self.minutes_used, 6),
            "max_minutes": self.budget.max_minutes,
            "in_flight": self.in_flight,
            "admissions_refused": self.admissions_refused,
        }

    def usage_summary(self) -> UsageSummary:
        minutes_used = self.minutes_used
        return UsageSummary(
            max_calls=self.budget.max_calls,
            calls_used=self.calls_used,
            calls_remaining=self.calls_remaining,
            max_concurrency=self.budget.max_concurrency,
            peak_concurrency=self.peak_concurrency,
            max_minutes=self.budget.max_minutes,
            minutes_used=minutes_used,
            minutes_remaining=max(0.0, self.budget.max_minutes - minutes_used),
            paid_api_authorized=self.budget.paid_api_authorized,
            measured={
                "calls_used": self.calls_used,
                "peak_concurrency": self.peak_concurrency,
                "minutes_used": round(minutes_used, 4),
                "admissions_refused": self.admissions_refused,
            },
            unknown=list(self.UNMEASURABLE),
            admissions_refused=self.admissions_refused,
            in_flight=self.in_flight,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "budget": self.budget.to_dict(),
            "calls_used": self.calls_used,
            "elapsed_minutes": round(self.elapsed_minutes, 6),
            "peak_concurrency": self.peak_concurrency,
            "admissions_refused": self.admissions_refused,
            "in_flight": self.in_flight,
            "clock_is_injected": self._clock is not time.monotonic,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], clock: Any = None) -> "Ledger":
        return cls(
            budget=Budget.from_dict(data.get("budget", {})),
            calls_used=int(data.get("calls_used", 0)),
            elapsed_minutes=data.get("elapsed_minutes", _UNSET),
            peak_concurrency=int(data.get("peak_concurrency", 0)),
            admissions_refused=int(data.get("admissions_refused", 0)),
            in_flight=int(data.get("in_flight", 0)),
            clock=clock,
        )


def _dimension_of(reason: str) -> str:
    if "调用预算" in reason:
        return "max_calls"
    if "并发槽位" in reason:
        return "max_concurrency"
    if "时间上限" in reason:
        return "max_minutes"
    return "unknown"
