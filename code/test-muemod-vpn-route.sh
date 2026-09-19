#!/bin/bash
# 测试：让 muemod.top 的流量走公司 VPN 隧道
# 用法: ./test-muemod-vpn-route.sh

set -e

echo "=========================================="
echo "  muemod.top VPN 路由测试"
echo "=========================================="
echo ""

# 检查 VPN 是否连接
if ! ifconfig utun4 2>/dev/null | grep -q 'inet '; then
    echo "❌ 公司 VPN 未连接，请先连接 VPN 后再运行此脚本"
    exit 1
fi
echo "✅ 公司 VPN 已连接 (utun4)"
echo ""

# 获取 muemod.top 的真实 IP（用公共 DNS）
echo "--- 步骤 1: 获取 muemod.top 真实 IP ---"
MUEMOD_IPS=$(nslookup muemod.top 223.5.5.5 2>/dev/null | grep 'Address:' | grep -v '#' | awk '{print $2}')
echo "muemod.top IP 列表: $MUEMOD_IPS"
echo ""

# 显示修改前的路由
echo "--- 步骤 2: 显示当前路由（修改前）---"
for ip in $MUEMOD_IPS; do
    echo "到 $ip:"
    route -n get "$ip" 2>&1 | grep -E 'interface|gateway'
done
echo ""

# 添加路由
echo "--- 步骤 3: 添加路由，让 muemod.top 走 VPN 隧道 ---"
echo "需要 sudo 权限，请输入密码："
for ip in $MUEMOD_IPS; do
    echo "  添加路由: $ip -> utun4"
    sudo route add "$ip" -interface utun4 2>&1 || echo "  (路由可能已存在)"
done
echo ""

# 验证路由
echo "--- 步骤 4: 验证路由是否生效 ---"
for ip in $MUEMOD_IPS; do
    echo "到 $ip:"
    route -n get "$ip" 2>&1 | grep -E 'interface|gateway'
done
echo ""

# 测试 muemod.top 连通性
echo "--- 步骤 5: 测试 muemod.top 连通性 ---"
HTTP_CODE=$(curl -s --max-time 10 --noproxy '*' -o /dev/null -w "%{http_code}" https://muemod.top 2>&1 || echo "000")
TIME_TOTAL=$(curl -s --max-time 10 --noproxy '*' -o /dev/null -w "%{time_total}" https://muemod.top 2>&1 || echo "0")
echo "muemod.top: HTTP $HTTP_CODE, 耗时 ${TIME_TOTAL}s"
echo ""

# 测试通过 CC Switch 访问 Anthropic API
echo "--- 步骤 6: 测试通过 CC Switch 访问 Anthropic API ---"
CC_RESULT=$(curl -s --max-time 10 -X POST http://127.0.0.1:15721/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: test-key" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"claude-3-haiku-20240307","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}' \
  -w "\n%{http_code}|%{time_total}" 2>&1 || echo "ERROR|0")
CC_HTTP=$(echo "$CC_RESULT" | tail -1 | cut -d'|' -f1)
CC_TIME=$(echo "$CC_RESULT" | tail -1 | cut -d'|' -f2)
CC_BODY=$(echo "$CC_RESULT" | head -1)
echo "CC Switch -> Anthropic API: HTTP $CC_HTTP, 耗时 ${CC_TIME}s"
echo "响应内容: $CC_BODY"
echo ""

# 结果判断
echo "=========================================="
echo "  测试结果"
echo "=========================================="
if [ "$HTTP_CODE" != "000" ] && [ "$HTTP_CODE" != "0" ]; then
    echo "✅ 成功！muemod.top 可以通过 VPN 隧道访问了！"
    echo ""
    echo "下一步：我可以帮你写一个自动化脚本，连接 VPN 后自动添加"
    echo "muemod.top（和其他中转站）的路由，断开 VPN 后自动删除。"
else
    echo "❌ 失败！muemod.top 仍然无法通过 VPN 隧道访问。"
    echo ""
    echo "这说明公司 VPN 服务器只允许转发 10.0.0.0/8 内网地址的流量，"
    echo "不支持代理任意外网流量。"
    echo ""
    echo "正在自动删除测试添加的路由..."
    for ip in $MUEMOD_IPS; do
        sudo route delete "$ip" -interface utun4 2>&1 || true
    done
    echo "✅ 路由已清理"
    echo ""
    echo "建议方案："
    echo "  1. 用 dragtokens.com 替代 muemod.top（已验证在 VPN 下可达）"
    echo "  2. 需要用 muemod.top 时，同时开 ClashX"
fi
echo ""
