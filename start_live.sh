#!/bin/bash
# 世界杯实时追踪一键启动
# 双击运行，或在终端执行：bash start_live.sh

cd "$(dirname "$0")"

echo "======================================"
echo "  2026 世界杯实时事件追踪"
echo "  按 Ctrl+C 停止"
echo "======================================"
echo ""

python3 live_events.py --today
