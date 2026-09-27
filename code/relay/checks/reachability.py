"""Separate transport reachability from authentication and service failures."""
from urllib.parse import urlsplit
import ipaddress
from .base import BaseCheck, register
from ..profiles import definition_hash, scope_state, target_key
from ..target_paths import pinned_host_option, route_interface, vpn_destination


@register
class ReachabilityCheck(BaseCheck):
    name = "reachability"
    display_name = "连通性"
    description = "区分 DNS、连接、TLS、HTTP 和服务拒绝"

    def __init__(self, config=None, runner=None, curl_path='/usr/bin/curl', null_device='/dev/null', resolve_vpn=None):
        super().__init__(config, runner)
        self.curl_path, self.null_device, self.resolve_vpn = curl_path, null_device, resolve_vpn or vpn_destination

    def check(self, status):
        issues, observations = [], {}
        for target in self.cfg("targets", []):
            name, url = target.get("name", "unknown"), target.get("url", "")
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("检测目标必须是无凭据的 HTTP(S) 地址")
            timeout = max(1, min(30, int(target.get("timeout", 8))))
            key = target_key(target)
            applicable = scope_state(target, status)
            observation = {'name': name, 'definition_hash': definition_hash(target), 'applicable': applicable,
                           'requirement': target.get('requirement', 'transport'),
                           'path': 'unknown', 'path_verified': None, 'transport': 'unknown',
                           'http_status': None, 'service': 'unknown'}
            observations[key] = observation
            if applicable is not True:
                observation.update(transport='not_applicable' if applicable is False else 'unknown')
                status[key] = 'skipped' if applicable is False else 'unknown'
                if applicable is None:
                    issues.append(('low', f'reachability_{key}', f'{name} 的适用网络环境尚未确认'))
                continue
            args = [self.curl_path, "--disable", "--globoff", "-sS", "--max-time", str(timeout), "--connect-timeout", str(min(timeout, 5)),
                    "-o", self.null_device, "-w", "%{http_code}"]
            proxy = status.get("proxy_details", {}).get(parsed.scheme)
            socks = status.get("proxy_details", {}).get("socks")
            path = "direct_pac_unresolved" if status.get("proxy_pac") else "direct"
            selected = proxy or socks
            expected = target.get('expected_path', 'system')
            destination = None
            constraints = status.get('proxy_constraints', {})
            if (target.get('id') and expected == 'system'
                    and (status.get('proxy_pac') or constraints.get('scoped')
                         or selected and constraints.get('exceptions') or status.get('system_proxy') == 'unknown')):
                observation.update(limitation='系统代理或分接口规则尚未按目标验证，未代用其他路径', transport='not_tested')
                status[key] = 'unknown'
                issues.append(('low', f'reachability_{key}', f'{name} 的系统访问路径尚未验证'))
                continue
            if expected == 'system_proxy' and not selected:
                observation.update(path_verified=False, limitation='未发现目标所需的系统代理', transport='not_tested')
                status[key] = 'warning'
                issues.append(('medium', f'reachability_{key}', f'{name} 缺少预期代理，未改用直连'))
                continue
            if expected in ('direct', 'vpn'):
                selected = None
                path = 'direct'
            if expected == 'vpn':
                destination, limitation = self.resolve_vpn(target, status, self.runner)
                if destination is None:
                    observation.update(path_verified=None, limitation=limitation, transport='not_tested')
                    status[key] = 'unknown'
                    issues.append(('medium', f'reachability_{key}', f'{name}：{limitation}'))
                    continue
                args += pinned_host_option(url, destination['address'])
                args += ['--interface', 'if!' + destination['interface']]
                args[args.index('%{http_code}')] = '%{http_code} %{remote_ip}'
                path = 'vpn'
            if selected:
                scheme = "http" if proxy else "socks5h"
                host = selected["host"]
                host = f"[{host}]" if ":" in host and not host.startswith("[") else host
                args += ["--proxy", f"{scheme}://{host}:{selected['port']}", "--noproxy", ""]
                path = "system_proxy"
            else:
                args += ["--noproxy", "*"]
            result = self.runner(args + ["--", url], timeout=timeout + 2)
            observation.update(path=path, path_verified=None if path == 'direct_pac_unresolved' else True)
            if not result.ok:
                category = {6: "dns_error", 7: "connect_error", 28: "timeout", 35: "tls_error", 60: "tls_error"}.get(result.returncode, "check_failed")
                observation.update(transport=category, service="unavailable")
                status[key] = "unknown" if category == "check_failed" else "error"
                issues.append(("medium", f"reachability_{key}", f"{name} 检测异常：{category}"))
                continue
            fields = result.stdout.strip().split()
            code_text = fields[0] if fields else ''
            if destination:
                remote = fields[1] if len(fields) == 2 else ''
                try:
                    verified = bool(remote and ipaddress.ip_address(remote) == ipaddress.ip_address(destination['address'])
                                    and route_interface(remote, self.runner) == destination['interface'])
                except ValueError:
                    verified = False
                observation.update(remote_ip=remote, route_interface=destination['interface'],
                                   vpn_owner=destination['owner'], path_verified=verified)
                if not verified:
                    observation['limitation'] = '实际连接地址或 VPN 路由在探测期间发生变化'
                    issues.append(('medium', f'reachability_{key}', f'{name} 的实际 VPN 路径未通过核对'))
            if not code_text.isdigit() or not 100 <= int(code_text) <= 599:
                raise ValueError("curl 未返回有效 HTTP 状态码")
            code = int(code_text)
            observation.update(transport="ok", http_status=code)
            status[key] = "ok"
            if 300 <= code < 400:
                observation['service'] = 'redirected'
                if target.get('requirement') == 'service':
                    status[key] = 'warning'
                    issues.append(('low', f'service_{key}', f'{name} 返回重定向，目标服务尚未直接响应成功'))
            elif code == 401:
                observation["service"] = "auth_required"
                if target.get('requirement') == 'service':
                    status[key] = 'warning'
                    issues.append(('low', f'service_{key}', f'{name} 网络可达，但需要身份认证'))
            elif code == 403:
                observation["service"] = "access_denied"
            elif code == 429:
                observation["service"] = "rate_limited"
            elif code >= 500:
                observation["service"] = "server_error"
            elif code >= 400:
                observation["service"] = "request_rejected"
            else:
                observation["service"] = "responding"
            if code >= 400 and code != 401:
                status[key] = "warning"
                issues.append(("medium", f"service_{key}", f"{name} 网络可达，服务返回 HTTP {code}"))
            if observation['path_verified'] is not True:
                status[key] = 'unknown'
                if path == 'direct_pac_unresolved' and target.get('id'):
                    observation['limitation'] = '自动代理路径尚未验证，直连结果不能代表系统路径'
                    issues.append(('low', f'reachability_{key}', f'{name} 的自动代理路径尚未验证'))
        status["reachability_results"] = observations
        status["reachability"] = ("warning" if issues else "ok" if any(o['applicable'] for o in observations.values()) else "skipped")
        return issues

    def get_status_lines(self, status):
        if status.get("reachability") == "unknown":
            return ["🟡 连通性: 检查未完成"]
        lines = []
        for target in self.cfg("targets", [])[:3]:
            name = target.get("name", "")
            key = target_key(target)
            observation = status.get("reachability_results", {}).get(key, {})
            icon = "🟢" if status.get(key) == "ok" else "🟡" if status.get(key) in ("warning", "unknown", "skipped") else "🔴"
            suffix = "（需认证）" if observation.get("service") == "auth_required" else ""
            lines.append(f"{icon} {name}{suffix}")
        return lines
