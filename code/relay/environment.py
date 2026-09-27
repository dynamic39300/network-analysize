"""Versioned environment evidence shared by platform observers and the Agent."""
import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime
import os


@dataclass
class EnvironmentSnapshot:
    platform: str
    captured_at: str
    scope: dict
    interfaces: list = field(default_factory=list)
    routes: list = field(default_factory=list)
    resolvers: list = field(default_factory=list)
    proxies: list = field(default_factory=list)
    vpns: list = field(default_factory=list)
    capabilities: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)
    limitations: list = field(default_factory=list)
    schema: str = 'relay-environment-v1'

    def to_dict(self):
        return asdict(self)


def macos_environment(snapshot, config):
    """Expose only the legacy checks' actual coverage, without inventing GUIDs."""
    status = snapshot['status']
    stamp = snapshot.get('last_check') or datetime.now()
    name = config.get('wifi.interface', 'auto') if config else 'auto'
    evidence = status.get('vpn_evidence', {})
    interfaces = []
    if name != 'auto' and status.get('wifi_ip'):
        interfaces.append({'id': None, 'name': name, 'identity_source': 'bsd_name',
                           'addresses': [status['wifi_ip']], 'state': status.get('wifi')})
    for interface, details in evidence.get('interfaces', {}).items():
        interfaces.append({'id': None, 'name': interface, 'identity_source': 'bsd_name',
                           'addresses': list(details.get('addresses', [])), 'state': 'observed'})
    return EnvironmentSnapshot(
        platform='macos', captured_at=stamp.isoformat(),
        scope={'kind': 'process_user', 'uid': os.getuid() if hasattr(os, 'getuid') else None},
        interfaces=interfaces,
        routes=[{'destination': address, 'interface_name': interface, 'source': 'route_get'}
                for address, interface in evidence.get('routes', {}).items()],
        resolvers=[{'scope': 'legacy_summary', 'servers': copy.deepcopy(status.get('dns_effective_servers', [])),
                    'source': 'scutil_dns', 'ownership': 'unknown'}],
        proxies=[{'scope': 'system_summary', 'endpoints': copy.deepcopy(status.get('proxy_details', {})),
                  'auto_config': status.get('proxy_pac'), 'source': 'scutil_proxy'}],
        vpns=[{'owner': status.get('vpn_client'), 'interface_name': evidence.get('interface'),
               'owner_confirmed': evidence.get('owner_confirmed', False), 'state': status.get('vpn')}],
        capabilities={'inventory': 'partial', 'target_routes': 'configured_vpn_targets',
                      'mature_repairs': 'per_run_confirmation'},
        errors=copy.deepcopy(snapshot.get('check_errors', {})),
        limitations=['Legacy macOS checks do not enumerate all service identities, resolver scopes or application paths.'],
    ).to_dict()
