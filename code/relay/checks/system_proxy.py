"""Inspect actual configured endpoints, including legitimate remote proxies."""
import ipaddress
from .base import BaseCheck, register
from ..network import key_values


@register
class SystemProxyCheck(BaseCheck):
    name = "system_proxy"
    display_name = "系统代理"
    description = "检测实际代理端点，不依赖客户端白名单"

    def _load_enabled(self):
        return True

    def check(self, status):
        raw = self.command(["/usr/sbin/scutil", "--proxy"])
        if "<dictionary>" not in raw:
            raise ValueError("无法识别系统代理输出")
        fields = key_values(raw)
        endpoints, issues, failed_types = {}, [], []
        for kind in ("HTTP", "HTTPS", "SOCKS"):
            enabled = fields.get(kind + "Enable", "0")
            if enabled not in ("0", "1"):
                raise ValueError("无法识别代理启用状态")
            if enabled != "1":
                continue
            host = fields.get(kind + "Proxy", "")
            port = int(fields.get(kind + "Port", "0"))
            if not host or not 1 <= port <= 65535:
                raise ValueError("代理端点不完整")
            result = self.runner(["/usr/bin/nc", "-G", "2", "-z", host, str(port)], timeout=3)
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = host.casefold() == "localhost"
            endpoints[kind.lower()] = {"host": host, "port": port, "local": local,
                                       "state": "ok" if result.ok else "unknown"}
            if not result.ok:
                if result.returncode == 1 and not result.timed_out:
                    endpoints[kind.lower()]["state"] = "unreachable"
                    if local:
                        failed_types.append(kind.lower())
                    else:
                        issues.append(("medium", "proxy_endpoint_unreachable", f"{kind} 远程代理端点不可达；保留现有代理配置"))
                else:
                    issues.append(("medium", "proxy_check_incomplete", f"{kind} 代理端点检查未完成"))
        pac = fields.get("ProxyAutoConfigEnable", "0") == "1" or fields.get("ProxyAutoDiscoveryEnable", "0") == "1"
        status["proxy_details"] = endpoints
        status["proxy_pac"] = pac
        status["proxy_failed_types"] = failed_types
        status["proxy"] = "on" if endpoints or pac else "off"
        status["system_proxy"] = "unknown" if any(e['state'] == 'unknown' for e in endpoints.values()) else "warning" if issues or failed_types else "ok"
        if failed_types:
            issues.append(("high", "proxy_leftover", "本机代理端点未监听：" + ", ".join(failed_types)))
        return issues

    def get_status_lines(self, status):
        if status.get("system_proxy") == "unknown":
            return ["🟡 系统代理: 检查未完成"]
        if status.get("proxy") == "off":
            return ["⚪ 系统代理: 已关闭"]
        if status.get("proxy_pac"):
            return ["⚪ 系统代理: 自动配置（PAC/WPAD）"]
        icon = "🟢" if status.get("system_proxy") == "ok" else "🟡"
        return [f"{icon} 系统代理: 已开启"]
