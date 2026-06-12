#!/usr/bin/env bash
# run.sh - Linux/macOS 启动脚本（对应 Windows 的 run.ps1）
# 后台启动 Python server(:8010)，再起 bun 前端；退出时自动收尾关闭 server。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export OPENAI_API_KEY="sk-dummy"
export OPENAI_BASE_URL="http://10.129.107.145:8001/v1"
export MODEL_NAME="Qwen_agent"
export PYTHONIOENCODING="utf-8"
export PATH="$PATH:$HOME/.bun/bin"
export ANTHROPIC_AUTH_TOKEN="sk-dummy"
export CALLER_DIR="$SCRIPT_DIR"

# 启动 Python server（后台），stdout+stderr 合并到单个 server.log
python -m interfaces.server > "$SCRIPT_DIR/server.log" 2>&1 &
SERVER_PID=$!
echo "Python server started (PID $SERVER_PID)"

# 无论前端如何退出，都关闭后台 server
cleanup() {
    echo "Stopping Python server..."
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT

sleep 3

cd "$SCRIPT_DIR/frontend"
bun run ./src/entrypoints/cli.tsx "$@"
