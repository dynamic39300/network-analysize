"""Small parsers for macOS network observations; they perform no mutations."""
import ipaddress
import os
import re


def ip_addresses(values):
    if not isinstance(values, (list, tuple)):
        raise ValueError("DNS/路由锚点必须是 IP 地址列表")
    result = []
    for value in values:
        value = str(value).strip()
        ipaddress.ip_address(value.split("%", 1)[0])
        if value not in result:
            result.append(value)
    return result


def interfaces(output):
    result = {}
    current = None
    for line in output.splitlines():
        match = re.match(r"^(\S+): flags=.*?<([^>]+)>", line)
        if match:
            current = {"up": "UP" in match[2].split(","), "addresses": []}
            result[match[1]] = current
        elif current is not None:
            match = re.match(r"\s+inet6?\s+(\S+)", line)
            if match:
                try:
                    address = ipaddress.ip_address(match[1].split("%", 1)[0])
                except ValueError:
                    continue
                if not (address.is_link_local or address.is_loopback or address.is_unspecified):
                    current["addresses"].append(str(address))
    if not result:
        raise ValueError("无法识别系统网络接口输出")
    return result


def tunnel_interfaces(output):
    return {name: data for name, data in interfaces(output).items()
            if re.fullmatch(r"(?:utun|tun|ppp)\d+", name) and data["up"] and data["addresses"]}


def process_names(output):
    # Executable names, not user-controlled command-line substrings.
    names = set()
    for line in output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            names.add(os.path.basename(parts[1]).casefold())
    if not names:
        raise ValueError('无法识别系统进程列表')
    return names


def key_values(output):
    return {m[1].strip(): m[2].strip() for line in output.splitlines()
            if (m := re.match(r"\s*([^:]+?)\s*:\s*(.*)", line))}


def dns_list(output):
    if "There aren't any DNS Servers" in output:
        return []
    if not output.strip():
        raise ValueError('DNS 配置命令没有返回有效内容')
    return ip_addresses([line.strip() for line in output.splitlines() if line.strip()])
