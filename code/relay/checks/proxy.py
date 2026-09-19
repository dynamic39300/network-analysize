#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""代理工具检测插件（支持 ClashX/Surge/V2Ray 等多种代理客户端）"""

import subprocess
from .base import BaseCheck, register


def run_cmd(cmd, timeout=10):
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()
    except Exception:
        return ""


@register
class ProxyCheck(BaseCheck):
    name = "proxy"
    display_name = "代理工具"
    description = "检测代理客户端运行状态和端口监听"
    
    def check(self, status):
        issues = []
        known_clients = self.cfg('known_clients', [])
        
        proxy_running = False
        proxy_client_name = None
        port_listening = False
        
        for client in known_clients:
            process = client.get('process', '')
            port = client.get('port', 0)
            if not process:
                continue
            running = run_cmd(f"pgrep -f -i '{process}' > /dev/null 2>&1 && echo 'yes' || echo 'no'")
            if running == 'yes':
                proxy_running = True
                proxy_client_name = client.get('name', process)
                if port:
                    listen = run_cmd(f"lsof -nP -iTCP:{port} -sTCP:LISTEN 2>/dev/null | head -1")
                    if listen:
                        port_listening = True
                break
        
        if proxy_running and port_listening:
            status['proxy_app'] = 'ok'
            status['proxy_client'] = proxy_client_name
        elif proxy_running:
            status['proxy_app'] = 'warning'
            status['proxy_client'] = proxy_client_name
            issues.append(('medium', 'proxy_port_not_listening', f'{proxy_client_name} 运行但代理端口未监听'))
        else:
            status['proxy_app'] = 'off'
            status['proxy_client'] = None
        
        return issues
    
    def get_status_lines(self, status):
        proxy_client = status.get('proxy_client', '')
        if status.get('proxy_app') == 'ok':
            return [f"🟢 代理: {proxy_client or '运行中'}"]
        elif status.get('proxy_app') == 'warning':
            return [f"🟡 代理: 端口异常"]
        elif status.get('proxy_app') == 'off':
            return [f"⚪ 代理: 未运行"]
        return []
