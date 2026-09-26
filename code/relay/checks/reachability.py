"""Separate transport reachability from authentication and service failures."""
from urllib.parse import urlsplit
from .base import BaseCheck, register


@register
class ReachabilityCheck(BaseCheck):
    name = "reachability"
    display_name = "连通性"
    description = "区分 DNS、连接、TLS、HTTP 和服务拒绝"

    def check(self, status):
        issues, observations = [], {}
        for target in self.cfg("targets", []):
            name, url = target.get("name", "unknown"), target.get("url", "")
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("检测目标必须是无凭据的 HTTP(S) 地址")
            timeout = max(1, min(30, int(target.get("timeout", 8))))
            key = name.lower().replace(" ", "_")
            args = ["/usr/bin/curl", "-sS", "--max-time", str(timeout), "--connect-timeout", str(min(timeout, 5)),
                    "-o", "/dev/null", "-w", "%{http_code}"]
            proxy = status.get("proxy_details", {}).get(parsed.scheme)
            socks = status.get("proxy_details", {}).get("socks")
            path = "direct_pac_unresolved" if status.get("proxy_pac") else "direct"
            selected = proxy or socks
            if selected:
                scheme = "http" if proxy else "socks5h"
                host = selected["host"]
                host = f"[{host}]" if ":" in host and not host.startswith("[") else host
                args += ["--proxy", f"{scheme}://{host}:{selected['port']}", "--noproxy", ""]
                path = "system_proxy"
            else:
                args += ["--noproxy", "*"]
            result = self.runner(args + ["--", url], timeout=timeout + 2)
            observation = {"path": path, "transport": "unknown", "http_status": None, "service": "unknown"}
            observations[key] = observation
            if not result.ok:
                category = {6: "dns_error", 7: "connect_error", 28: "timeout", 35: "tls_error", 60: "tls_error"}.get(result.returncode, "check_failed")
                observation.update(transport=category, service="unavailable")
                status[key] = "unknown" if category == "check_failed" else "error"
                issues.append(("medium", f"reachability_{key}", f"{name} 检测异常：{category}"))
                continue
            code_text = result.stdout.strip()
            if not code_text.isdigit() or not 100 <= int(code_text) <= 599:
                raise ValueError("curl 未返回有效 HTTP 状态码")
            code = int(code_text)
            observation.update(transport="ok", http_status=code)
            status[key] = "ok"
            if code == 401:
                observation["service"] = "auth_required"
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
        status["reachability_results"] = observations
        status["reachability"] = "warning" if issues else "ok" if observations else "skipped"
        return issues

    def get_status_lines(self, status):
        if status.get("reachability") == "unknown":
            return ["🟡 连通性: 检查未完成"]
        lines = []
        for target in self.cfg("targets", [])[:3]:
            name = target.get("name", "")
            key = name.lower().replace(" ", "_")
            observation = status.get("reachability_results", {}).get(key, {})
            icon = "🟢" if status.get(key) == "ok" else "🟡" if status.get(key) in ("warning", "unknown") else "🔴"
            suffix = "（需认证）" if observation.get("service") == "auth_required" else ""
            lines.append(f"{icon} {name}{suffix}")
        return lines
