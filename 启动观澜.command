#!/usr/bin/env bash
# macOS 可双击入口；终端保持打开即可持续服务。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
printf '\n观澜 · 对话与长期记忆\n打开 http://127.0.0.1:8787 即可使用。\n按 Control-C 停止；再次启动会恢复同一份数据。\n\n'
exec bash "${ROOT:?}/run.sh" "$@"
