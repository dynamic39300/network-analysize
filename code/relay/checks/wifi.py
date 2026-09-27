"""Wi-Fi observations; optional SSID access is separate from connectivity."""
from .base import BaseCheck, register
from ..network import interfaces, key_values


@register
class WifiCheck(BaseCheck):
    name = "wifi"
    display_name = "Wi-Fi"
    description = "检测接口地址，并区分使用其他网络服务的情况"

    def check(self, status):
        all_interfaces = interfaces(self.command(["/sbin/ifconfig", "-a"]))
        iface = self.wifi_interface()
        data = all_interfaces.get(iface, {})
        addresses = data.get("addresses", [])
        status.update(wifi_interface=iface, wifi_ip=addresses[0] if addresses else "", wifi_network="未知")
        if data.get("up") and addresses:
            status["wifi"] = "ok"
            result = self.runner(["/usr/sbin/networksetup", "-getairportnetwork", iface], timeout=5)
            if result.ok and result.stdout.startswith("Current Wi-Fi Network: "):
                status["wifi_network"] = result.stdout.partition(": ")[2].strip()
            else:
                status["wifi_name_available"] = False
            return []
        route = self.command(["/sbin/route", "-n", "get", "default"])
        default_interface = key_values(route).get("interface")
        if not default_interface:
            raise ValueError("默认路由未返回接口")
        status["default_interface"] = default_interface
        if default_interface != iface and all_interfaces.get(default_interface, {}).get("addresses"):
            status["wifi"] = "off"
            return []
        status["wifi"] = "error"
        return [("high", "wifi_disconnected", "当前网络接口未连接或无可路由地址")]

    def get_status_lines(self, status):
        state = status.get("wifi")
        if state == "off":
            return [f"⚪ Wi-Fi: 未使用（当前接口 {status.get('default_interface', '未知')}）"]
        if state == "unknown":
            return ["🟡 Wi-Fi: 检查未完成"]
        icon = "🟢" if state == "ok" else "🔴"
        return [f"{icon} Wi-Fi: {status.get('wifi_network', '未知')}"]
