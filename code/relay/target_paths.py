"""macOS target-path evidence for explicitly configured VPN-only requests."""
import ipaddress
from urllib.parse import urlsplit

from .commands import checked
from .network import key_values


def vpn_destination(target, status, runner):
    evidence = status.get('vpn_evidence', {})
    interface = evidence.get('interface')
    if status.get('vpn') != 'ok' or not evidence.get('owner_confirmed') or not interface:
        return None, '尚无已确认归属的 VPN 路径'
    parsed = urlsplit(target['url'])
    try:
        addresses = [str(ipaddress.ip_address(parsed.hostname))]
    except ValueError:
        raw = checked(runner, ['/usr/bin/dscacheutil', '-q', 'host', '-a', 'name', parsed.hostname], timeout=5)
        addresses = []
        for line in raw.splitlines():
            key, _, value = line.partition(':')
            if key.strip() in ('ip_address', 'ipv6_address'):
                addresses.append(str(ipaddress.ip_address(value.strip())))
    for address in addresses[:8]:
        current = route_interface(address, runner)
        if current == interface:
            return {'address': address, 'interface': interface, 'owner': status.get('vpn_client')}, ''
    return None, '目标地址的路由尚未指向已确认的 VPN，未发起访问'


def route_interface(address, runner):
    return key_values(checked(runner, ['/sbin/route', '-n', 'get', address], timeout=5)).get('interface')


def pinned_host_option(url, address):
    parsed = urlsplit(url)
    try:
        ipaddress.ip_address(parsed.hostname)
        return []
    except ValueError:
        host_address = f'[{address}]' if ':' in address else address
        return ['--resolve', f'{parsed.hostname}:{parsed.port or (443 if parsed.scheme == "https" else 80)}:{host_address}']
