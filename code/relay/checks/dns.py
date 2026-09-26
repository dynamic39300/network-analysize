"""Observe effective resolvers, including DHCP and scoped VPN resolvers."""
import re
from .base import BaseCheck, register
from ..network import dns_list, ip_addresses


@register
class DnsCheck(BaseCheck):
    name = "dns"
    display_name = "DNS"
    description = "检测手工与系统实际生效的 DNS 配置"

    def check(self, status):
        manual = dns_list(self.command(["/usr/sbin/networksetup", "-getdnsservers", self.wifi_service()]))
        raw = self.command(["/usr/sbin/scutil", "--dns"])
        effective = ip_addresses(re.findall(r"nameserver\[\d+\]\s*:\s*(\S+)", raw))
        if not effective and "No DNS configuration available" not in raw:
            raise ValueError("无法识别系统 DNS 输出")
        status["dns_manual_servers"] = manual
        status["dns_effective_servers"] = effective
        status["dns_servers"] = " ".join(effective)
        status["dns_mode"] = "manual" if manual else "automatic"
        status["dns"] = "ok" if effective else "error"
        if not effective:
            return [("high", "dns_unavailable", "系统没有可用的 DNS 解析器")]

        company = ip_addresses(self.config.get("vpn.company_dns", []) if self.config else [])
        public = ip_addresses(self.cfg("public_dns", []))
        enforce = self.cfg("enforce_company_dns_on_vpn", False) and bool(company)
        # Scoped resolvers can legitimately coexist. Only an explicit profile
        # and confirmed VPN ownership justify policy-specific repair advice.
        if enforce and status.get("vpn") == "ok":
            if set(manual) & set(public):
                status["dns"] = "warning"
                return [("medium", "dns_mixed_on_vpn", "手工 DNS 与已确认的公司 VPN 档案不一致")]
            if not set(company) & set(effective):
                status["dns"] = "warning"
                return [("medium", "dns_no_company_on_vpn", "已确认的公司 VPN 未发现档案 DNS")]
        elif enforce and status.get("vpn") == "off" and status.get("vpn_path") == "off":
            if set(manual) & set(company) and not set(manual) & set(public):
                status["dns"] = "warning"
                return [("medium", "dns_company_leftover", "无活动 VPN 隧道，手工 DNS 仍为公司档案地址")]
        return []

    def get_status_lines(self, status):
        if status.get("dns") == "unknown":
            return ["🟡 DNS: 检查未完成"]
        icon = "🟢" if status.get("dns") == "ok" else "🟡"
        mode = "自动" if status.get("dns_mode") == "automatic" else "手工"
        return [f"{icon} DNS ({mode}): {status.get('dns_servers', '无')[:60]}"]
