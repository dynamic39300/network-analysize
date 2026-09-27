"""Windows read-only diagnostics behind the shared Agent engine interface."""
import copy
from datetime import datetime
import threading
from urllib.parse import urlsplit

from ..checks.reachability import ReachabilityCheck
from ..commands import run_command
from ..profiles import SelectedTargets
from .windows import WindowsObserver, system_directory


def manual_proxy_endpoints(value):
    if not value:
        return {}
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError('Invalid proxy configuration')
    entries = value.split(';')
    endpoints = {}
    for item in entries:
        if '=' in item:
            scheme, endpoint = item.split('=', 1)
            schemes = [scheme.strip().lower()]
        else:
            schemes, endpoint = ['http', 'https'], item
        endpoint = endpoint.strip()
        parsed = urlsplit('http://' + endpoint)
        if (any(s not in ('http', 'https') for s in schemes) or not parsed.hostname or not parsed.port
                or parsed.username is not None or parsed.password is not None or parsed.path
                or parsed.query or parsed.fragment or any(c.isspace() for c in endpoint)):
            raise ValueError('Proxy format requires a supported explicit HTTP endpoint')
        for scheme in schemes:
            if scheme in endpoints:
                raise ValueError('Ambiguous proxy endpoints')
            endpoints[scheme] = {'host': parsed.hostname, 'port': parsed.port, 'state': 'not_probed', 'local': False}
    return endpoints


def environment_status(environment):
    data = environment.to_dict()
    status = {'environment_platform': 'windows', 'vpn': 'unknown', 'vpn_path': 'unknown',
              'vpn_evidence': {'owner_confirmed': False}, 'system_proxy': 'unknown',
              'proxy_details': {}, 'proxy_pac': False, 'dns': 'observed', 'connection': 'unknown'}
    issues = []
    errors = copy.deepcopy(data['errors'])
    if not {'interfaces', 'addresses'} & set(errors):
        active = [interface for interface in data['interfaces'] if interface['state'] == 'Up'
                  and interface['type'] != 'Loopback' and any(a['state'] == 'Preferred' for a in interface['addresses'])]
        status['connection'] = 'ok' if active else 'off'
        if not active:
            issues.append(('high', 'network_disconnected', '未发现具有可用地址的活动网络接口'))
    if data['scope']['kind'] != 'interactive_user':
        errors['user_scope'] = '当前是服务或后台会话，未将该会话当作交互用户的访问路径'
    proxy = next((p for p in data['proxies'] if p['scope'] == 'process_user_wininet'), {})
    if proxy.get('state') == 'observed':
        try:
            status['proxy_details'] = manual_proxy_endpoints(proxy.get('proxy', ''))
            status['proxy_pac'] = bool(proxy.get('pac_url') or proxy.get('auto_detect'))
            status['proxy_constraints'] = {'exceptions': bool(proxy.get('bypass')), 'scoped': False}
            status['system_proxy'] = 'observed'
        except ValueError:
            errors['proxy_configuration'] = '当前用户代理格式尚未支持，未猜测其他访问路径'
    else:
        errors['proxy_configuration'] = '当前用户代理配置未确认'
    status['proxy'] = ('unknown' if status['system_proxy'] == 'unknown' else
                       'on' if status['proxy_details'] or status['proxy_pac'] else 'off')
    return status, issues, errors


class WindowsDetectionEngine:
    def __init__(self, config, runner=run_command, observer=None, directory=None):
        self.config, self.runner = config, runner
        self.observer = observer or WindowsObserver(runner, directory=directory)
        self.directory = directory
        self.lock = threading.RLock()
        self.checking = False
        self._snapshot = {'status': {}, 'issues': [], 'check_errors': {}, 'last_check': None}

    def snapshot(self):
        return copy.deepcopy(self._snapshot)

    def run_all(self, progress=None, budget=None, target_ids=None):
        def notify(phase, check):
            if progress:
                try:
                    progress({'phase': phase, 'check': check, 'snapshot': self.snapshot()})
                except Exception:
                    pass
        with self.lock:
            self.checking = True
            result = {'status': {}, 'issues': [], 'check_errors': {}, 'last_check': None,
                      'health_profile': self.config.get('health_profile.binding')}
            if target_ids is not None:
                result['target_selection'] = list(target_ids)
            try:
                notify('running', 'environment')
                environment = self.observer.observe(budget)
                result['environment'] = environment.to_dict()
                status, issues, errors = environment_status(environment)
                result.update(status=status, issues=issues, check_errors=errors)
                if getattr(self.config, 'load_error', None):
                    errors['config'] = '检测配置读取失败'
                if (self.config.get('dns.enforce_company_dns_on_vpn', False)
                        or self.config.get('dns.public_dns', [])
                        or self.config.get('ipv6.should_be', 'observe') != 'observe'):
                    errors['policy_validation'] = 'Windows 尚未验证所配置的 DNS/IPv6 策略，网页可达不代表策略已满足'
                if 'user_scope' not in errors and self.config.get('reachability.enabled', True):
                    notify('running', 'reachability')
                    runner = self.runner if budget is None else lambda argv, timeout=10: budget.run(self.runner, argv, timeout)
                    config = SelectedTargets(self.config, target_ids) if target_ids is not None else self.config
                    check = ReachabilityCheck(config, runner, str((self.directory or system_directory()) / 'curl.exe'),
                        'NUL', resolve_vpn=lambda *_: (None, 'Windows VPN 目标的接口归属和实际路由尚未验证'))
                    try:
                        issues.extend(check.check(status))
                    except Exception:
                        status['reachability'] = 'unknown'
                        errors['reachability'] = '目标探测未完成或达到预算，未将缺失结果作为正常'
                else:
                    status['reachability'] = 'unknown'
            except Exception:
                result['check_errors']['environment'] = 'Windows 环境采集未完成；请检查平台能力或会话范围'
                result['status']['connection'] = 'unknown'
            finally:
                for key, error in result['check_errors'].items():
                    result['issues'].append(('medium', 'check_failed_' + key, error))
                result['last_check'] = datetime.now()
                self._snapshot = result
                self.checking = False
                notify('checked', 'reachability')
            return self.snapshot()

    def get_overall_status(self):
        snapshot = self.snapshot()
        if not snapshot['last_check'] or snapshot['check_errors']:
            return 'unknown'
        return 'error' if any(i[0] == 'high' for i in snapshot['issues']) else 'warning' if snapshot['issues'] else 'ok'

    def get_status_summary(self):
        snapshot = self.snapshot()
        return ['Windows 网络只读检测', f"{len(snapshot.get('environment', {}).get('interfaces', []))} 个接口",
                f"{len(snapshot['issues'])} 条需要确认的结果"]


class WindowsRepairCapabilities:
    """Declare the current executable surface explicitly, without a macOS fallback."""
    def repair_options(self):
        return {}

    def describe_fixes(self, issues=None):
        return ['没有可安全自动修复的问题']

    def plan_actions(self, issues=None):
        return []

    def fix_all(self, *args, **kwargs):
        raise PermissionError('Windows 写入和系统授权模块尚未实现')

    def inspect_recovery(self, receipt, snapshot):
        return {'outcome': 'unknown', 'message': 'Windows 恢复执行尚未接入；未重放修改命令'}
