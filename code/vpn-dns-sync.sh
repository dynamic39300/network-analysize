#!/bin/bash
# VPN DNS 自动同步守护脚本
# 功能：检测公司 VPN 连接状态，自动切换 DNS
#   - VPN 连接时：使用公司内网 DNS（10.0.0.66, 10.0.0.68）
#   - VPN 断开时：使用公共 DNS（223.5.5.5, 114.114.114.114）
# 用法：./vpn-dns-sync.sh [start|stop|status]

# ==================== 配置 ====================
COMPANY_DNS=("10.0.0.66" "10.0.0.68")
PUBLIC_DNS=("223.5.5.5" "114.114.114.114")
NETWORK_SERVICE="Wi-Fi"
VPN_INTERFACE="utun4"
CHECK_INTERVAL=10  # 检测间隔（秒）
LOG_FILE="$(dirname "$0")/vpn-dns-sync.log"
PID_FILE="$(dirname "$0")/vpn-dns-sync.pid"

# ==================== 函数 ====================

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

# 检测 VPN 是否连接
is_vpn_connected() {
    # 检查 utun4 接口是否存在且有 inet 地址
    if ifconfig "$VPN_INTERFACE" 2>/dev/null | grep -q 'inet '; then
        return 0
    fi
    return 1
}

# 获取当前 DNS 列表
get_current_dns() {
    networksetup -getdnsservers "$NETWORK_SERVICE" 2>/dev/null | grep -v 'There aren' | tr '\n' ' '
}

# 检查当前 DNS 是否包含指定 DNS
dns_contains() {
    local current="$1"
    local target="$2"
    if echo "$current" | grep -q "$target"; then
        return 0
    fi
    return 1
}

# 检查当前 DNS 是否正好是目标 DNS（数量和内容都匹配）
dns_exactly() {
    local current="$1"
    shift
    local target=("$@")
    local current_count
    current_count=$(echo "$current" | tr ' ' '\n' | grep -c .)
    local target_count=${#target[@]}
    
    # 数量不同则不匹配
    if [ "$current_count" != "$target_count" ]; then
        return 1
    fi
    
    # 检查每个目标 DNS 是否都在当前列表中
    for dns in "${target[@]}"; do
        if ! echo "$current" | grep -q "$dns"; then
            return 1
        fi
    done
    return 0
}

# 设置公司 DNS
set_company_dns() {
    networksetup -setdnsservers "$NETWORK_SERVICE" "${COMPANY_DNS[@]}" > /dev/null 2>&1
    if [ $? -eq 0 ]; then
        log "✓ DNS 已切换为公司 DNS: ${COMPANY_DNS[*]}"
        return 0
    else
        log "✗ 设置公司 DNS 失败"
        return 1
    fi
}

# 设置公共 DNS
set_public_dns() {
    networksetup -setdnsservers "$NETWORK_SERVICE" "${PUBLIC_DNS[@]}" > /dev/null 2>&1
    if [ $? -eq 0 ]; then
        log "✓ DNS 已切换为公共 DNS: ${PUBLIC_DNS[*]}"
        return 0
    else
        log "✗ 设置公共 DNS 失败"
        return 1
    fi
}

# 清空 DNS 缓存
flush_dns_cache() {
    dscacheutil -flushcache 2>/dev/null
    killall -HUP mDNSResponder 2>/dev/null
    log "✓ DNS 缓存已清空"
}

# 主循环
daemon_loop() {
    log "========== VPN DNS 同步守护进程启动 =========="
    log "VPN 接口: $VPN_INTERFACE"
    log "公司 DNS: ${COMPANY_DNS[*]}"
    log "公共 DNS: ${PUBLIC_DNS[*]}"
    log "检测间隔: ${CHECK_INTERVAL}秒"
    
    local last_state=""
    
    while true; do
        if is_vpn_connected; then
            current_state="connected"
            current_dns=$(get_current_dns)
            
            if [ "$last_state" != "$current_state" ]; then
                log "→ 检测到 VPN 已连接"
                last_state="$current_state"
            fi
            
            # 检查是否需要切换 DNS（当前 DNS 不是正好的公司 DNS）
            if ! dns_exactly "$current_dns" "${COMPANY_DNS[@]}"; then
                log "当前 DNS: $current_dns"
                set_company_dns
                flush_dns_cache
            fi
        else
            current_state="disconnected"
            current_dns=$(get_current_dns)
            
            if [ "$last_state" != "$current_state" ]; then
                log "→ 检测到 VPN 已断开"
                last_state="$current_state"
            fi
            
            # 检查是否需要切换 DNS（当前 DNS 不是正好的公共 DNS）
            if ! dns_exactly "$current_dns" "${PUBLIC_DNS[@]}"; then
                log "当前 DNS: $current_dns"
                set_public_dns
                flush_dns_cache
            fi
        fi
        
        sleep "$CHECK_INTERVAL"
    done
}

# ==================== 命令处理 ====================

case "${1:-}" in
    start)
        # 检查是否已在运行
        if [ -f "$PID_FILE" ]; then
            old_pid=$(cat "$PID_FILE")
            if kill -0 "$old_pid" 2>/dev/null; then
                echo "VPN DNS 同步已在运行中 (PID: $old_pid)"
                echo "日志文件: $LOG_FILE"
                exit 0
            else
                rm -f "$PID_FILE"
            fi
        fi
        
        echo "启动 VPN DNS 同步守护进程..."
        # 后台运行
        nohup "$0" daemon > /dev/null 2>&1 &
        echo $! > "$PID_FILE"
        sleep 1
        
        if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo "✓ 启动成功 (PID: $(cat "$PID_FILE"))"
            echo "日志文件: $LOG_FILE"
            echo "使用 '$0 status' 查看状态"
            echo "使用 '$0 stop' 停止"
        else
            echo "✗ 启动失败，请查看日志: $LOG_FILE"
            exit 1
        fi
        ;;
    
    stop)
        if [ -f "$PID_FILE" ]; then
            pid=$(cat "$PID_FILE")
            if kill -0 "$pid" 2>/dev/null; then
                echo "停止 VPN DNS 同步守护进程 (PID: $pid)..."
                kill "$pid"
                rm -f "$PID_FILE"
                echo "✓ 已停止"
                # 恢复公共 DNS
                echo "恢复公共 DNS..."
                networksetup -setdnsservers "$NETWORK_SERVICE" "${PUBLIC_DNS[@]}" > /dev/null 2>&1
                dscacheutil -flushcache 2>/dev/null
                killall -HUP mDNSResponder 2>/dev/null
                echo "✓ DNS 已恢复为公共 DNS: ${PUBLIC_DNS[*]}"
            else
                echo "进程不存在，清理 PID 文件"
                rm -f "$PID_FILE"
            fi
        else
            echo "VPN DNS 同步未在运行"
        fi
        ;;
    
    status)
        echo "=== VPN DNS 同步状态 ==="
        if [ -f "$PID_FILE" ]; then
            pid=$(cat "$PID_FILE")
            if kill -0 "$pid" 2>/dev/null; then
                echo "运行状态: ✓ 运行中 (PID: $pid)"
            else
                echo "运行状态: ✗ 进程已退出 (PID 文件残留)"
            fi
        else
            echo "运行状态: ✗ 未运行"
        fi
        echo ""
        echo "VPN 状态: $(is_vpn_connected && echo '✓ 已连接' || echo '✗ 未连接')"
        echo "当前 DNS: $(get_current_dns)"
        echo ""
        echo "最近 10 条日志:"
        if [ -f "$LOG_FILE" ]; then
            tail -10 "$LOG_FILE"
        else
            echo "  (无日志)"
        fi
        ;;
    
    daemon)
        # 内部命令：守护进程模式
        daemon_loop
        ;;
    
    *)
        echo "VPN DNS 自动同步工具"
        echo ""
        echo "用法: $0 [命令]"
        echo ""
        echo "命令:"
        echo "  start    启动守护进程（后台运行）"
        echo "  stop     停止守护进程"
        echo "  status   查看运行状态和日志"
        echo ""
        echo "功能说明:"
        echo "  - VPN 连接时自动切换为公司 DNS (${COMPANY_DNS[*]})"
        echo "  - VPN 断开时自动恢复公共 DNS (${PUBLIC_DNS[*]})"
        echo "  - 每 ${CHECK_INTERVAL} 秒检测一次"
        echo ""
        echo "日志文件: $LOG_FILE"
        ;;
esac
