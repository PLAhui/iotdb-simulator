#!/bin/bash
# IoTDB 数据模拟器启动脚本
set -e
cd "$(dirname "$0")"
PORT="${1:-8000}"
echo "启动 IoTDB 数据模拟器，浏览器访问 http://127.0.0.1:${PORT}"
exec python3 -m uvicorn server.app:app --host 127.0.0.1 --port "$PORT"
