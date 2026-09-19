#!/bin/bash
# 停止网络助手菜单栏应用
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SCRIPT_DIR/.menu-app.pid"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "⏹  停止网络助手 (PID: $PID)..."
        kill "$PID"
        # 等待进程退出，超时后强制结束
        for i in 1 2 3 4 5; do
            kill -0 "$PID" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$PID" 2>/dev/null; then
            echo "   进程未响应，强制结束..."
            kill -9 "$PID"
        fi
        rm -f "$PID_FILE"
        echo "✅ 已停止"
    else
        echo "进程不存在，清理 PID 文件"
        rm -f "$PID_FILE"
    fi
else
    # 尝试通过进程名查找
    PIDS=$(pgrep -f "network-doctor-menu.py" 2>/dev/null)
    if [ -n "$PIDS" ]; then
        echo "⏹  停止网络助手进程: $PIDS"
        kill $PIDS
        echo "✅ 已停止"
    else
        echo "ℹ️  网络助手未在运行"
    fi
fi
