"""VPN path evidence and client ownership are deliberately separate facts."""
import re
from .base import BaseCheck, register
from ..network import ip_addresses, key_values, process_names, tunnel_interfaces


@register
class VpnCheck(BaseCheck):
    name = "vpn"
    display_name = "VPN"
    description = "以路由和活动接口验证 VPN 路径；归属不明时保留未知状态"

    def check(self, status):
        tunnels = tunnel_interfaces(self.command(["/sbin/ifconfig", "-a"]))
        processes = process_names(self.command(["/bin/ps", "-axo", "pid=,comm="]))
        clients = self.cfg("known_clients", [])
        running = [client for client in clients
                   if str(client.get("process", "")).casefold() in processes]
        anchors = self.cfg("route_anchors", [])
        if not anchors and self.config and self.config.get("dns.enforce_company_dns_on_vpn", False):
            anchors = self.cfg("company_dns", [])
        anchors = ip_addresses(anchors)
        routes = {}
        for anchor in anchors:
            route = key_values(self.command(["/sbin/route", "-n", "get", anchor]))
            interface = route.get("interface")
            if not interface:
                raise ValueError("路由查询未返回接口")
            routes[anchor] = interface

        candidates = set(routes.values())
        path = next(iter(candidates)) if len(candidates) == 1 else None
        path_ok = bool(path and path in tunnels and routes)
        owners = []
        if path_ok:
            for client in clients:
                service_id = client.get("service_id")
                if not service_id:
                    continue
                # Explicit OS service mapping is ownership evidence. A process
                # or a numbered utun alone never proves ownership.
                service = self.command(["/usr/sbin/scutil", "--nc", "status", str(service_id)])
                fields = key_values(service)
                if service and service.splitlines()[0].strip() == "Connected" and fields.get("InterfaceName") == path:
                    owners.append(client.get("name", "已配置 VPN"))

        status.update(vpn_ip="", vpn_client=None, vpn_path="unknown")
        status["vpn_evidence"] = {"interfaces": tunnels, "routes": routes,
                    "process_candidates": [c.get("name") for c in running],
                    "interface": path if path_ok else None,
                    "owner_confirmed": len(owners) == 1}
        if path_ok:
            status["vpn_path"] = "ok"
            status["vpn_ip"] = tunnels[path]["addresses"][0]
            if len(owners) == 1:
                status.update(vpn="ok", vpn_client=owners[0])
                return []
            status["vpn"] = "unknown"
            return [("low", "vpn_owner_unconfirmed", f"VPN 路径经 {path} 可用，客户端归属未确认；不自动调整 DNS")]
        has_tunnel_route = any(re.fullmatch(r'(?:utun|tun|ppp)\d+', interface) for interface in routes.values())
        if not tunnels and not running and not has_tunnel_route:
            status.update(vpn="off", vpn_path="off")
            return []
        status["vpn"] = "unknown"
        detail = "档案路由与活动接口不一致" if routes else "尚未配置路由锚点或系统服务归属"
        return [("low", "vpn_unconfirmed", f"VPN 状态未确认：{detail}；不自动调整 DNS")]

    def get_status_lines(self, status):
        if status.get("vpn") == "ok":
            return [f"🟢 VPN: 已连接 ({status.get('vpn_client')})"]
        if status.get("vpn_path") == "ok":
            interface = status.get("vpn_evidence", {}).get("interface", "")
            return [f"🟡 VPN 路径: {interface} 可用（客户端归属未确认）"]
        if status.get("vpn") == "off":
            return ["⚪ VPN: 未发现活动隧道"]
        return ["🟡 VPN: 检查未完成或证据不足"]
