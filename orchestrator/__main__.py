"""命令行入口：``python3 -m orchestrator <cmd>``。

子命令
------
- ``demo``                          端到端离线演示（十个步骤，任一步失败则退出非零）
- ``selftest``                      离线断言五条不变量
- ``run --contract <path>``         从契约 JSON 新建运行目录并写入检查点
- ``status --run <dir>``            打印运行状态、候选与用量
- ``checkpoint --run <dir>``        打印（或用 ``--write`` 刷新）检查点摘要
- ``prompt --role <role> --method <name>``  渲染角色提示词

未知子命令由 argparse 以退出码 2 拒绝。``run`` 与 ``checkpoint`` 需要分别给出
``--contract`` / ``--run``，缺失时退出码也是 2。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from . import __version__
from .model import MODEL_UNKNOWN, Role
from .roles import TemplateError, default_template_path, render_prompt
from .state import RunState, StateError, load_contract, new_run

__all__ = ["main", "build_parser"]

ROLE_CHOICES = [role.value for role in Role]


def _default_run_dir(contract_path: str) -> str:
    base = os.path.splitext(os.path.basename(contract_path))[0] or "run"
    return os.path.join("run", base)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m orchestrator",
        description=(
            "ai-research-orchestration 的参考编排器：机械执行五条编排不变量"
            "（角色隔离 P5 / 预算账本 / 种子交换 P3-P6 / 独立验证 P8 / 检查点 P7）。"
        ),
        epilog=(
            "示例:\n"
            "  python3 -m orchestrator selftest\n"
            "  python3 -m orchestrator demo --run-dir run/demo --keep\n"
            "  python3 -m orchestrator run --contract run/demo/contract.json\n"
            "  python3 -m orchestrator status --run run/demo\n"
            "  python3 -m orchestrator checkpoint --run run/demo --write\n"
            "  python3 -m orchestrator prompt --role explorer --method invariant-first\n"
            "\n"
            "本工具不调用任何 LLM API，不需要网络，也不读取密钥或环境凭据。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version="orchestrator %s" % __version__)
    subparsers = parser.add_subparsers(dest="command", metavar="<cmd>")

    demo_parser = subparsers.add_parser(
        "demo", help="运行端到端离线演示（十个步骤，全部断言通过才退出 0）",
        description="运行离线演示；任一步断言失败即以非零码退出。",
    )
    demo_parser.add_argument("--run-dir", default=None, help="运行目录（默认：临时目录）")
    demo_parser.add_argument("--keep", action="store_true", help="保留产物，不清理临时目录")
    demo_parser.set_defaults(handler=_cmd_demo)

    selftest_parser = subparsers.add_parser(
        "selftest", help="离线断言五条不变量（plain 断言，无第三方测试框架）",
        description="运行内置断言；失败时退出 1 并列出失败项。",
    )
    selftest_parser.add_argument("--quiet", action="store_true", help="只打印失败项与汇总")
    selftest_parser.set_defaults(handler=_cmd_selftest)

    run_parser = subparsers.add_parser(
        "run", help="从契约 JSON 新建运行目录并落盘检查点",
        description="读取契约 JSON，创建 RunState 与预算账本，写入 contract.json 与 checkpoint.json。",
    )
    run_parser.add_argument("--contract", required=True, help="契约 JSON 路径（本包不依赖 PyYAML）")
    run_parser.add_argument("--run-dir", default=None, help="运行目录（默认：run/<契约文件名>）")
    run_parser.add_argument("--model-id", default=MODEL_UNKNOWN, help="宿主模型标识（记入检查点）")
    run_parser.set_defaults(handler=_cmd_run)

    status_parser = subparsers.add_parser(
        "status", help="打印运行状态、候选、门禁与用量",
        description="读取检查点并打印状态；不修改任何文件。",
    )
    status_parser.add_argument("--run", required=True, help="运行目录")
    status_parser.add_argument("--json", action="store_true", help="以 JSON 输出（便于宿主集成）")
    status_parser.set_defaults(handler=_cmd_status)

    checkpoint_parser = subparsers.add_parser(
        "checkpoint", help="查看检查点摘要；--write 时用当前状态刷新检查点",
        description="默认只读；--write 会原子重写 checkpoint.json（临时文件 + os.replace）。",
    )
    checkpoint_parser.add_argument("--run", required=True, help="运行目录")
    checkpoint_parser.add_argument("--write", action="store_true", help="用当前加载的状态刷新检查点")
    checkpoint_parser.add_argument("--note", default="", help="写入检查点的备注")
    checkpoint_parser.set_defaults(handler=_cmd_checkpoint)

    prompt_parser = subparsers.add_parser(
        "prompt", help="用 templates/prompts.md 渲染角色提示词",
        description=(
            "从模板渲染四类角色提示词。参数可用 --set k=v 覆盖；"
            "模板里出现但未提供的占位符会报错退出 3。"
        ),
    )
    prompt_parser.add_argument("--role", required=True, choices=ROLE_CHOICES, help="角色")
    prompt_parser.add_argument("--method", default="method-under-test", help="方法名（explorer 必填项之一）")
    prompt_parser.add_argument("--contract", default="contract.json", help="契约位置（写入提示词）")
    prompt_parser.add_argument("--records", default="groups/", help="探索结果位置（综合者）")
    prompt_parser.add_argument("--candidate", default="groups/<group>/candidate.py", help="候选产物位置")
    prompt_parser.add_argument("--evidence", default="groups/<group>/check.py", help="复现材料位置")
    prompt_parser.add_argument("--verification", default="verification/", help="技术报告位置（对齐审查者）")
    prompt_parser.add_argument("--seeds", default="（本轮未注入种子）", help="种子位置（探索者）")
    prompt_parser.add_argument("--budget", default="1 次调用", help="本角色预算描述")
    prompt_parser.add_argument("--seed-limit", type=int, default=2, help="综合者每组线索上限（默认 2）")
    prompt_parser.add_argument("--template", default=None, help="模板路径（默认自动查找 templates/prompts.md）")
    prompt_parser.add_argument(
        "--set", action="append", default=[], metavar="K=V", help="额外/覆盖的占位符，可重复"
    )
    prompt_parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    prompt_parser.set_defaults(handler=_cmd_prompt)

    return parser


# --------------------------------------------------------------------------
# 子命令实现
# --------------------------------------------------------------------------


def _cmd_demo(args: argparse.Namespace) -> int:
    from .demo import run_demo

    return run_demo(run_dir=args.run_dir, keep=args.keep)


def _cmd_selftest(args: argparse.Namespace) -> int:
    from .selftest import run_all

    return run_all(quiet=args.quiet)


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        contract = load_contract(args.contract)
    except StateError as exc:
        print("错误: %s" % exc, file=sys.stderr)
        return 2
    run_dir = args.run_dir or _default_run_dir(args.contract)
    state = new_run(run_dir, contract, budget=contract.budget, model_id=args.model_id)
    print("已创建运行目录: %s" % state.run_dir)
    print("契约: %s v%s" % (contract.identifier, contract.version))
    print("预算: max_calls=%d max_concurrency=%d max_minutes=%.1f paid_api_authorized=%s"
          % (contract.budget.max_calls, contract.budget.max_concurrency,
             contract.budget.max_minutes, contract.budget.paid_api_authorized))
    print("已写入: %s" % state.contract_path)
    print("已写入: %s" % state.checkpoint_path)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    try:
        state = RunState.load(args.run)
    except StateError as exc:
        print("错误: %s" % exc, file=sys.stderr)
        return 2
    if args.json:
        payload = {
            "metadata": state.metadata,
            "candidates": {
                cid: {"status": c.status.value, "version_hash": c.version_hash, "claim": c.claim}
                for cid, c in sorted(state.candidates.items())
            },
            "gates": {
                cid: state.gate(cid).to_dict() for cid in sorted(state.candidates)
            },
            "usage": state.ledger.usage_summary().to_dict(),
            "reverification_required": state.reverification_required,
            "needs_reverification": state.needs_reverification,
            "reverified_after_model_change": state.reverified_after_model_change,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    for line in state.status_lines():
        print(line)
    if state.candidates:
        print("门禁:")
        for candidate_id in sorted(state.candidates):
            print("  %s: %s" % (candidate_id, state.gate(candidate_id).explain()))
    return 0


def _cmd_checkpoint(args: argparse.Namespace) -> int:
    try:
        state = RunState.load(args.run)
    except StateError as exc:
        print("错误: %s" % exc, file=sys.stderr)
        return 2
    if args.write:
        path = state.checkpoint(note=args.note or "checkpoint 命令刷新")
        print("已刷新检查点: %s" % path)
    else:
        path = state.checkpoint_path
        print("检查点: %s" % path)
    size = os.path.getsize(path) if os.path.exists(path) else 0
    print("字节数: %d" % size)
    print("契约: %s v%s（哈希 %s）" % (state.contract.identifier, state.contract.version,
                                       state.contract.version_hash()))
    print("候选: %d 个" % len(state.candidates))
    for candidate_id, candidate in sorted(state.candidates.items()):
        print("  - %s [%s] %s" % (candidate_id, candidate.status.value, candidate.version_hash[:12]))
    print("检查: %d 条 | 技术报告: %d 份 | 对齐报告: %d 份 | 种子: %d 张"
          % (len(state.checks), len(state.verification_reports), len(state.alignment_reports), len(state.seeds)))
    print("账本: 调用 %d/%d，峰值并发 %d，耗时 %.3f 分钟，拒绝准入 %d 次"
          % (state.ledger.calls_used, state.ledger.budget.max_calls, state.ledger.peak_concurrency,
             state.ledger.minutes_used, state.ledger.admissions_refused))
    print("模型: %s（历史 %d 次变更）| 需要重验: %s | reverified_after_model_change: %s"
          % (state.model_id, len(state.model_history),
             ", ".join(state.needs_reverification) or "无", state.reverified_after_model_change))
    return 0


def _cmd_prompt(args: argparse.Namespace) -> int:
    parameters = {
        "method": args.method,
        "contract": args.contract,
        "records": args.records,
        "candidate": args.candidate,
        "evidence": args.evidence,
        "verification": args.verification,
        "seeds": args.seeds,
        "budget": args.budget,
        "seed_limit": args.seed_limit,
    }
    for item in args.set:
        if "=" not in item:
            print("错误: --set 需要 K=V 形式，收到 %r" % item, file=sys.stderr)
            return 2
        key, value = item.split("=", 1)
        parameters[key.strip()] = value
    try:
        rendered = render_prompt(args.role, parameters, template_path=args.template)
    except TemplateError as exc:
        print("错误: %s" % exc, file=sys.stderr)
        print("提示: 模板默认位置 %s；可用 --template 指定，或设置 ORCHESTRATOR_TEMPLATE"
              % (default_template_path() or "(未找到)"))
        return 3
    if args.json:
        print(json.dumps(rendered.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print("# 角色: %s（小节：%s）" % (rendered.role.value, rendered.section_title))
    print("# 模板: %s" % (rendered.template_path or "(内联)"))
    print("")
    print(rendered.text)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return int(handler(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
