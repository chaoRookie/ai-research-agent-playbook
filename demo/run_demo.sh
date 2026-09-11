#!/usr/bin/env bash
# 端到端离线演示的薄包装：从仓库根目录运行。
#
#   bash demo/run_demo.sh                 # 产物写到 run/demo（run/ 已在 .gitignore 中）
#   bash demo/run_demo.sh --run-dir /tmp/x --keep
#
# 退出码与 `python3 -m orchestrator demo` 一致：全部不变量守住才为 0。
set -euo pipefail

# 本脚本位于 <repo>/demo/，因此仓库根目录是它的上一级。
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [ "$#" -eq 0 ]; then
  set -- --run-dir run/demo --keep
fi

echo "[run_demo] repo root: $REPO_ROOT"
echo "[run_demo] python:    $(command -v python3) ($(python3 --version 2>&1))"
# `python3 -m orchestrator` 要求包目录的父目录位于 import path 上。
PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m orchestrator demo "$@"
