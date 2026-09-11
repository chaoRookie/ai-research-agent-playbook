"""角色隔离（P5）：默认拒绝的路径白名单。

语义
----
* ``AccessPolicy(run_dir, instance_id, role)`` 代表**一个**角色实例的读取边界。
* ``grant()`` 只登记**相对 run_dir 的路径**；绝对路径、`..` 逃逸、run_dir 之外的
  相对路径在登记时就被拒绝。
* ``check()`` 每次都解析 realpath 之后再比较，因此：
  - ``..`` 逃逸被拒；
  - 绝对路径逃逸被拒；
  - 符号链接指向 run_dir 之外时，**按链接目标判定**并拒绝（不能靠软链接绕过白名单）。
* ``read()`` 在允许时返回文本内容，否则抛 ``AccessDenied``。

局限（如实记录，不夸大）
------------------------
这是**路径白名单**，不是操作系统级沙箱。它拦不住同一进程内绕过本模块的直接文件 API
调用；真正的强隔离需要宿主沙箱、独立工作区或容器。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Sequence

from .model import Role

__all__ = ["AccessDenied", "AccessDecision", "AccessPolicy", "within"]


class AccessDenied(PermissionError):
    """路径不在该角色实例的白名单内（默认拒绝）。"""

    def __init__(self, instance_id: str, requested: str, reason: str):
        super().__init__("角色实例 %s 被拒绝读取 %s：%s" % (instance_id, requested, reason))
        self.instance_id = instance_id
        self.requested = requested
        self.reason = reason


@dataclass
class AccessDecision:
    """一次访问判定。``allowed=False`` 时 ``reason`` 说明具体原因。"""

    allowed: bool
    requested: str
    resolved: str
    reason: str = ""
    matched_grant: str = ""
    kind: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "requested": self.requested,
            "resolved": self.resolved,
            "reason": self.reason,
            "matched_grant": self.matched_grant,
            "kind": self.kind,
        }

    def __bool__(self) -> bool:  # 便于 if policy.check(...)
        return self.allowed


def _is_within(candidate: str, root: str) -> bool:
    """candidate 是否等于 root 或位于 root 之下（两者都应为 realpath）。"""

    if candidate == root:
        return True
    root_with_sep = root if root.endswith(os.sep) else root + os.sep
    return candidate.startswith(root_with_sep)


def within(candidate: str, root: str) -> bool:
    """公开的包含判定：先 realpath 再比较，避免前缀混淆（/run 与 /run2）。"""

    return _is_within(os.path.realpath(candidate), os.path.realpath(root))


@dataclass
class AccessPolicy:
    """单个角色实例的读取白名单。"""

    run_dir: str
    instance_id: str
    role: Role = Role.EXPLORER
    grants: List[str] = field(default_factory=list)
    can_write: bool = False
    write_scopes: List[str] = field(default_factory=list)
    audit: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.role, Role):
            self.role = Role(str(self.role))
        self.run_dir = os.path.abspath(os.path.expanduser(self.run_dir))
        os.makedirs(self.run_dir, exist_ok=True)
        self.run_dir_real = os.path.realpath(self.run_dir)
        self.grants = [str(g) for g in self.grants]

    # -- 白名单维护 ------------------------------------------------------
    def grant(self, relative_path: str) -> str:
        """登记一条相对路径（文件或目录）。越界或绝对路径直接报错。"""

        raw = str(relative_path)
        if not raw.strip():
            raise ValueError("grant 路径不能为空")
        if os.path.isabs(raw) or (len(raw) > 1 and raw[1] == ":"):
            raise ValueError("grant 只接受 run_dir 内的相对路径，收到绝对路径: %s" % raw)
        normalized = os.path.normpath(raw)
        if normalized == ".." or normalized.startswith(".." + os.sep):
            raise ValueError("grant 不允许 .. 逃逸: %s" % raw)
        resolved = os.path.realpath(os.path.join(self.run_dir, normalized))
        if not _is_within(resolved, self.run_dir_real):
            raise ValueError("grant 解析后位于 run_dir 之外: %s -> %s" % (raw, resolved))
        if normalized not in self.grants:
            self.grants.append(normalized)
        return normalized

    def grant_many(self, paths: Sequence[str]) -> List[str]:
        return [self.grant(p) for p in paths]

    def revoke(self, relative_path: str) -> bool:
        normalized = os.path.normpath(str(relative_path))
        if normalized in self.grants:
            self.grants.remove(normalized)
            return True
        return False

    def allowed_grants(self) -> List[str]:
        return list(self.grants)

    # -- 判定 ------------------------------------------------------------
    def resolve(self, requested: str) -> str:
        """把请求路径解析为绝对 realpath。相对路径按 run_dir 拼接。"""

        raw = str(requested)
        candidate = raw if os.path.isabs(raw) else os.path.join(self.run_dir, raw)
        return os.path.realpath(candidate)

    def _grant_resolved(self, grant: str) -> str:
        return os.path.realpath(os.path.join(self.run_dir, grant))

    def check(self, requested: str) -> AccessDecision:
        """判定可否读取 ``requested``；每次都重新解析 realpath。"""

        raw = str(requested)
        resolved = self.resolve(raw)
        decision = self._decide(raw, resolved)
        self.audit.append(decision.to_dict())
        return decision

    def _decide(self, raw: str, resolved: str) -> AccessDecision:
        # 1) run_dir 之外：无论是否命中白名单都拒绝
        if not _is_within(resolved, self.run_dir_real):
            if os.path.isabs(raw):
                return AccessDecision(
                    False, raw, resolved, "绝对路径逃逸：解析到 run_dir 之外", kind="absolute-escape"
                )
            if _contains_link(self.run_dir, raw):
                return AccessDecision(
                    False, raw, resolved, "符号链接逃逸：链接目标在 run_dir 之外", kind="symlink-escape"
                )
            return AccessDecision(False, raw, resolved, "路径逃逸：解析到 run_dir 之外", kind="traversal")

        # 2) run_dir 之内但白名单未覆盖：默认拒绝
        for grant in self.grants:
            grant_resolved = self._grant_resolved(grant)
            if resolved == grant_resolved or _is_within(resolved, grant_resolved):
                return AccessDecision(True, raw, resolved, "命中白名单", matched_grant=grant, kind="granted")
        return AccessDecision(
            False,
            raw,
            resolved,
            "不在白名单内（默认拒绝）；已授予: %s" % (", ".join(self.grants) if self.grants else "无"),
            kind="not-granted",
        )

    # -- 读写 ------------------------------------------------------------
    def read(self, requested: str, encoding: str = "utf-8") -> str:
        """读取文本；被拒绝时抛 ``AccessDenied``，不存在时抛 ``FileNotFoundError``。"""

        decision = self.check(requested)
        if not decision.allowed:
            raise AccessDenied(self.instance_id, str(requested), decision.reason)
        with open(decision.resolved, "r", encoding=encoding) as handle:
            return handle.read()

    def check_write(self, requested: str) -> AccessDecision:
        """写权限判定。未授予写范围时一律拒绝。"""

        raw = str(requested)
        resolved = self.resolve(raw)
        base = self._decide(raw, resolved)
        if not base.allowed:
            return base
        if not self.can_write:
            decision = AccessDecision(False, raw, resolved, "该角色实例没有写权限", kind="write-disabled")
            self.audit.append(decision.to_dict())
            return decision
        for scope in self.write_scopes:
            scope_resolved = self._grant_resolved(scope)
            if resolved == scope_resolved or _is_within(resolved, scope_resolved):
                decision = AccessDecision(True, raw, resolved, "命中写范围", matched_grant=scope, kind="writable")
                self.audit.append(decision.to_dict())
                return decision
        decision = AccessDecision(
            False,
            raw,
            resolved,
            "不在写范围内；已授予写范围: %s" % (", ".join(self.write_scopes) if self.write_scopes else "无"),
            kind="write-not-granted",
        )
        self.audit.append(decision.to_dict())
        return decision

    def write(self, requested: str, content: str, encoding: str = "utf-8") -> str:
        """写文本；被拒绝时抛 ``AccessDenied``。"""

        decision = self.check_write(requested)
        if not decision.allowed:
            raise AccessDenied(self.instance_id, str(requested), decision.reason)
        os.makedirs(os.path.dirname(decision.resolved), exist_ok=True)
        with open(decision.resolved, "w", encoding=encoding) as handle:
            handle.write(content)
        return decision.resolved

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_dir": self.run_dir,
            "instance_id": self.instance_id,
            "role": self.role.value,
            "grants": list(self.grants),
            "can_write": self.can_write,
            "write_scopes": list(self.write_scopes),
            "audit": list(self.audit),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AccessPolicy":
        policy = cls(
            run_dir=str(data["run_dir"]),
            instance_id=str(data["instance_id"]),
            role=Role(str(data.get("role", Role.EXPLORER.value))),
            grants=[str(g) for g in data.get("grants", [])],
            can_write=bool(data.get("can_write", False)),
            write_scopes=[str(g) for g in data.get("write_scopes", [])],
        )
        policy.audit = [dict(a) for a in data.get("audit", [])]
        return policy


def explorer_policy(run_dir: str, instance_id: str, group: str) -> AccessPolicy:
    """探索者初始边界：只读契约与自己组的目录（P5 的"各组初期只读契约和自己的记录"）。"""

    policy = AccessPolicy(run_dir, instance_id, Role.EXPLORER, can_write=True)
    policy.grant("contract.json")
    policy.grant(os.path.join("groups", group))
    policy.write_scopes = [os.path.join("groups", group), "evidence"]
    return policy


def synthesizer_policy(run_dir: str, instance_id: str, groups: Sequence[str]) -> AccessPolicy:
    """综合者边界：读契约 + 指定组目录；写范围仅 ``synthesis/``。"""

    policy = AccessPolicy(run_dir, instance_id, Role.SYNTHESIZER, can_write=True)
    policy.grant("contract.json")
    for group in groups:
        policy.grant(os.path.join("groups", group))
    policy.write_scopes = ["synthesis"]
    return policy


def verifier_policy(run_dir: str, instance_id: str, candidate_group: str, role: Role = Role.TECHNICAL_VERIFIER) -> AccessPolicy:
    """验证者/审查者边界：读契约、候选组目录、自身报告目录；写范围仅自己的报告目录。"""

    report_dir = "verification" if role is Role.TECHNICAL_VERIFIER else "alignment"
    policy = AccessPolicy(run_dir, instance_id, role, can_write=True)
    policy.grant("contract.json")
    policy.grant(os.path.join("groups", candidate_group))
    policy.grant(report_dir)
    policy.write_scopes = [report_dir]
    return policy


def _contains_link(root: str, rel: str) -> bool:
    """rel（相对 root 的原始请求路径）中任一前缀是否为符号链接。

    只看请求路径本身，不跟随 ``..``，因此不会把普通的 ``../../etc/passwd``
    误报为符号链接逃逸。
    """

    parts = [p for p in os.path.normpath(rel).split(os.sep) if p not in ("", ".")]
    if parts and parts[0] == "..":
        return False
    current = root
    for part in parts:
        current = os.path.join(current, part)
        if os.path.islink(current):
            return True
    return False
