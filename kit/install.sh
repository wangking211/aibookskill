#!/usr/bin/env bash
# aibook-kit macOS / Linux 安装脚本
# 用法: bash install.sh [--token ak_xxx] [--force]
set -euo pipefail

KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${ROOT:-$KIT}"

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
  done
fi
if [ -z "$PY" ]; then
  echo "[ERR] 未找到 Python (>=3.9)，请先安装并加入 PATH" >&2
  exit 1
fi

echo "[run] $PY $KIT/setup.py --root $ROOT $*"
exec "$PY" "$KIT/setup.py" --root "$ROOT" "$@"
