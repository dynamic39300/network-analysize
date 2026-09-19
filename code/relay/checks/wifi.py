#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wi-Fi 连接状态检测插件"""

import subprocess
from .base import BaseCheck, register


def run_cmd(cmd, timeout=10):
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()
    except Exception:
        return ""


@register
class WifiCheck(BaseCheck):
    name = "wifi"
    display_name = "Wi-Fi"
    description = "检测 Wi-Fi 连接状态和 IP 地址"
    
    def check(self, status):
        issues = []
        iface = self.wifi_interface()
        
        wifi_ip = run_cmd(f"ifconfig {iface} 2>/dev/null | grep 'inet ' | awk '{{print $2}}'")
        wifi_network = run_cmd(f"networksetup -getairportnetwork {iface} 2>/dev/null | sed 's/Current Wi-Fi Network: //'")
        
        if wifi_ip:
            status['wifi'] = 'ok'
            status['wifi_ip'] = wifi_ip
            status['wifi_network'] = wifi_network if wifi_network and 'not associated' not in wifi_network else '未知'
        else:
            status['wifi'] = 'error'
            status['wifi_ip'] = ''
            status['wifi_network'] = ''
            issues.append(('high', 'wifi_disconnected', 'Wi-Fi 未连接或无 IP 地址'))
        
        return issues
    
    def get_status_lines(self, status):
        icon = '🟢' if status.get('wifi') == 'ok' else '🔴'
        wifi_name = status.get('wifi_network', '未知')
        return [f"{icon} Wi-Fi: {wifi_name}"]
