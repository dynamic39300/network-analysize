#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IPv6 状态检测插件"""

import subprocess
from .base import BaseCheck, register


def run_cmd(cmd, timeout=10):
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()
    except Exception:
        return ""


@register
class Ipv6Check(BaseCheck):
    name = "ipv6"
    display_name = "IPv6"
    description = "检测 IPv6 启用状态"
    
    def check(self, status):
        issues = []
        should_be = self.cfg('should_be', 'off')
        
        if should_be == 'ignore':
            status['ipv6'] = 'ignored'
            return issues
        
        svc = self.wifi_service()
        ipv6_status = run_cmd(f"networksetup -getinfo '{svc}' 2>/dev/null | grep -i 'IPv6' | awk -F': ' '{{print $2}}'")
        
        if should_be == 'off' and ipv6_status and ipv6_status != 'Off':
            status['ipv6'] = 'on'
            issues.append(('medium', 'ipv6_enabled', 'IPv6 已启用，可能导致网络异常'))
        elif should_be == 'on' and (not ipv6_status or ipv6_status == 'Off'):
            status['ipv6'] = 'off'
            issues.append(('low', 'ipv6_disabled', 'IPv6 已禁用'))
        else:
            status['ipv6'] = should_be
        
        return issues
    
    def get_status_lines(self, status):
        if status.get('ipv6') == 'ignored':
            return []
        return []  # IPv6 状态不在摘要中显示，仅在问题时提示
