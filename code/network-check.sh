#!/bin/bash
# WiFi 网络环境一键检查脚本
# 用法: ./network-check.sh

echo "========================================="
echo "  WiFi 网络环境一键检查"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "========================================="

echo ""
echo "=== 1. Wi-Fi 状态 ==="
echo "当前网络: $(networksetup -getairportnetwork en0 2>/dev/null | sed 's/Current Wi-Fi Network: //')"
echo "Wi-Fi 电源: $(networksetup -getairportpower en0 2>/dev/null | awk '{print $NF}')"
WIFI_IP=$(ifconfig en0 2>/dev/null | grep 'inet ' | awk '{print $2}')
echo "Wi-Fi IP: ${WIFI_IP:-未获取}"

echo ""
echo "=== 2. DNS 配置 ==="
networksetup -getdnsservers Wi-Fi 2>/dev/null | while read line; do echo "  $line"; done

echo ""
echo "=== 3. IPv6 状态 ==="
networksetup -getinfo Wi-Fi 2>/dev/null | grep -i 'ipv6\|IPv6' | sed 's/^/  /'

echo ""
echo "=== 4. 系统代理 ==="
scutil --proxy 2>/dev/null | grep -E 'Enable|Proxy|Port' | sed 's/^/  /'

echo ""
echo "=== 5. 公司 VPN (UniVPN) ==="
if pgrep -f -i UniVPN > /dev/null 2>&1; then
    echo "  VPN: 运行中"
    VPN_IP=$(ifconfig utun4 2>/dev/null | grep 'inet ' | awk '{print $2}')
    echo "  VPN IP (utun4): ${VPN_IP:-未获取}"
else
    echo "  VPN: 未运行"
fi

echo ""
echo "=== 6. ClashX ==="
if pgrep -f -i clash > /dev/null 2>&1; then
    echo "  ClashX: 运行中"
    CLASH_PORT=$(lsof -nP -iTCP:7890 -sTCP:LISTEN 2>/dev/null | head -2 | tail -1 | awk '{print $9}')
    echo "  代理端口: ${CLASH_PORT:-未监听}"
else
    echo "  ClashX: 未运行"
fi

echo ""
echo "=== 7. 默认路由 ==="
netstat -rn -f inet 2>/dev/null | grep '^default' | sed 's/^/  /'

echo ""
echo "=== 8. 连通性测试 ==="
curl -s --max-time 8 --noproxy '*' -o /dev/null -w "  百度(直连): HTTP %{http_code}\n" https://www.baidu.com 2>&1
curl -s --max-time 8 --noproxy '*' -o /dev/null -w "  Google(直连): HTTP %{http_code}\n" https://www.google.com 2>&1
if pgrep -f -i clash > /dev/null 2>&1; then
    curl -s --max-time 8 -x http://127.0.0.1:7890 -o /dev/null -w "  Google(走ClashX): HTTP %{http_code}\n" https://www.google.com 2>&1
fi
curl -s --max-time 8 --noproxy '*' -o /dev/null -w "  公司内网(10.0.0.66): HTTP %{http_code}\n" http://10.0.0.66 2>&1

echo ""
echo "=== 9. 出口 IP ==="
EXIT_IP=$(curl -s --max-time 10 --noproxy '*' https://api.ipify.org 2>/dev/null)
echo "  出口 IP: ${EXIT_IP:-未获取}"

echo ""
echo "========================================="
echo "  检查完成"
echo "========================================="
