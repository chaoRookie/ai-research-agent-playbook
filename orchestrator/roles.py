"""角色提示词渲染：读取 ``templates/prompts.md`` 并把 ``{占位符}`` 填成具体提示词。

设计
----
* 模板是**中文 Markdown**，四类角色的定义写在 ``## 探索者`` 等标题下。
* ``render_prompt`` 只做机械替换：模板里出现的占位符若没被提供，就抛 ``TemplateError``
  并列出缺哪些，绝不静默留下 ``{xxx}``。
* 模板缺失或无法解析时，返回可诊断的 ``TemplateError``（而不是崩溃或伪造内容）。
  这是"优雅降级"：宿主可以据此改用自带提示词，但不会拿到看似正常实则空转的字符串。

**提示词不是访问控制系统。** 边界由 ``isolation.AccessPolicy`` 与宿主权限实现；
这里生成的文本只是约定。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .model import Role

__all__ = [
    "TemplateError",
    "ROLE_SECTIONS",
    "ROLE_PARAMETERS",
    "default_template_path",
    "load_template",
    "split_sections",
    "render_prompt",
    "render_all",
    "available_roles",
]

#: 模板标题 → 角色枚举
ROLE_SECTIONS: Dict[str, Role] = {
    "探索者": Role.EXPLORER,
    "综合者": Role.SYNTHESIZER,
    "技术验证者": Role.TECHNICAL_VERIFIER,
    "对齐审查者": Role.ALIGNMENT_REVIEWER,
}

ROLE_ALIASES: Dict[str, Role] = {
    "explorer": Role.EXPLORER,
    "synthesizer": Role.SYNTHESIZER,
    "technical_verifier": Role.TECHNICAL_VERIFIER,
    "verifier": Role.TECHNICAL_VERIFIER,
    "alignment_reviewer": Role.ALIGNMENT_REVIEWER,
    "alignment": Role.ALIGNMENT_REVIEWER,
    "reviewer": Role.ALIGNMENT_REVIEWER,
}

#: 每个角色"通常需要"的参数，仅用于 CLI 的 --help 说明与缺参提示。
ROLE_PARAMETERS: Dict[Role, Tuple[str, ...]] = {
    Role.EXPLORER: ("method", "contract", "seeds", "budget"),
    Role.SYNTHESIZER: ("contract", "records", "budget", "seed_limit"),
    Role.TECHNICAL_VERIFIER: ("contract", "candidate", "evidence", "budget"),
    Role.ALIGNMENT_REVIEWER: ("contract", "candidate", "verification", "budget"),
}

PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


class TemplateError(RuntimeError):
    """模板缺失、无法解析或占位符未提供。"""


def default_template_path() -> Optional[str]:
    """在若干候选位置里寻找 ``templates/prompts.md``，找到返回路径，否则 None。

    ``ORCHESTRATOR_TEMPLATE`` 一旦设置就是**权威路径**（即使不存在也照样返回），
    这样"显式指定了模板但路径写错"会报明确错误，而不是悄悄回退到仓库内模板。
    """

    env = os.environ.get("ORCHESTRATOR_TEMPLATE", "").strip()
    if env:
        return str(Path(env).expanduser())

    package_dir = Path(__file__).resolve().parent
    candidates: List[Path] = [
        package_dir.parent / "templates" / "prompts.md",
        package_dir / "templates" / "prompts.md",
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:  # pragma: no cover
            continue
    return None


def load_template(path: Optional[str] = None) -> str:
    """读取模板文本；找不到时抛 ``TemplateError``（消息给出尝试过的位置）。"""

    target = path or default_template_path()
    if target is None:
        raise TemplateError(
            "找不到角色模板 templates/prompts.md；请用 ORCHESTRATOR_TEMPLATE 环境变量指定路径，"
            "或直接向 render_prompt 传入模板文本"
        )
    try:
        with open(target, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise TemplateError("角色模板无法读取 (%s): %s" % (target, exc))


def split_sections(text: str) -> Dict[str, str]:
    """把 Markdown 按 ``## 标题`` 切成 {标题: 正文}。"""

    sections: Dict[str, str] = {}
    matches = list(HEADING_RE.finditer(text))
    for index, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[title] = text[start:end].strip()
    return sections


def _find_section(sections: Mapping[str, str], role: Role) -> Tuple[str, str]:
    for title, body in sections.items():
        for label, mapped in ROLE_SECTIONS.items():
            if mapped is role and label in title:
                return title, body
        for alias, mapped in ROLE_ALIASES.items():
            if mapped is role and alias in title.lower():
                return title, body
    raise TemplateError(
        "模板中找不到角色 %s 的小节；现有小节: %s"
        % (role.value, ", ".join(sections) if sections else "（无）")
    )


def resolve_role(role: Any) -> Role:
    if isinstance(role, Role):
        return role
    key = str(role).strip()
    if key in ROLE_ALIASES:
        return ROLE_ALIASES[key]
    for label, mapped in ROLE_SECTIONS.items():
        if key == label:
            return mapped
    raise TemplateError("未知角色 %r；可用: %s" % (role, ", ".join(sorted(ROLE_ALIASES))))


@dataclass
class RenderedPrompt:
    """渲染结果：正文、小节标题、参数原文与缺失的占位符。"""

    role: Role
    section_title: str
    text: str
    parameters: Dict[str, str] = field(default_factory=dict)
    template_path: str = ""
    missing: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role.value,
            "section_title": self.section_title,
            "template_path": self.template_path,
            "parameters": dict(self.parameters),
            "missing": list(self.missing),
            "text": self.text,
        }


def render_prompt(
    role: Any,
    parameters: Mapping[str, Any],
    template_text: Optional[str] = None,
    template_path: Optional[str] = None,
) -> RenderedPrompt:
    """把角色模板渲染成填好参数的提示词。"""

    resolved = resolve_role(role)
    text = template_text if template_text is not None else load_template(template_path)
    sections = split_sections(text)
    if not sections:
        raise TemplateError("模板里没有解析到任何 ``##`` 小节，无法定位角色定义")
    title, body = _find_section(sections, resolved)

    values = {str(k): str(v) for k, v in parameters.items()}
    found: List[str] = []
    missing: List[str] = []

    def _replace(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key in values:
            found.append(key)
            return values[key]
        missing.append(key)
        return match.group(0)

    rendered = PLACEHOLDER_RE.sub(_replace, body)
    if missing:
        unique = sorted(set(missing))
        raise TemplateError(
            "角色 %s 的模板占位符未提供: %s；该角色通常需要: %s"
            % (resolved.value, ", ".join(unique), ", ".join(ROLE_PARAMETERS.get(resolved, ())))
        )
    return RenderedPrompt(
        role=resolved,
        section_title=title,
        text=rendered.strip(),
        parameters=values,
        template_path=template_path or default_template_path() or "",
        missing=[],
    )


def render_all(
    parameters: Mapping[str, Mapping[str, Any]],
    template_text: Optional[str] = None,
    template_path: Optional[str] = None,
) -> Dict[Role, RenderedPrompt]:
    """按角色批量渲染；参数不足的角色会抛 ``TemplateError``。"""

    out: Dict[Role, RenderedPrompt] = {}
    for role in (
        Role.EXPLORER,
        Role.SYNTHESIZER,
        Role.TECHNICAL_VERIFIER,
        Role.ALIGNMENT_REVIEWER,
    ):
        out[role] = render_prompt(
            role, parameters.get(role, {}), template_text=template_text, template_path=template_path
        )
    return out


def available_roles(template_text: Optional[str] = None, template_path: Optional[str] = None) -> List[str]:
    """列出模板中实际可用的角色名（用于 CLI 提示）。"""

    text = template_text if template_text is not None else load_template(template_path)
    sections = split_sections(text)
    roles: List[str] = []
    for role in Role:
        try:
            _find_section(sections, role)
        except TemplateError:
            continue
        roles.append(role.value)
    return roles
