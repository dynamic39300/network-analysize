"""Known proxy applications are informational, never proof of proxy residue."""
from .base import BaseCheck, register
from ..network import process_names


@register
class ProxyCheck(BaseCheck):
    name = "proxy"
    display_name = "代理工具"
    description = "识别已知代理工具；代理可用性由实际系统端点检测"

    def check(self, status):
        names = process_names(self.command(["/bin/ps", "-axo", "pid=,comm="]))
        matches = [c for c in self.cfg("known_clients", [])
                   if str(c.get("process", "")).casefold() in names]
        status["proxy_client"] = ", ".join(c.get("name", "代理工具") for c in matches) or None
        status["proxy_app"] = "running" if matches else "off"
        return []

    def get_status_lines(self, status):
        if status.get("proxy_app") == "running":
            return [f"⚪ 代理工具: {status.get('proxy_client')}"]
        if status.get("proxy_app") == "unknown":
            return ["🟡 代理工具: 检查未完成"]
        return ["⚪ 代理工具: 未识别到已知客户端"]
