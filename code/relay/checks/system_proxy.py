#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""系统代理状态检测插件"""

import subprocess
from .base import BaseCheck, register


def run_cmd(cmd, timeout=10):
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()
    except Exception:
        return ""


@register
class SystemProxyCheck(BaseCheck):
    name = "system_proxy"
    display_name = "系统代理"
    description = "检测系统代理开关状态，检测代理残留"
    default_enabled = True
    
    def _load_enabled(self):
        # 系统代理检测始终启用，不依赖配置开关
        return True
    
    def check(self, status):
        issues = []
        
        http_enable = run_cmd("scutil --proxy 2>/dev/null | grep 'HTTPEnable' | awk '{print $3}'")
        https_enable = run_cmd("scutil --proxy 2>/dev/null | grep 'HTTPSEnable' | awk '{print $3}'")
        
        proxy_on = http_enable == '1' or https_enable == '1'
        status['proxy'] = 'on' if proxy_on else 'off'
        
        if proxy_on and status.get('proxy_app') == 'off':
            issues.append(('high', 'proxy_leftover', '代理残留：代理工具未运行但系统代理开启'))
        
        return issues
    
    def get_status_lines(self, status):
        icon = '🟢' if status.get('proxy') == 'off' else '🟡'
        proxy_status = '已关闭' if status.get('proxy') == 'off' else '已开启'
        return [f"{icon} 系统代理: {proxy_status}"]
