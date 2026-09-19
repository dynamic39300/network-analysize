#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网络连通性检测插件（可配置目标列表）"""

import subprocess
from .base import BaseCheck, register


def run_cmd(cmd, timeout=15):
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()
    except Exception:
        return ""


@register
class ReachabilityCheck(BaseCheck):
    name = "reachability"
    display_name = "连通性"
    description = "检测关键网站/API 的网络连通性"
    
    def check(self, status):
        issues = []
        targets = self.cfg('targets', [
            {"name": "百度", "url": "https://www.baidu.com", "timeout": 5},
            {"name": "Google", "url": "https://www.google.com", "timeout": 8},
        ])
        
        for target in targets:
            name = target.get('name', 'unknown')
            url = target.get('url', '')
            timeout = target.get('timeout', 8)
            if not url:
                continue
            code = run_cmd(f"curl -s --max-time {timeout} --noproxy '*' -o /dev/null -w '%{{http_code}}' {url} 2>/dev/null")
            key = name.lower().replace(' ', '_')
            status[key] = 'ok' if code and code != '000' else 'error'
            if not code or code == '000':
                issues.append(('low', f'reachability_{key}', f'{name} 无法访问'))
        
        return issues
    
    def get_status_lines(self, status):
        targets = self.cfg('targets', [])
        if not targets:
            return []
        parts = []
        for t in targets[:3]:
            name = t.get('name', '')
            key = name.lower().replace(' ', '_')
            icon = '🟢' if status.get(key) == 'ok' else '🔴'
            parts.append(f"{icon}{name}")
        if parts:
            return [" ".join(parts)]
        return []
