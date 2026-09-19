#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Relay 配置管理模块
负责配置文件读写、环境自动探测、预设配置管理
"""

import json
import os
import subprocess
from pathlib import Path


# ==================== 默认配置 ====================

DEFAULT_CONFIG = {
    "general": {
        "check_interval": 30,
        "log_file": "~/Library/Logs/Relay.log",
        "auto_fix": False,
        "preset": "auto"
    },
    "wifi": {
        "enabled": True,
        "interface": "auto",
        "service_name": "auto"
    },
    "vpn": {
        "enabled": True,
        "mode": "auto",
        "known_clients": [
            {"name": "UniVPN", "process": "UniVPN", "interface": "utun4"},
            {"name": "Cisco AnyConnect", "process": "vpnagentd", "interface": "utun0"},
            {"name": "WireGuard", "process": "wireguard-go", "interface": "utun"},
            {"name": "OpenVPN", "process": "openvpn", "interface": "utun"},
            {"name": "Tunnelblick", "process": "Tunnelblick", "interface": "utun"},
            {"name": "Viscosity", "process": "Viscosity", "interface": "utun"}
        ],
        "company_dns": ["10.0.0.66", "10.0.0.68"]
    },
    "proxy": {
        "enabled": True,
        "mode": "auto",
        "known_clients": [
            {"name": "ClashX", "process": "clash", "port": 7890},
            {"name": "Clash Verge", "process": "clash-verge", "port": 7897},
            {"name": "Surge", "process": "Surge", "port": 6152},
            {"name": "V2Ray", "process": "v2ray", "port": 1080},
            {"name": "Shadowsocks", "process": "ss-local", "port": 1080},
            {"name": "Quantumult", "process": "Quantumult", "port": 6152}
        ]
    },
    "dns": {
        "enabled": True,
        "public_dns": ["223.5.5.5", "114.114.114.114"],
        "enforce_company_dns_on_vpn": True
    },
    "ipv6": {
        "enabled": True,
        "should_be": "off"
    },
    "reachability": {
        "enabled": True,
        "targets": [
            {"name": "百度", "url": "https://www.baidu.com", "timeout": 5},
            {"name": "Google", "url": "https://www.google.com", "timeout": 8},
            {"name": "OpenAI", "url": "https://api.openai.com/v1/models", "timeout": 8}
        ]
    },
    "fix_rules": {
        "dns_mixed_on_vpn": {
            "command": "networksetup -setdnsservers {service} {company_dns}",
            "description": "切换为公司 DNS",
            "requires_admin": False
        },
        "dns_company_leftover": {
            "command": "networksetup -setdnsservers {service} {public_dns}",
            "description": "恢复公共 DNS",
            "requires_admin": False
        },
        "dns_no_company_on_vpn": {
            "command": "networksetup -setdnsservers {service} {company_dns}",
            "description": "切换为公司 DNS",
            "requires_admin": False
        },
        "proxy_leftover": {
            "command": "networksetup -setwebproxystate {service} off && networksetup -setsecurewebproxystate {service} off && networksetup -setsocksfirewallproxystate {service} off",
            "description": "关闭系统代理",
            "requires_admin": False
        },
        "ipv6_enabled": {
            "command": "networksetup -setv6off {service}",
            "description": "禁用 IPv6",
            "requires_admin": False
        }
    }
}

# 预设配置覆盖
PRESETS = {
    "company": {
        "general": {"preset": "company"},
        "vpn": {"enabled": True, "mode": "auto"},
        "proxy": {"enabled": True, "mode": "auto"},
        "dns": {"enforce_company_dns_on_vpn": True},
        "ipv6": {"enabled": True, "should_be": "off"}
    },
    "personal_proxy": {
        "general": {"preset": "personal_proxy"},
        "vpn": {"enabled": False},
        "proxy": {"enabled": True, "mode": "auto"},
        "dns": {"enforce_company_dns_on_vpn": False},
        "ipv6": {"enabled": False, "should_be": "ignore"}
    },
    "minimal": {
        "general": {"preset": "minimal"},
        "vpn": {"enabled": False},
        "proxy": {"enabled": False},
        "dns": {"enabled": True, "enforce_company_dns_on_vpn": False},
        "ipv6": {"enabled": False},
        "reachability": {"enabled": True}
    }
}


def run_cmd(cmd, timeout=10):
    """执行 shell 命令，返回输出"""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip()
    except Exception:
        return ""


class ConfigManager:
    """Relay 配置管理器"""

    def __init__(self, config_path=None):
        if config_path is None:
            config_path = os.path.expanduser("~/.config/relay/config.json")
        self.config_path = config_path
        self.config = {}
        self._detected = {}

    def load(self):
        """加载配置，不存在则用默认配置初始化"""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    self.config = json.load(f)
                # 合并默认值（新增字段补全）
                self._merge_defaults()
                return True
            except Exception as e:
                print(f"配置文件读取失败，使用默认配置: {e}")
        # 首次运行：自动探测 + 生成配置
        self.config = self._deep_copy(DEFAULT_CONFIG)
        self.auto_detect()
        self.save()
        return False

    def save(self):
        """保存配置到文件"""
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(self.config, f, indent=2, ensure_ascii=False)

    def get(self, key_path, default=None):
        """按点分路径获取配置，如 get('vpn.company_dns')"""
        keys = key_path.split('.')
        value = self.config
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        return value

    def set(self, key_path, value):
        """按点分路径设置配置"""
        keys = key_path.split('.')
        node = self.config
        for key in keys[:-1]:
            if key not in node:
                node[key] = {}
            node = node[key]
        node[keys[-1]] = value

    def apply_preset(self, preset_name):
        """应用预设配置"""
        if preset_name not in PRESETS:
            return False
        preset = PRESETS[preset_name]
        for section, values in preset.items():
            if section not in self.config:
                self.config[section] = {}
            self.config[section].update(values)
        return True

    # ==================== 环境自动探测 ====================

    def auto_detect(self):
        """自动探测运行环境，填充配置"""
        detected = {}

        # 1. 探测 Wi-Fi 接口和服务名
        wifi_info = self._detect_wifi()
        detected['wifi'] = wifi_info
        if wifi_info['interface']:
            self.set('wifi.interface', wifi_info['interface'])
        if wifi_info['service_name']:
            self.set('wifi.service_name', wifi_info['service_name'])

        # 2. 探测 VPN 客户端
        vpn_info = self._detect_vpn()
        detected['vpn'] = vpn_info
        if vpn_info['detected']:
            # 确保已知客户端列表中包含检测到的
            pass
        else:
            # 没有 VPN 客户端，自动选择 personal_proxy 或 minimal
            pass

        # 3. 探测代理工具
        proxy_info = self._detect_proxy()
        detected['proxy'] = proxy_info

        # 4. 根据探测结果自动选择预设
        preset = self._select_preset(vpn_info, proxy_info)
        detected['preset'] = preset
        self.apply_preset(preset)

        self._detected = detected
        return detected

    def _detect_wifi(self):
        """探测 Wi-Fi 接口和服务名"""
        result = {'interface': None, 'service_name': None}

        # 探测硬件接口
        hw_output = run_cmd("networksetup -listallhardwareports 2>/dev/null")
        current_device = None
        for line in hw_output.split('\n'):
            if 'Wi-Fi' in line or 'AirPort' in line:
                current_device = True
                continue
            if current_device and 'Device:' in line:
                result['interface'] = line.split('Device:')[-1].strip()
                break
            if current_device and line.strip() == '':
                break

        if not result['interface']:
            result['interface'] = 'en0'  # fallback

        # 探测服务名
        services = run_cmd("networksetup -listallnetworkservices 2>/dev/null")
        for svc in services.split('\n'):
            svc = svc.strip().lstrip('*')
            if 'Wi-Fi' in svc or 'AirPort' in svc:
                result['service_name'] = svc
                break
        if not result['service_name']:
            result['service_name'] = 'Wi-Fi'  # fallback

        return result

    def _detect_vpn(self):
        """探测已安装的 VPN 客户端"""
        result = {'detected': False, 'clients': [], 'active_interface': None}

        known = self.get('vpn.known_clients', [])
        for client in known:
            process = client.get('process', '')
            running = run_cmd(f"pgrep -f -i '{process}' > /dev/null 2>&1 && echo 'yes' || echo 'no'")
            if running == 'yes':
                result['detected'] = True
                result['clients'].append(client['name'])

        # 探测活动的 utun 接口
        utun_output = run_cmd("ifconfig 2>/dev/null | grep -o 'utun[0-9]*' | sort -u")
        if utun_output:
            interfaces = utun_output.split('\n')
            if interfaces:
                result['active_interface'] = interfaces[-1]

        return result

    def _detect_proxy(self):
        """探测已安装的代理工具"""
        result = {'detected': False, 'clients': [], 'active_port': None}

        known = self.get('proxy.known_clients', [])
        for client in known:
            process = client.get('process', '')
            running = run_cmd(f"pgrep -f -i '{process}' > /dev/null 2>&1 && echo 'yes' || echo 'no'")
            if running == 'yes':
                result['detected'] = True
                result['clients'].append(client['name'])

        # 探测常见代理端口
        ports = [7890, 7897, 6152, 1080, 8080, 1087]
        for port in ports:
            listening = run_cmd(f"lsof -nP -iTCP:{port} -sTCP:LISTEN 2>/dev/null | head -1")
            if listening:
                result['active_port'] = port
                break

        return result

    def _select_preset(self, vpn_info, proxy_info):
        """根据探测结果选择预设"""
        has_vpn = vpn_info['detected'] or self.get('vpn.enabled', False)
        has_proxy = proxy_info['detected'] or self.get('proxy.enabled', False)

        if has_vpn and has_proxy:
            return 'company'
        elif has_proxy and not has_vpn:
            return 'personal_proxy'
        else:
            return 'minimal'

    # ==================== 模板变量 ====================

    def template_vars(self):
        """获取修复命令模板变量"""
        return {
            'service': self.get('wifi.service_name', 'Wi-Fi'),
            'interface': self.get('wifi.interface', 'en0'),
            'company_dns': ' '.join(self.get('vpn.company_dns', ['10.0.0.66', '10.0.0.68'])),
            'public_dns': ' '.join(self.get('dns.public_dns', ['223.5.5.5', '114.114.114.114'])),
        }

    def get_detected_info(self):
        """获取探测结果摘要（用于 About 显示）"""
        return self._detected

    # ==================== 工具方法 ====================

    def _deep_copy(self, obj):
        """深拷贝"""
        return json.loads(json.dumps(obj))

    def _merge_defaults(self):
        """将默认配置中新增的字段合并到现有配置"""
        def merge(default, current):
            for key, value in default.items():
                if key not in current:
                    current[key] = self._deep_copy(value)
                elif isinstance(value, dict) and isinstance(current.get(key), dict):
                    merge(value, current[key])
        merge(DEFAULT_CONFIG, self.config)

    def reset_to_default(self):
        """重置为默认配置并重新探测"""
        self.config = self._deep_copy(DEFAULT_CONFIG)
        self.auto_detect()
        self.save()
