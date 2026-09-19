#!/bin/bash
# ============================================================
# 网络诊断与修复工具 (Network Doctor)
# 版本: v1.0 MVP
# 功能: 状态监测 + 可达性测试 + 问题诊断 + 一键修复 + 自检
# 用法:
#   ./network-doctor.sh          # 交互菜单
#   ./network-doctor.sh --quick  # 快速检测
#   ./network-doctor.sh --fix    # 一键修复
#   ./network-doctor.sh --status # 状态摘要
# ============================================================

set -o pipefail

# ==================== 配置区 ====================
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$SCRIPT_DIR/network-doctor.log"
REPORT_FILE="$SCRIPT_DIR/network-report-$(date +%Y%m%d_%H%M%S).txt"

# DNS 配置
COMPANY_DNS=("10.0.0.66" "10.0.0.68")
PUBLIC_DNS=("223.5.5.5" "114.114.114.114")

# 网络接口
NETWORK_SERVICE="Wi-Fi"
VPN_INTERFACE="utun4"

# 测试目标
DOMESTIC_URLS=("https://www.baidu.com" "https://www.zhihu.com")
FOREIGN_URLS=("https://www.google.com" "https://www.youtube.com" "https://github.com")
INTRANET_IPS=("10.0.0.66" "10.0.0.76")
AI_OFFICIAL_URLS=("https://api.openai.com/v1/models" "https://api.anthropic.com/v1/messages")
AI_RELAY_URLS=("https://muemod.top" "https://dragtokens.com")
LOCAL_SERVICES=("127.0.0.1:15721" "127.0.0.1:7890")

# 超时设置
CURL_TIMEOUT=8
PING_TIMEOUT=3

# ==================== 颜色输出 ====================
if [ -t 1 ]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    CYAN='\033[0;36m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    RED='' GREEN='' YELLOW='' BLUE='' CYAN='' BOLD='' NC=''
fi

# 全局变量，存储检测结果
# 状态变量使用 STATUS_xxx 形式
# 可达性变量使用 REACH_xxx 形式
declare -a ISSUES
declare -a FIX_ACTIONS

# ==================== 工具函数 ====================

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

print_header() {
    echo ""
    echo -e "${CYAN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}${BOLD}  $1${NC}"
    echo -e "${CYAN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
}

print_ok()   { echo -e "  ${GREEN}✅${NC} $1"; }
print_warn() { echo -e "  ${YELLOW}⚠️  $1${NC}"; }
print_err()  { echo -e "  ${RED}🔴 $1${NC}"; }
print_info() { echo -e "  ${BLUE}ℹ️  $1${NC}"; }

add_issue() {
    local severity="$1"
    local desc="$2"
    local fix="$3"
    ISSUES+=("${severity}|${desc}|${fix}")
    log "发现问题: [$severity] $desc"
}

# ==================== 状态检测模块 ====================

check_wifi() {
    print_header "1. Wi-Fi 状态检测"
    
    local wifi_power
    wifi_power=$(networksetup -getairportpower en0 2>/dev/null | awk -F': ' '{print $2}')
    local wifi_network
    wifi_network=$(networksetup -getairportnetwork en0 2>/dev/null | sed 's/Current Wi-Fi Network: //')
    local wifi_ip
    wifi_ip=$(ifconfig en0 2>/dev/null | grep 'inet ' | awk '{print $2}')
    
    if [ "$wifi_power" = "On" ] && [ -n "$wifi_ip" ]; then
        print_ok "Wi-Fi 已连接"
        print_info "网络名称: ${wifi_network:-未知}"
        print_info "IP 地址: ${wifi_ip:-未获取}"
        STATUS_wifi="ok"
        STATUS_wifi_network="$wifi_network"
        STATUS_wifi_ip="$wifi_ip"
    else
        print_err "Wi-Fi 未连接或无 IP"
        STATUS_wifi="error"
        add_issue "high" "Wi-Fi 未连接或无 IP 地址" "检查 Wi-Fi 开关和网络连接"
    fi
}

check_vpn() {
    print_header "2. 公司 VPN 状态检测"
    
    if pgrep -f -i UniVPN > /dev/null 2>&1; then
        local vpn_ip
        vpn_ip=$(ifconfig "$VPN_INTERFACE" 2>/dev/null | grep 'inet ' | awk '{print $2}')
        
        if [ -n "$vpn_ip" ]; then
            print_ok "公司 VPN 已连接"
            print_info "接口: $VPN_INTERFACE"
            print_info "虚拟 IP: $vpn_ip"
            STATUS_vpn="ok"
            STATUS_vpn_ip="$vpn_ip"
        else
            print_warn "VPN 进程在运行，但隧道未建立（无虚拟 IP）"
            STATUS_vpn="warning"
            add_issue "high" "VPN 假连接：进程在运行但隧道未建立" "断开 VPN 重新连接"
        fi
    else
        print_info "公司 VPN 未运行"
        STATUS_vpn="off"
    fi
}

check_clashx() {
    print_header "3. ClashX 状态检测"
    
    if pgrep -f -i clash > /dev/null 2>&1; then
        local port_listening
        port_listening=$(lsof -nP -iTCP:7890 -sTCP:LISTEN 2>/dev/null | head -1)
        
        if [ -n "$port_listening" ]; then
            print_ok "ClashX 运行中，代理端口 7890 已监听"
            STATUS_clashx="ok"
        else
            print_warn "ClashX 运行中，但代理端口未监听"
            STATUS_clashx="warning"
            add_issue "medium" "ClashX 运行但代理端口未监听" "重启 ClashX 或检查配置"
        fi
    else
        print_info "ClashX 未运行"
        STATUS_clashx="off"
    fi
}

check_dns() {
    print_header "4. DNS 配置检测"
    
    local dns_servers
    dns_servers=$(networksetup -getdnsservers "$NETWORK_SERVICE" 2>/dev/null | grep -v 'There aren' | tr '\n' ' ')
    local dns_count
    dns_count=$(echo "$dns_servers" | tr ' ' '\n' | grep -c .)
    
    print_info "当前 DNS 服务器 ($dns_count 个):"
    for dns in $dns_servers; do
        echo -e "    ${CYAN}•${NC} $dns"
    done
    
    STATUS_dns_servers="$dns_servers"
    STATUS_dns_count="$dns_count"
    
    # 智能判断 DNS 配置
    if [ "${STATUS_vpn}" = "ok" ]; then
        # VPN 连接时，应该只用公司 DNS
        if echo "$dns_servers" | grep -q "223.5.5.5\|114.114.114.114"; then
            print_warn "VPN 已连接，但 DNS 混合了公共 DNS，可能导致外网访问不稳定"
            add_issue "medium" "DNS 混合配置：VPN 连接时混入公共 DNS，导致外网不稳定" "切换为仅公司 DNS (10.0.0.66, 10.0.0.68)"
            STATUS_dns="warning"
        elif echo "$dns_servers" | grep -q "10.0.0.66"; then
            print_ok "DNS 配置正确（仅公司 DNS）"
            STATUS_dns="ok"
        else
            print_err "VPN 已连接，但 DNS 未包含公司 DNS"
            add_issue "high" "VPN 连接但 DNS 未切换到公司 DNS" "切换为公司 DNS (10.0.0.66, 10.0.0.68)"
            STATUS_dns="error"
        fi
    else
        # VPN 未连接时，应该用公共 DNS
        if echo "$dns_servers" | grep -q "10.0.0.66"; then
            print_warn "VPN 未连接，但 DNS 仍指向公司内网 DNS，可能导致解析失败"
            add_issue "medium" "VPN 断开但 DNS 残留公司 DNS" "恢复公共 DNS (223.5.5.5, 114.114.114.114)"
            STATUS_dns="warning"
        else
            print_ok "DNS 配置正确（公共 DNS）"
            STATUS_dns="ok"
        fi
    fi
}

check_ipv6() {
    print_header "5. IPv6 状态检测"
    
    local ipv6_status
    ipv6_status=$(networksetup -getinfo "$NETWORK_SERVICE" 2>/dev/null | grep -i 'IPv6' | awk -F': ' '{print $2}')
    
    if [ "$ipv6_status" = "Off" ] || [ -z "$ipv6_status" ]; then
        print_ok "IPv6 已禁用"
        STATUS[ipv6]="off"
    else
        print_warn "IPv6 已启用 ($ipv6_status)，可能导致 Codex 等工具绕过代理直连"
        add_issue "medium" "IPv6 已启用，可能导致 AI 工具绕过代理触发地域限制" "禁用 Wi-Fi IPv6"
        STATUS[ipv6]="on"
    fi
}

check_proxy() {
    print_header "6. 系统代理检测"
    
    local http_enable https_enable socks_enable
    http_enable=$(scutil --proxy 2>/dev/null | grep 'HTTPEnable' | awk '{print $3}')
    https_enable=$(scutil --proxy 2>/dev/null | grep 'HTTPSEnable' | awk '{print $3}')
    socks_enable=$(scutil --proxy 2>/dev/null | grep 'SOCKSEnable' | awk '{print $3}')
    
    local proxy_on=false
    if [ "$http_enable" = "1" ] || [ "$https_enable" = "1" ] || [ "$socks_enable" = "1" ]; then
        proxy_on=true
    fi
    
    if [ "$proxy_on" = true ]; then
        local http_proxy https_proxy
        http_proxy=$(scutil --proxy 2>/dev/null | grep -A1 'HTTPProxy' | head -2 | tail -1 | awk '{print $3}')
        https_proxy=$(scutil --proxy 2>/dev/null | grep -A1 'HTTPSProxy' | head -2 | tail -1 | awk '{print $3}')
        print_warn "系统代理已开启 (HTTP: $http_enable, HTTPS: $https_enable, SOCKS: $socks_enable)"
        
        if [ "${STATUS_clashx}" = "off" ]; then
            print_err "ClashX 未运行但系统代理已开启，会导致所有网站无法访问！"
            add_issue "high" "代理残留：ClashX 未运行但系统代理开启" "关闭系统代理"
            STATUS_proxy="error"
        else
            STATUS_proxy="on"
        fi
    else
        print_ok "系统代理已关闭"
        STATUS_proxy="off"
    fi
}

check_exit_ip() {
    print_header "7. 出口 IP 检测"
    
    local exit_ip
    exit_ip=$(curl -s --max-time 10 --noproxy '*' https://api.ipify.org 2>/dev/null)
    
    if [ -n "$exit_ip" ]; then
        print_info "出口 IP: $exit_ip"
        STATUS_exit_ip="$exit_ip"
        
        # 简单判断是否为国内 IP
        if echo "$exit_ip" | grep -qE '^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.)'; then
            print_warn "出口 IP 为内网地址，可能存在代理或 VPN 异常"
        fi
    else
        print_warn "无法获取出口 IP（可能网络不通）"
        STATUS_exit_ip="unknown"
    fi
}

# ==================== 可达性测试模块 ====================

test_url() {
    local url="$1"
    local label="$2"
    local use_proxy="${3:-false}"
    
    local curl_args=(-s --max-time "$CURL_TIMEOUT" -o /dev/null -w "%{http_code}|%{time_total}")
    
    if [ "$use_proxy" = "false" ]; then
        curl_args+=(--noproxy '*')
    fi
    
    local result
    result=$(curl "${curl_args[@]}" "$url" 2>/dev/null)
    local http_code=$(echo "$result" | cut -d'|' -f1)
    local time_total=$(echo "$result" | cut -d'|' -f2)
    
    if [ "$http_code" = "000" ] || [ -z "$http_code" ]; then
        print_err "$label: 连接失败/超时"
        return 1
    elif [ "$http_code" -ge 400 ] && [ "$http_code" != "401" ] && [ "$http_code" != "403" ]; then
        print_warn "$label: HTTP $http_code (${time_total}s)"
        return 2
    else
        print_ok "$label: HTTP $http_code (${time_total}s)"
        return 0
    fi
}

test_ping() {
    local ip="$1"
    local label="$2"
    
    if ping -c 1 -t "$PING_TIMEOUT" "$ip" > /dev/null 2>&1; then
        print_ok "$label ($ip): 可达"
        return 0
    else
        print_err "$label ($ip): 不可达"
        return 1
    fi
}

test_port() {
    local addr="$1"
    local label="$2"
    local host port
    host=$(echo "$addr" | cut -d: -f1)
    port=$(echo "$addr" | cut -d: -f2)
    
    if nc -z -w 3 "$host" "$port" 2>/dev/null; then
        print_ok "$label ($addr): 端口开放"
        return 0
    else
        print_warn "$label ($addr): 端口未开放"
        return 1
    fi
}

run_reachability_tests() {
    print_header "8. 可达性测试"
    
    echo -e "${BOLD}  [国内网站]${NC}"
    for url in "${DOMESTIC_URLS[@]}"; do
        test_url "$url" "$(echo "$url" | sed 's|https://||')"
    done
    
    echo ""
    echo -e "${BOLD}  [国外网站]${NC}"
    for url in "${FOREIGN_URLS[@]}"; do
        test_url "$url" "$(echo "$url" | sed 's|https://||')"
    done
    
    echo ""
    echo -e "${BOLD}  [公司内网]${NC}"
    for ip in "${INTRANET_IPS[@]}"; do
        test_ping "$ip" "内网节点"
    done
    
    echo ""
    echo -e "${BOLD}  [AI 官方 API]${NC}"
    for url in "${AI_OFFICIAL_URLS[@]}"; do
        test_url "$url" "$(echo "$url" | sed 's|https://||;s|/v1/.*||')"
    done
    
    echo ""
    echo -e "${BOLD}  [AI 中转站]${NC}"
    for url in "${AI_RELAY_URLS[@]}"; do
        test_url "$url" "$(echo "$url" | sed 's|https://||')"
    done
    
    echo ""
    echo -e "${BOLD}  [本地服务]${NC}"
    for addr in "${LOCAL_SERVICES[@]}"; do
        local name="CC Switch"
        [ "$addr" = "127.0.0.1:7890" ] && name="ClashX 代理"
        test_port "$addr" "$name"
    done
}

# ==================== 问题诊断模块 ====================

diagnose_issues() {
    print_header "9. 问题诊断汇总"
    
    if [ ${#ISSUES[@]} -eq 0 ]; then
        print_ok "未发现明显问题，网络状态良好！"
        return
    fi
    
    local high_count=0 medium_count=0
    for issue in "${ISSUES[@]}"; do
        local severity=$(echo "$issue" | cut -d'|' -f1)
        [ "$severity" = "high" ] && high_count=$((high_count+1))
        [ "$severity" = "medium" ] && medium_count=$((medium_count+1))
    done
    
    echo -e "  共发现 ${RED}$high_count 个严重问题${NC}，${YELLOW}$medium_count 个警告${NC}"
    echo ""
    
    local idx=1
    for issue in "${ISSUES[@]}"; do
        local severity=$(echo "$issue" | cut -d'|' -f1)
        local desc=$(echo "$issue" | cut -d'|' -f2)
        local fix=$(echo "$issue" | cut -d'|' -f3)
        
        if [ "$severity" = "high" ]; then
            echo -e "  ${RED}[$idx] 🔴 严重${NC}: $desc"
        else
            echo -e "  ${YELLOW}[$idx] ⚠️  警告${NC}: $desc"
        fi
        echo -e "      ${BLUE}建议: $fix${NC}"
        echo ""
        idx=$((idx+1))
    done
}

# ==================== 一键修复模块 ====================

fix_dns() {
    print_info "修复 DNS 配置..."
    
    if [ "${STATUS_vpn}" = "ok" ]; then
        networksetup -setdnsservers "$NETWORK_SERVICE" "${COMPANY_DNS[@]}" > /dev/null 2>&1
        print_ok "DNS 已切换为公司 DNS: ${COMPANY_DNS[*]}"
    else
        networksetup -setdnsservers "$NETWORK_SERVICE" "${PUBLIC_DNS[@]}" > /dev/null 2>&1
        print_ok "DNS 已恢复为公共 DNS: ${PUBLIC_DNS[*]}"
    fi
    FIX_ACTIONS+=("DNS 修复")
}

fix_proxy() {
    print_info "关闭残留系统代理..."
    networksetup -setwebproxystate "$NETWORK_SERVICE" off > /dev/null 2>&1
    networksetup -setsecurewebproxystate "$NETWORK_SERVICE" off > /dev/null 2>&1
    networksetup -setsocksfirewallproxystate "$NETWORK_SERVICE" off > /dev/null 2>&1
    print_ok "系统代理已全部关闭"
    FIX_ACTIONS+=("代理修复")
}

fix_ipv6() {
    print_info "禁用 Wi-Fi IPv6..."
    networksetup -setv6off "$NETWORK_SERVICE" > /dev/null 2>&1
    print_ok "Wi-Fi IPv6 已禁用"
    FIX_ACTIONS+=("IPv6 修复")
}

flush_dns_cache() {
    print_info "清空 DNS 缓存..."
    dscacheutil -flushcache 2>/dev/null
    killall -HUP mDNSResponder 2>/dev/null
    print_ok "DNS 缓存已清空"
    FIX_ACTIONS+=("DNS 缓存清空")
}

run_fix_all() {
    print_header "一键修复"
    
    FIX_ACTIONS=()
    
    # 根据检测到的问题执行修复
    local need_dns_fix=false
    local need_proxy_fix=false
    local need_ipv6_fix=false
    
    for issue in "${ISSUES[@]}"; do
        local desc=$(echo "$issue" | cut -d'|' -f2)
        if echo "$desc" | grep -qi "DNS"; then need_dns_fix=true; fi
        if echo "$desc" | grep -qi "代理残留"; then need_proxy_fix=true; fi
        if echo "$desc" | grep -qi "IPv6"; then need_ipv6_fix=true; fi
    done
    
    [ "$need_dns_fix" = true ] && fix_dns
    [ "$need_proxy_fix" = true ] && fix_proxy
    [ "$need_ipv6_fix" = true ] && fix_ipv6
    
    # 始终清空 DNS 缓存
    flush_dns_cache
    
    echo ""
    if [ ${#FIX_ACTIONS[@]} -gt 0 ]; then
        print_ok "修复完成，执行了以下操作: ${FIX_ACTIONS[*]}"
    else
        print_info "无需修复，网络状态正常"
    fi
}

# ==================== 自检模块 ====================

run_self_check() {
    print_header "修复后自检"
    
    # 重新检测关键项
    echo -e "${BOLD}  [关键指标复检]${NC}"
    
    # DNS 复检
    local dns_after
    dns_after=$(networksetup -getdnsservers "$NETWORK_SERVICE" 2>/dev/null | grep -v 'There aren' | tr '\n' ' ')
    print_info "当前 DNS: $dns_after"
    
    # 代理复检
    local http_after
    http_after=$(scutil --proxy 2>/dev/null | grep 'HTTPEnable' | awk '{print $3}')
    if [ "$http_after" = "1" ]; then
        print_warn "系统代理仍开启"
    else
        print_ok "系统代理已关闭"
    fi
    
    # IPv6 复检
    local ipv6_after
    ipv6_after=$(networksetup -getinfo "$NETWORK_SERVICE" 2>/dev/null | grep -i 'IPv6' | awk -F': ' '{print $2}')
    if [ "$ipv6_after" = "Off" ] || [ -z "$ipv6_after" ]; then
        print_ok "IPv6 已禁用"
    else
        print_warn "IPv6 仍启用 ($ipv6_after)"
    fi
    
    echo ""
    echo -e "${BOLD}  [连通性复检]${NC}"
    test_url "https://www.baidu.com" "百度"
    test_url "https://www.google.com" "Google"
    
    if [ "${STATUS_vpn}" = "ok" ]; then
        test_ping "10.0.0.66" "公司 DNS"
    fi
}

# ==================== 完整检测流程 ====================

run_full_check() {
    ISSUES=()
    
    check_wifi
    check_vpn
    check_clashx
    check_dns
    check_ipv6
    check_proxy
    check_exit_ip
    run_reachability_tests
    diagnose_issues
}

# ==================== 状态摘要 ====================

show_status_summary() {
    echo ""
    echo -e "${CYAN}${BOLD}╔══════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}${BOLD}║          网络状态快速摘要                  ║${NC}"
    echo -e "${CYAN}${BOLD}╚══════════════════════════════════════════╝${NC}"
    echo ""
    
    # 快速检测
    local wifi_ok vpn_ok clashx_ok dns_ok
    pgrep -f -i UniVPN > /dev/null 2>&1 && ifconfig utun4 2>/dev/null | grep -q 'inet ' && vpn_ok=true || vpn_ok=false
    pgrep -f -i clash > /dev/null 2>&1 && clashx_ok=true || clashx_ok=false
    ifconfig en0 2>/dev/null | grep -q 'inet ' && wifi_ok=true || wifi_ok=false
    
    local dns_servers
    dns_servers=$(networksetup -getdnsservers Wi-Fi 2>/dev/null | grep -v 'There aren' | tr '\n' ' ')
    
    echo -e "  Wi-Fi:    $([ "$wifi_ok" = true ] && echo -e "${GREEN}✅ 已连接${NC}" || echo -e "${RED}❌ 未连接${NC}")"
    echo -e "  公司VPN:  $([ "$vpn_ok" = true ] && echo -e "${GREEN}✅ 已连接${NC}" || echo -e "${BLUE}⚪ 未连接${NC}")"
    echo -e "  ClashX:   $([ "$clashx_ok" = true ] && echo -e "${GREEN}✅ 运行中${NC}" || echo -e "${BLUE}⚪ 未运行${NC}")"
    echo -e "  DNS:      $dns_servers"
    echo ""
    
    # 快速连通性
    echo -e "  百度:     $(curl -s --max-time 5 --noproxy '*' -o /dev/null -w '%{http_code}' https://www.baidu.com 2>/dev/null)"
    echo -e "  Google:   $(curl -s --max-time 5 --noproxy '*' -o /dev/null -w '%{http_code}' https://www.google.com 2>/dev/null)"
    echo ""
}

# ==================== 主菜单 ====================

show_menu() {
    echo ""
    echo -e "${CYAN}${BOLD}╔══════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}${BOLD}║        网络诊断与修复工具 v1.0            ║${NC}"
    echo -e "${CYAN}${BOLD}╚══════════════════════════════════════════╝${NC}"
    echo ""
    echo "  1. 🔍 完整检测（状态 + 可达性 + 诊断）"
    echo "  2. ⚡ 快速状态摘要"
    echo "  3. 🔧 一键修复所有问题"
    echo "  4. 🧪 仅可达性测试"
    echo "  5. 📋 查看日志"
    echo "  0. 🚪 退出"
    echo ""
    read -p "  请选择操作 [0-5]: " choice
    
    case $choice in
        1)
            log "用户选择: 完整检测"
            run_full_check
            ;;
        2)
            log "用户选择: 快速状态摘要"
            show_status_summary
            ;;
        3)
            log "用户选择: 一键修复"
            run_full_check
            echo ""
            read -p "  是否执行一键修复？[y/N]: " confirm
            if [ "$confirm" = "y" ] || [ "$confirm" = "Y" ]; then
                run_fix_all
                echo ""
                read -p "  修复完成，是否执行自检？[Y/n]: " self_check
                if [ "$self_check" != "n" ] && [ "$self_check" != "N" ]; then
                    run_self_check
                fi
            else
                print_info "已取消修复"
            fi
            ;;
        4)
            log "用户选择: 仅可达性测试"
            run_reachability_tests
            ;;
        5)
            log "用户选择: 查看日志"
            if [ -f "$LOG_FILE" ]; then
                tail -30 "$LOG_FILE"
            else
                print_info "暂无日志"
            fi
            ;;
        0)
            echo ""
            print_info "再见！"
            exit 0
            ;;
        *)
            print_err "无效选择，请重新输入"
            ;;
    esac
    
    echo ""
    read -p "  按回车键返回主菜单..." _
    show_menu
}

# ==================== 主函数 ====================

main() {
    log "===== 网络诊断工具启动 ====="
    
    case "${1:-}" in
        --quick|-q)
            show_status_summary
            ;;
        --fix|-f)
            run_full_check
            run_fix_all
            run_self_check
            ;;
        --status|-s)
            show_status_summary
            ;;
        --help|-h)
            echo "网络诊断与修复工具 v1.0"
            echo ""
            echo "用法: $0 [选项]"
            echo ""
            echo "选项:"
            echo "  (无参数)    交互菜单模式"
            echo "  --quick, -q  快速状态摘要"
            echo "  --fix, -f    一键检测+修复+自检"
            echo "  --status, -s 同 --quick"
            echo "  --help, -h   显示帮助"
            ;;
        *)
            show_menu
            ;;
    esac
}

main "$@"
