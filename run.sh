#!/usr/bin/env bash
# 使用独立数据目录运行本机工作台，不服务项目根目录。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  echo '需要 Python 3.9+，请先安装 Python。' >&2
  exit 1
fi
exec python3 "${ROOT:?}/server.py" "$@"
