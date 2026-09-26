"""IPv6 is observed by default; only an explicit policy asks for a change."""
from .base import BaseCheck, register
from ..network import key_values


@register
class Ipv6Check(BaseCheck):
    name = "ipv6"
    display_name = "IPv6"
    description = "观察 IPv6 模式，不将启用本身视为故障"

    def check(self, status):
        policy = self.cfg("should_be", "observe")
        if policy == "ignore":
            status["ipv6"] = "ignored"
            return []
        raw = self.command(["/usr/sbin/networksetup", "-getinfo", self.wifi_service()])
        mode = key_values(raw).get("IPv6")
        if mode not in ("Off", "Automatic", "Manual", "Link-local only", "Link-local"):
            raise ValueError("无法识别 IPv6 模式")
        status["ipv6_mode"] = mode
        status["ipv6"] = "off" if mode == "Off" else "on"
        if policy == "off" and mode != "Off":
            return [("medium", "ipv6_enabled", "IPv6 已启用，与用户明确选择的关闭策略不一致")]
        if policy == "on" and mode == "Off":
            return [("low", "ipv6_disabled", "IPv6 已关闭，与用户选择的策略不一致")]
        return []

    def get_status_lines(self, status):
        if status.get("ipv6") == "unknown":
            return ["🟡 IPv6: 检查未完成"]
        return []
