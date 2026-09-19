#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VPN 连接状态检测插件（支持多种 VPN 客户端）"""

import subprocess
from .base import BaseCheck, register


def run_cmd(cmd, timeout=10):
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()
    except Exception:
        return ""


@register
class VpnCheck(BaseCheck):
    name = "vpn"
    display_name = "VPN"
    description = "检测 VPN 客户端运行状态和隧道连接"
    
    def check(self, status):
        issues = []
        known_clients = self.cfg('known_clients', [])
        
        vpn_running = False
        vpn_client_name = None
        vpn_ip = None
        
        for client in known_clients:
            process = client.get('process', '')
            if not process:
                continue
            running = run_cmd(f"pgrep -f -i '{process}' > /dev/null 2>&1 && echo 'yes' || echo 'no'")
            if running == 'yes':
                vpn_running = True
                vpn_client_name = client.get('name', process)
                interface_pattern = client.get('interface', 'utun')
                if interface_pattern == 'utun' or interface_pattern.endswith('*'):
                    vpn_ip = run_cmd("ifconfig 2>/dev/null | grep -A2 'utun' | grep 'inet ' | tail -1 | awk '{print $2}'")
                else:
                    vpn_ip = run_cmd(f"ifconfig {interface_pattern} 2>/dev/null | grep 'inet ' | awk '{{print $2}}'")
                if vpn_ip:
                    break
        
        if vpn_running and vpn_ip:
            status['vpn'] = 'ok'
            status['vpn_ip'] = vpn_ip
            status['vpn_client'] = vpn_client_name
        elif vpn_running:
            status['vpn'] = 'warning'
            status['vpn_ip'] = ''
            status['vpn_client'] = vpn_client_name
            issues.append(('high', 'vpn_fake_connection', f'VPN 假连接：{vpn_client_name} 进程在运行但隧道未建立'))
        else:
            status['vpn'] = 'off'
            status['vpn_ip'] = ''
            status['vpn_client'] = None
        
        return issues
    
    def get_status_lines(self, status):
        vpn_client = status.get('vpn_client', '')
        if status.get('vpn') == 'ok':
            return [f"🟢 VPN: 已连接 ({vpn_client or '未知'})"]
        elif status.get('vpn') == 'warning':
            return [f"🟡 VPN: 假连接"]
        elif status.get('vpn') == 'off':
            return [f"⚪ VPN: 未连接"]
        return []
