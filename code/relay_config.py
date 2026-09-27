"""Local configuration. Fresh installs observe; company policy is explicit."""
import copy
import json
import os
from pathlib import Path
import re
import sys

from relay.commands import checked, run_command
from relay.network import ip_addresses, process_names, tunnel_interfaces


DEFAULT_CONFIG = {
    'schema_version': 2,
    'general': {'check_interval': 30, 'log_file': '~/Library/Logs/Relay.log', 'auto_fix': False, 'preset': 'observe'},
    'guard': {'enabled': False},
    'wifi': {'enabled': True, 'interface': 'auto', 'service_name': 'auto'},
    'vpn': {
        'enabled': True, 'mode': 'auto', 'route_anchors': [], 'company_dns': [],
        'known_clients': [
            {'name': 'UniVPN', 'process': 'UniVPN', 'interface': 'auto'},
            {'name': 'Cisco AnyConnect', 'process': 'vpnagentd', 'interface': 'auto'},
            {'name': 'WireGuard', 'process': 'wireguard-go', 'interface': 'auto'},
            {'name': 'OpenVPN', 'process': 'openvpn', 'interface': 'auto'},
            {'name': 'Tunnelblick', 'process': 'Tunnelblick', 'interface': 'auto'},
            {'name': 'Viscosity', 'process': 'Viscosity', 'interface': 'auto'},
        ],
    },
    'proxy': {
        'enabled': True, 'mode': 'auto',
        'known_clients': [
            {'name': 'ClashX', 'process': 'clash', 'port': 7890},
            {'name': 'Clash Verge', 'process': 'clash-verge', 'port': 7897},
            {'name': 'Surge', 'process': 'Surge', 'port': 6152},
            {'name': 'V2Ray', 'process': 'v2ray', 'port': 1080},
            {'name': 'Shadowsocks', 'process': 'ss-local', 'port': 1080},
        ],
    },
    'dns': {'enabled': True, 'public_dns': [], 'enforce_company_dns_on_vpn': False},
    'ipv6': {'enabled': True, 'should_be': 'observe'},
    'reachability': {
        'enabled': True,
        'targets': [
            {'name': '百度', 'url': 'https://www.baidu.com', 'timeout': 5},
            {'name': 'Google', 'url': 'https://www.google.com', 'timeout': 8},
            {'name': 'OpenAI', 'url': 'https://api.openai.com/v1/models', 'timeout': 8},
        ],
    },
    # Legacy command strings in existing files are retained as data but ignored.
    'fix_rules': {key: {'enabled': True} for key in (
        'dns_mixed_on_vpn', 'dns_company_leftover', 'dns_no_company_on_vpn', 'proxy_leftover', 'ipv6_enabled')},
}

PRESETS = {
    'observe': {'general': {'preset': 'observe'}, 'vpn': {'enabled': True}, 'proxy': {'enabled': True},
                'dns': {'enforce_company_dns_on_vpn': False}, 'ipv6': {'enabled': True, 'should_be': 'observe'}},
    'company': {'general': {'preset': 'company'}, 'vpn': {'enabled': True}, 'proxy': {'enabled': True},
                'dns': {'enforce_company_dns_on_vpn': True}},
    'personal_proxy': {'general': {'preset': 'personal_proxy'}, 'vpn': {'enabled': True}, 'proxy': {'enabled': True},
                       'dns': {'enforce_company_dns_on_vpn': False}, 'ipv6': {'enabled': True, 'should_be': 'observe'}},
    'minimal': {'general': {'preset': 'minimal'}, 'vpn': {'enabled': True}, 'proxy': {'enabled': False},
                'dns': {'enabled': True, 'enforce_company_dns_on_vpn': False},
                'ipv6': {'enabled': False}, 'reachability': {'enabled': True}},
}


class ConfigManager:
    def __init__(self, config_path=None, runner=None):
        self.config_path = str(config_path or Path.home() / '.config/relay/config.json')
        self.runner = runner or run_command
        self.config = {}
        self._detected = {}
        self.load_error = None

    def load(self):
        self.load_error = None
        path = Path(self.config_path)
        if path.exists():
            try:
                self.config = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(self.config, dict):
                    raise ValueError('配置根节点必须为对象')
                self._merge_defaults()
                self._validate()
                return True
            except (ValueError, TypeError, OSError) as exc:
                self.load_error = f'配置读取失败：{exc}'
                self.config = copy.deepcopy(DEFAULT_CONFIG)
                # Preserve the user's file for recovery; never overwrite it.
                return False
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.auto_detect()
        self.save()
        return False

    def _validate(self):
        for section in ('general', 'guard', 'wifi', 'vpn', 'proxy', 'dns', 'ipv6', 'reachability', 'fix_rules'):
            if not isinstance(self.config.get(section), dict):
                raise ValueError(f'{section} 必须为对象')
        if not isinstance(self.get('guard.enabled'), bool):
            raise ValueError('guard.enabled 必须为布尔值')
        interval = self.get('general.check_interval')
        if not isinstance(interval, int) or isinstance(interval, bool) or not 5 <= interval <= 3600:
            raise ValueError('检测间隔必须在 5 至 3600 秒之间')
        for key in ('vpn.company_dns', 'vpn.route_anchors', 'dns.public_dns'):
            ip_addresses(self.get(key, []))
        for key in ('vpn.known_clients', 'proxy.known_clients', 'reachability.targets'):
            values = self.get(key, [])
            if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
                raise ValueError(f'{key} 必须为对象列表')

    def save(self):
        self._validate()
        path = Path(self.config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as handle:
            json.dump(self.config, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        self.load_error = None

    def get(self, key_path, default=None):
        value = self.config
        for key in key_path.split('.'):
            if not isinstance(value, dict) or key not in value:
                return default
            value = value[key]
        return value

    def set(self, key_path, value):
        parts = key_path.split('.')
        node = self.config
        for key in parts[:-1]:
            node = node.setdefault(key, {})
        node[parts[-1]] = value

    def apply_preset(self, preset_name):
        if preset_name not in PRESETS:
            return False
        for section, values in PRESETS[preset_name].items():
            self.config.setdefault(section, {}).update(values)
        # A company preset without company addresses is still observational.
        if preset_name == 'company' and not self.get('vpn.company_dns', []):
            self.set('dns.enforce_company_dns_on_vpn', False)
        return True

    def auto_detect(self):
        if sys.platform == 'win32':
            self._detected = {'platform': 'windows', 'preset': self.get('general.preset', 'observe'),
                              'message': 'Windows environment is collected by the scoped observer; policy is unchanged.'}
            return copy.deepcopy(self._detected)
        detected = {}
        for name, getter in (('wifi', self._detect_wifi), ('vpn', self._detect_vpn), ('proxy', self._detect_proxy)):
            try:
                detected[name] = getter()
            except Exception as exc:
                detected[name] = {'detected': False, 'clients': [], 'error': str(exc)}
        wifi = detected['wifi']
        if wifi.get('interface'):
            self.set('wifi.interface', wifi['interface'])
        if wifi.get('service_name'):
            self.set('wifi.service_name', wifi['service_name'])
        preset = self._select_preset(detected['vpn'], detected['proxy'])
        detected['preset'] = preset
        # Keep explicit existing company DNS/IPv6 choices on re-detection.
        if preset != 'company':
            self.apply_preset(preset)
        self._detected = detected
        return detected

    def _detect_wifi(self):
        raw = checked(self.runner, ['/usr/sbin/networksetup', '-listallhardwareports'])
        match = re.search(r'Hardware Port: (?:Wi-Fi|AirPort)\nDevice: (\S+)', raw)
        result = {'interface': match[1] if match else None, 'service_name': None}
        order = checked(self.runner, ['/usr/sbin/networksetup', '-listnetworkserviceorder'])
        service = None
        for line in order.splitlines():
            found = re.match(r'\(\d+\) (.+)', line)
            if found:
                service = found[1]
            elif result['interface'] and re.search(r'Device: ' + re.escape(result['interface']) + r'\)', line):
                result['service_name'] = service
                break
        return result

    def _detect_vpn(self):
        names = process_names(checked(self.runner, ['/bin/ps', '-axo', 'pid=,comm=']))
        clients = [c.get('name') for c in self.get('vpn.known_clients', [])
                   if str(c.get('process', '')).casefold() in names]
        tunnels = tunnel_interfaces(checked(self.runner, ['/sbin/ifconfig', '-a']))
        return {'detected': bool(clients), 'clients': clients, 'interfaces': list(tunnels), 'active_interface': None}

    def _detect_proxy(self):
        names = process_names(checked(self.runner, ['/bin/ps', '-axo', 'pid=,comm=']))
        clients = [c.get('name') for c in self.get('proxy.known_clients', [])
                   if str(c.get('process', '')).casefold() in names]
        return {'detected': bool(clients), 'clients': clients, 'active_port': None}

    def _select_preset(self, vpn_info, proxy_info):
        if self.get('vpn.company_dns', []) and (self.get('general.preset') == 'company'
                                                or self.get('dns.enforce_company_dns_on_vpn', False)):
            return 'company'
        return 'personal_proxy' if proxy_info.get('detected') else 'observe'

    def template_vars(self):
        """Compatibility only: the repair engine never executes templates."""
        return {'service': self.get('wifi.service_name', 'Wi-Fi'), 'interface': self.get('wifi.interface', 'en0'),
                'company_dns': ' '.join(self.get('vpn.company_dns', [])),
                'public_dns': ' '.join(self.get('dns.public_dns', []))}

    def get_detected_info(self):
        return copy.deepcopy(self._detected)

    def _deep_copy(self, obj):
        return copy.deepcopy(obj)

    def _merge_defaults(self):
        def merge(defaults, current):
            for key, value in defaults.items():
                if key not in current:
                    current[key] = copy.deepcopy(value)
                elif isinstance(value, dict) and isinstance(current.get(key), dict):
                    merge(value, current[key])
        merge(DEFAULT_CONFIG, self.config)

    def reset_to_default(self):
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.auto_detect()
        self.save()
