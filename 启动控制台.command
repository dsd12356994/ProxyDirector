#!/bin/bash
# 双击启动 Agent 控制台（macOS）。已在运行则直接打开浏览器。
cd "$(dirname "$0")"
if lsof -i :4207 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "控制台已在运行"
else
  echo "启动控制台 http://localhost:4207 ..."
  .venv/bin/python ui/server.py > ui/logs/server.log 2>&1 &
  sleep 2
fi
open "http://localhost:4207"
