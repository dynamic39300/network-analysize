#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Relay 检测引擎与修复引擎
- DetectionEngine: 调度所有检测插件，收集状态和问题
- FixEngine: 根据问题类型匹配修复规则，执行修复并验证
"""

import subprocess
import time
from datetime import datetime
from .checks import create_instances


def run_cmd(cmd, timeout=30):
    """执行 shell 命令"""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()
    except Exception:
        return ""


class DetectionEngine:
    """检测引擎：调度所有启用的检测插件"""
    
    def __init__(self, config=None):
        self.config = config
        self.checks = []
        self.status = {}
        self.issues = []
        self.last_check = None
        self.checking = False
        self._load_checks()
    
    # 检测执行顺序（有依赖关系的排在前面）
    CHECK_ORDER = ['wifi', 'vpn', 'proxy', 'dns', 'ipv6', 'system_proxy', 'reachability']
    
    def _load_checks(self):
        """加载所有启用的检测插件，按依赖顺序排序"""
        all_checks = create_instances(self.config)
        # 按 CHECK_ORDER 排序，未在列表中的排到最后
        order_map = {name: i for i, name in enumerate(self.CHECK_ORDER)}
        self.checks = sorted(all_checks, key=lambda c: order_map.get(c.name, 999))
    
    def reload_checks(self):
        """重新加载插件（配置变更后调用）"""
        self.checks = create_instances(self.config)
    
    def run_all(self):
        """执行所有检测"""
        if self.checking:
            return
        self.checking = True
        try:
            self.issues = []
            self.status = {}
            
            # 按插件顺序执行（DNS 依赖 VPN 结果，所以 VPN 在前）
            for check in self.checks:
                try:
                    issues = check.check(self.status)
                    if issues:
                        self.issues.extend(issues)
                except Exception as e:
                    print(f"插件 {check.name} 检测异常: {e}")
            
            self.last_check = datetime.now()
        finally:
            self.checking = False
    
    def get_status_summary(self):
        """获取所有插件的状态摘要行"""
        lines = []
        for check in self.checks:
            try:
                plugin_lines = check.get_status_lines(self.status)
                if plugin_lines:
                    lines.extend(plugin_lines)
            except Exception:
                pass
        
        # 问题统计
        if self.issues:
            high_count = len([i for i in self.issues if i[0] == 'high'])
            medium_count = len([i for i in self.issues if i[0] == 'medium'])
            lines.append("")
            lines.append(f"⚠️  发现 {high_count} 个严重问题，{medium_count} 个警告")
        
        # 最后检测时间
        if self.last_check:
            lines.append("")
            lines.append(f"最后检测: {self.last_check.strftime('%H:%M:%S')}")
        
        return lines
    
    def get_overall_status(self):
        """获取整体状态：ok / warning / error"""
        if not self.issues:
            return 'ok'
        if any(i[0] == 'high' for i in self.issues):
            return 'error'
        return 'warning'
    
    def get_detailed_report(self):
        """获取详细报告文本"""
        lines = []
        lines.append("=" * 40)
        lines.append(f"Relay 网络诊断报告")
        lines.append(f"检测时间: {self.last_check.strftime('%Y-%m-%d %H:%M:%S') if self.last_check else '未知'}")
        lines.append("=" * 40)
        lines.append("")
        
        # 各插件状态
        for check in self.checks:
            lines.append(f"【{check.display_name}】")
            for key, value in self.status.items():
                if key.startswith(check.name) or key in ('wifi', 'vpn', 'proxy_app', 'proxy', 'dns', 'ipv6', 'dns_servers', 'wifi_ip', 'wifi_network', 'vpn_ip', 'vpn_client', 'proxy_client'):
                    lines.append(f"  {key}: {value}")
            lines.append("")
        
        # 问题列表
        if self.issues:
            lines.append("-" * 40)
            lines.append("发现的问题:")
            for i, (severity, issue_type, desc) in enumerate(self.issues, 1):
                icon = '🔴' if severity == 'high' else '🟡' if severity == 'medium' else '⚪'
                lines.append(f"  {i}. {icon} [{issue_type}] {desc}")
        else:
            lines.append("✅ 所有检测项正常")
        
        return "\n".join(lines)


class FixEngine:
    """修复引擎：根据 issue_type 匹配修复规则并执行"""
    
    def __init__(self, config=None, detection_engine=None):
        self.config = config
        self.detection_engine = detection_engine
    
    def _template_vars(self):
        """获取修复命令模板变量"""
        if self.config and hasattr(self.config, 'template_vars'):
            return self.config.template_vars()
        return {
            'service': 'Wi-Fi',
            'interface': 'en0',
            'company_dns': '10.0.0.66 10.0.0.68',
            'public_dns': '223.5.5.5 114.114.114.114',
        }
    
    def fix_all(self, issues=None):
        """
        修复所有问题。
        
        Args:
            issues: 问题列表，为 None 时从 detection_engine 获取
            
        Returns:
            list of str: 修复操作描述列表
        """
        if issues is None and self.detection_engine:
            issues = self.detection_engine.issues
        
        if not issues:
            return ["没有需要修复的问题"]
        
        fixed = []
        fix_rules = {}
        if self.config:
            fix_rules = self.config.get('fix_rules', {})
        
        template_vars = self._template_vars()
        
        for issue in issues:
            if len(issue) < 3:
                continue
            severity, issue_type, description = issue
            
            rule = fix_rules.get(issue_type)
            if not rule:
                continue
            
            cmd_template = rule.get('command', '')
            if not cmd_template:
                continue
            
            try:
                cmd = cmd_template.format(**template_vars)
            except KeyError as e:
                print(f"修复命令模板变量缺失: {e}")
                continue
            
            run_cmd(cmd)
            fixed.append(rule.get('description', issue_type))
        
        # 始终清空 DNS 缓存
        run_cmd("dscacheutil -flushcache")
        run_cmd("killall -HUP mDNSResponder")
        fixed.append("DNS 缓存已清空")
        
        # 修复后重新检测验证
        if self.detection_engine:
            time.sleep(1)
            self.detection_engine.run_all()
            remaining = len(self.detection_engine.issues)
            if remaining == 0:
                fixed.append("✅ 修复后所有问题已解决")
            else:
                fixed.append(f"⚠️ 修复后仍有 {remaining} 个问题")
        
        return fixed
