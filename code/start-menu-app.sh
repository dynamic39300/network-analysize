#!/bin/bash
# 启动网络助手菜单栏应用
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_SCRIPT="$SCRIPT_DIR/network-doctor-menu.py"
PID_FILE="$SCRIPT_DIR/.menu-app.pid"

# 优先使用项目虚拟环境中的 Python（安装依赖后），否则回退到系统 python3
PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="python3"
fi

# 如果已由开机自启（launchd）管理，则不再手动重复启动
if launchctl print "gui/$(id -u)/com.wangxinlei.networkdoctor" >/dev/null 2>&1; then
    echo "✅ 网络助手已由开机自启（launchd）管理并运行"
    echo "   请查看菜单栏右上角的图标（🟢/🟡/🔴）"
    exit 0
fi

# 检查是否已在运行
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "✅ 网络助手已在运行中 (PID: $OLD_PID)"
        echo "   请查看菜单栏右上角的图标"
        exit 0
    else
        rm -f "$PID_FILE"
    fi
fi

echo "🚀 启动网络助手菜单栏应用..."
cd "$SCRIPT_DIR"
nohup "$PYTHON_BIN" "$APP_SCRIPT" > /dev/null 2>&1 &
APP_PID=$!
echo "$APP_PID" > "$PID_FILE"

sleep 2

if kill -0 "$APP_PID" 2>/dev/null; then
    echo "✅ 启动成功 (PID: $APP_PID)"
    echo "   请查看菜单栏右上角的图标（🟢/🟡/🔴）"
    echo "   点击图标查看网络状态和操作菜单"
else
    echo "❌ 启动失败，请检查日志: $SCRIPT_DIR/network-doctor-menu.log"
    exit 1
fi
