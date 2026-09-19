#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DNS 配置检测插件"""

import subprocess
from .base import BaseCheck, register


def run_cmd(cmd, timeout=10):
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()
    except Exception:
        return ""


@register
class DnsCheck(BaseCheck):
    name = "dns"
    display_name = "DNS"
    description = "检测 DNS 服务器配置，检查 VPN 联动是否正确"
    
    def check(self, status):
        issues = []
        svc = self.wifi_service()
        
        dns_servers = run_cmd(f"networksetup -getdnsservers '{svc}' 2>/dev/null | grep -v 'There aren' | tr '\\n' ' '")
        status['dns_servers'] = dns_servers
        
        company_dns = self.config.get('vpn.company_dns', ['10.0.0.66', '10.0.0.68']) if self.config else ['10.0.0.66', '10.0.0.68']
        public_dns = self.cfg('public_dns', ['223.5.5.5', '114.114.114.114'])
        enforce = self.cfg('enforce_company_dns_on_vpn', True)
        
        has_company_dns = any(d in dns_servers for d in company_dns)
        has_public_dns = any(d in dns_servers for d in public_dns)
        
        if enforce and status.get('vpn') == 'ok':
            if has_public_dns:
                status['dns'] = 'warning'
                issues.append(('medium', 'dns_mixed_on_vpn', 'DNS 混合配置：VPN 连接时混入公共 DNS，导致外网不稳定'))
            elif has_company_dns:
                status['dns'] = 'ok'
            else:
                status['dns'] = 'error'
                issues.append(('high', 'dns_no_company_on_vpn', 'VPN 连接但 DNS 未切换到公司 DNS'))
        elif enforce and status.get('vpn') in ('off', 'warning'):
            if has_company_dns and not has_public_dns:
                status['dns'] = 'warning'
                issues.append(('medium', 'dns_company_leftover', 'VPN 断开但 DNS 残留公司 DNS'))
            else:
                status['dns'] = 'ok'
        else:
            if not dns_servers.strip():
                status['dns'] = 'error'
                issues.append(('high', 'dns_empty', 'DNS 服务器为空'))
            else:
                status['dns'] = 'ok'
        
        return issues
    
    def get_status_lines(self, status):
        icon = '🟢' if status.get('dns') == 'ok' else '🟡'
        dns_servers = status.get('dns_servers', '')[:30]
        return [f"{icon} DNS: {dns_servers}..."]
