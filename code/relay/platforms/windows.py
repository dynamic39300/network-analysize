"""Windows observation: structured OS evidence, scoped identity, no configuration writes."""
import base64
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import uuid

from ..commands import checked, run_command
from ..environment import EnvironmentSnapshot

SECTIONS = ('interfaces', 'addresses', 'ip_interfaces', 'routes', 'dns', 'dns_clients',
            'nrpt', 'vpn_user', 'vpn_machine')


def current_scope():
    from ..private_files import windows_files
    kernel = ctypes.WinDLL('kernel32', use_last_error=True, winmode=0x800)
    get = kernel.ProcessIdToSessionId
    get.argtypes, get.restype = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL
    session = wintypes.DWORD()
    if not get(os.getpid(), ctypes.byref(session)):
        raise OSError('Windows session identity unavailable')
    return {'user_sid': windows_files().sid_text, 'session_id': session.value}


def system_directory():
    if sys.platform != 'win32':
        raise OSError('Windows collection requires Windows')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True, winmode=0x800)
    get = kernel.GetSystemDirectoryW
    get.argtypes, get.restype = [wintypes.LPWSTR, wintypes.UINT], wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = get(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise OSError('System directory unavailable')
    return Path(buffer.value)


def powershell_argv(script, directory=None):
    directory = directory or system_directory()
    executable = directory / 'WindowsPowerShell/v1.0/powershell.exe'
    return [str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand',
            base64.b64encode(script.encode('utf-16-le')).decode('ascii')]


def read_json(text):
    if len(text.encode('utf-8')) > 2 * 1024 * 1024:
        raise ValueError('Windows inventory exceeded its size limit')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate inventory field')
            result[key] = value
        return result
    return json.loads(text.lstrip('\ufeff'), object_pairs_hook=pairs)


def family(value):
    if value in ('IPv4', 2):
        return 'IPv4'
    if value in ('IPv6', 23):
        return 'IPv6'
    raise ValueError('Unknown address family')


def integer(value, minimum=0, maximum=2 ** 32 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('Invalid numeric network field')
    return value


def text_field(value, maximum=2048):
    if not isinstance(value, str) or len(value) > maximum or '\0' in value:
        raise ValueError('Invalid network text field')
    return value


def address(value, expected_family=None):
    result = ipaddress.ip_address(text_field(value, 100))
    if expected_family and (result.version == 4) != (expected_family == 'IPv4'):
        raise ValueError('Address family mismatch')
    return str(result)


class WindowsProxyReader:
    """Read process-user WinINet configuration and legacy WinHTTP defaults separately."""
    def read(self):
        winhttp = ctypes.WinDLL('winhttp', use_last_error=True, winmode=0x800)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True, winmode=0x800)
        kernel.GlobalFree.argtypes, kernel.GlobalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
        class UserProxy(ctypes.Structure):
            _fields_ = [('auto_detect', wintypes.BOOL), ('pac_url', ctypes.c_void_p),
                        ('proxy', ctypes.c_void_p), ('bypass', ctypes.c_void_p)]
        class DefaultProxy(ctypes.Structure):
            _fields_ = [('access_type', wintypes.DWORD), ('proxy', ctypes.c_void_p), ('bypass', ctypes.c_void_p)]
        observations = []
        for name, structure, scope in (
                ('WinHttpGetIEProxyConfigForCurrentUser', UserProxy, 'process_user_wininet'),
                ('WinHttpGetDefaultProxyConfiguration', DefaultProxy, 'winhttp_static_default')):
            value = structure()
            function = getattr(winhttp, name)
            function.argtypes, function.restype = [ctypes.POINTER(structure)], wintypes.BOOL
            pointers = ('proxy', 'bypass', 'pac_url') if structure is UserProxy else ('proxy', 'bypass')
            try:
                if not function(ctypes.byref(value)):
                    observations.append({'scope': scope, 'source': name, 'state': 'unknown',
                                         'error': ctypes.get_last_error()})
                    continue
                data = {key: ctypes.wstring_at(getattr(value, key)) if getattr(value, key) else '' for key in pointers}
                data.update(scope=scope, source=name, state='observed')
                if structure is UserProxy:
                    data['auto_detect'] = bool(value.auto_detect)
                else:
                    data['access_type'] = int(value.access_type)
                observations.append(data)
            finally:
                for key in pointers:
                    if getattr(value, key):
                        kernel.GlobalFree(getattr(value, key))
        return observations


class WindowsObserver:
    def __init__(self, runner=run_command, proxy_reader=None, directory=None, expected_scope=None):
        self.runner = runner
        self.proxy_reader = proxy_reader or WindowsProxyReader()
        self.directory, self.expected_scope = directory, expected_scope

    def observe(self, budget=None):
        runner = self.runner if budget is None else lambda argv, timeout=10: budget.run(self.runner, argv, timeout)
        script = Path(__file__).with_name('windows_inventory.ps1').read_text(encoding='utf-8')
        raw = checked(runner, powershell_argv(script, self.directory), timeout=30)
        environment = parse_inventory(read_json(raw), self.expected_scope)
        try:
            proxies = self.proxy_reader.read()
            if (not isinstance(proxies, list) or len(proxies) != 2 or
                    any(not isinstance(p, dict) for p in proxies) or
                    {p.get('scope') for p in proxies} != {'process_user_wininet', 'winhttp_static_default'} or
                    any(p.get('state') not in ('observed', 'unknown') for p in proxies)):
                raise ValueError('Invalid proxy observation')
            environment.proxies = proxies
            environment.capabilities['proxy_configuration'] = 'observed' if all(p.get('state') == 'observed' for p in proxies) else 'partial'
            if any(p.get('state') != 'observed' for p in proxies):
                environment.errors['proxy_configuration'] = 'Some Windows proxy scopes could not be read'
        except Exception:
            environment.errors['proxy_configuration'] = 'Windows proxy settings could not be read'
            environment.capabilities['proxy_configuration'] = 'unknown'
        return environment


def parse_inventory(document, expected_scope=None):
    if not isinstance(document, dict) or document.get('schema') != 'relay-windows-inventory-v1':
        raise ValueError('Unsupported Windows inventory schema')
    stamp = datetime.fromisoformat(text_field(document.get('captured_at'), 80))
    if stamp.tzinfo is None or abs((datetime.now(timezone.utc) - stamp).total_seconds()) > 120:
        raise ValueError('Windows inventory is stale or has no timezone')
    scope = document.get('scope', {})
    sid = text_field(scope.get('user_sid'), 200)
    if not re.fullmatch(r'S-1-\d+(?:-\d+)+', sid):
        raise ValueError('Windows user identity unavailable')
    session = integer(scope.get('session_id'))
    if type(scope.get('interactive')) is not bool or type(scope.get('elevated')) is not bool:
        raise ValueError('Windows process scope unavailable')
    if expected_scope and any(scope.get(key) != expected_scope[key] for key in ('user_sid', 'session_id')):
        raise ValueError('Windows observation belongs to a different user or session')
    scope = {**{key: scope[key] for key in ('user_sid', 'session_id', 'interactive', 'elevated')},
             'kind': 'interactive_user' if scope['interactive'] and session > 0
             and sid not in ('S-1-5-18', 'S-1-5-19', 'S-1-5-20') else 'service_or_background',
             'network_compartment': 'process_default'}
    environment = EnvironmentSnapshot('windows', stamp.isoformat(), scope)
    sections = document.get('sections')
    if not isinstance(sections, dict):
        raise ValueError('Missing Windows inventory sections')
    rows = {}
    for key in SECTIONS:
        section = sections.get(key, {})
        items = section.get('items')
        if section.get('state') != 'ok' or not isinstance(items, list) or len(items) > 8192 or any(not isinstance(i, dict) for i in items):
            environment.errors[key] = 'Windows ' + key + ' observation unavailable'
            environment.capabilities[key] = 'unknown'
            rows[key] = []
        else:
            environment.capabilities[key] = 'observed'
            rows[key] = items
    identities, indexes = set(), {}
    for row in rows['interfaces']:
        identity = str(uuid.UUID(text_field(row.get('id'), 80).strip('{}')))
        if identity in identities:
            raise ValueError('Duplicate interface identity')
        identities.add(identity)
        interface = {'id': identity, 'identity_source': 'NetworkInterface.Id', 'name': text_field(row.get('name')),
                     'type': text_field(row.get('type'), 80), 'state': text_field(row.get('state'), 40),
                     'addresses': [], 'ip_configuration': []}
        for af, key in (('IPv4', 'ipv4_index'), ('IPv6', 'ipv6_index')):
            index = row.get(key)
            if index is not None:
                pair = (af, integer(index, 1))
                if pair in indexes:
                    raise ValueError('Ambiguous interface index')
                indexes[pair] = interface
        environment.interfaces.append(interface)
    def reference(row, af):
        index = integer(row.get('index'), 1)
        interface = indexes.get((af, index))
        if interface is None:
            environment.errors['interface_mapping'] = 'Network evidence has no stable interface identity'
        return {'interface_id': interface['id'] if interface else None, 'interface_index': index, 'family': af}
    for row in rows['addresses']:
        af = family(row.get('family'))
        entry = {**reference(row, af), 'address': address(row.get('address'), af),
                 'prefix_length': integer(row.get('prefix_length'), 0, 32 if af == 'IPv4' else 128),
                 'state': text_field(row.get('state'), 80), 'origin': text_field(row.get('origin'), 80)}
        interface = indexes.get((af, row['index']))
        if interface:
            interface['addresses'].append(entry)
        else:
            environment.errors['interface_mapping'] = 'An address has no stable interface identity'
    for row in rows['ip_interfaces']:
        af = family(row.get('family'))
        entry = {**reference(row, af), 'metric': integer(row.get('metric')), 'dhcp': text_field(row.get('dhcp'), 40),
                 'state': text_field(row.get('state'), 80)}
        interface = indexes.get((af, row['index']))
        if interface:
            interface['ip_configuration'].append(entry)
    for row in rows['routes']:
        af = family(row.get('family'))
        network = ipaddress.ip_network(text_field(row.get('destination'), 120), strict=False)
        if (network.version == 4) != (af == 'IPv4'):
            raise ValueError('Route family mismatch')
        environment.routes.append({**reference(row, af), 'destination': str(network),
            'next_hop': address(row.get('next_hop'), af), 'metric': integer(row.get('metric')),
            'source': 'Get-NetRoute/ActiveStore', 'ownership': 'unknown',
            'protocol': text_field(row.get('protocol'), 80)})
    suffixes = {integer(row.get('index'), 1): text_field(row.get('suffix') or '') for row in rows['dns_clients']}
    for row in rows['dns']:
        af = family(row.get('family'))
        servers = row.get('servers')
        if not isinstance(servers, list):
            raise ValueError('DNS server list is invalid')
        environment.resolvers.append({**reference(row, af), 'scope': 'interface',
            'servers': [address(value, af) for value in servers], 'suffix': suffixes.get(row['index']),
            'source': 'Get-DnsClientServerAddress', 'ownership': 'unknown'})
    for row in rows['nrpt']:
        if (not isinstance(row.get('namespaces'), list) or not row['namespaces']
                or not isinstance(row.get('servers'), list)):
            raise ValueError('NRPT policy is invalid')
        environment.resolvers.append({'scope': 'namespace', 'namespaces': [text_field(v) for v in row['namespaces']],
            'servers': [text_field(v) for v in row['servers']], 'source': 'Get-DnsClientNrptPolicy/Effective',
            'ownership': 'policy_source_unknown', 'dnssec_required': row.get('dnssec_required')})
    for key in ('vpn_user', 'vpn_machine'):
        for row in rows[key]:
            environment.vpns.append({'profile_id': text_field(row.get('id'), 80), 'name': text_field(row.get('name')),
                'state': text_field(row.get('state'), 80), 'scope': 'current_user' if key == 'vpn_user' else 'all_users',
                'source': 'Get-VpnConnection', 'interface_id': None, 'owner_confirmed': False,
                'split_tunnel': row.get('split_tunnel'), 'tunnel_type': text_field(row.get('tunnel_type'), 80)})
    environment.capabilities.update(mature_repairs='not_implemented', vendor_vpn_ownership='unknown',
                                    application_paths='unknown', target_routes='not_implemented')
    environment.limitations = [
        'Separate OS reads are not an atomic snapshot; target paths and write preconditions need fresh validation.',
        'Routes cover only the process default network compartment; a route table is not proof of actual application traffic.',
        'DNS server lists do not establish per-domain effective resolution or permission to change policy.',
        'Windows VPN profiles do not enumerate all vendor VPNs or prove an interface owner.',
        'WinINet process-user settings, legacy WinHTTP defaults and application overrides are distinct scopes.',
    ]
    return environment
