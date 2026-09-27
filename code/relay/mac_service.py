"""Explicit SMAppService choices, separate from task and model authorization."""
from pathlib import Path
import os
import platform
import subprocess
import sys

LABEL = 'com.wangxinlei.relay.agent'
PLIST = LABEL + '.plist'


class _MacRegistration:
    def status(self):
        if self.app is None:
            return {'available': False, 'state': 'unavailable', 'reason': self.reason}
        state = {0: 'not_registered', 1: 'enabled', 2: 'requires_approval', 3: 'not_found'}.get(int(self.app.status()), 'unknown')
        return {'available': True, 'state': state, 'reason': None}

    def _change(self, method):
        if self.app is None:
            raise NotImplementedError('Registration requires a supported app bundle')
        ok, error = getattr(self.app, method)(None)
        if not ok:
            raise RuntimeError('Service registration was not confirmed')

    def open_settings(self):
        if self.app is None:
            raise NotImplementedError('Background settings are unavailable')
        import ServiceManagement
        ServiceManagement.SMAppService.openSystemSettingsLoginItems()


class MacBackgroundService(_MacRegistration):
    def __init__(self, data_dir):
        from .core import default_directory
        self.app = None
        self.reason = 'bundle_required'
        if sys.platform != 'darwin' or not getattr(sys, 'frozen', False):
            return
        if int(platform.mac_ver()[0].split('.')[0]) < 13:
            self.reason = 'macos_13_required'
            return
        if Path(data_dir).absolute() != default_directory().absolute():
            self.reason = 'default_directory_required'
            return
        bundle = Path(sys.executable).resolve().parents[2]
        if not (bundle / 'Contents/Library/LaunchAgents' / PLIST).is_file():
            return
        import ServiceManagement
        self.app = ServiceManagement.SMAppService.agentServiceWithPlistName_(PLIST)
        self.reason = None

    def register(self):
        if self.status()['state'] != 'enabled':
            self._change('registerAndReturnError_')

    def unregister(self):
        if self.status()['state'] != 'not_registered':
            self._change('unregisterAndReturnError_')

    def start(self):
        if self.status()['state'] != 'enabled':
            raise PermissionError('Background service is not enabled')
        subprocess.run(['/bin/launchctl', 'kickstart', f'gui/{os.geteuid()}/{LABEL}'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=True, timeout=10, env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'})


class MacPrivilegedService(_MacRegistration):
    def __init__(self, data_dir):
        from .core import default_directory
        from .mac_helper import HELPER_LABEL, MacHelperTransport, trusted_pair
        self.app, self.transport = None, None
        self.reason = 'signed_bundle_required'
        if sys.platform != 'darwin' or not getattr(sys, 'frozen', False):
            return
        if int(platform.mac_ver()[0].split('.')[0]) < 13:
            self.reason = 'macos_13_required'
            return
        if Path(data_dir).absolute() != default_directory().absolute():
            self.reason = 'default_directory_required'
            return
        try:
            pair = trusted_pair()
        except (PermissionError, OSError):
            return
        plist = HELPER_LABEL + '.plist'
        bundle = Path(sys.executable).resolve().parents[2]
        if not (bundle / 'Contents/Library/LaunchDaemons' / plist).is_file():
            return
        import ServiceManagement
        self.app = ServiceManagement.SMAppService.daemonServiceWithPlistName_(plist)
        self.transport = MacHelperTransport(requirement=pair['helper_requirement'])
        self.reason = None

    def register(self):
        if self.status()['state'] != 'enabled':
            self._change('registerAndReturnError_')
        if self.status()['state'] == 'enabled':
            if self.transport.call('resume') != {'draining': False}:
                raise RuntimeError('Helper still has an unsettled task')

    def unregister(self):
        state = self.status()['state']
        if self.app is None:
            raise NotImplementedError('System repair requires the signed app bundle')
        if state == 'not_registered':
            return
        if state not in ('enabled', 'requires_approval'):
            raise RuntimeError('Helper registration is unknown')
        # A revoked approval may coexist with a previously running helper.
        # Require its durable no-active-task acknowledgement before removal.
        if self.transport.call('drain') != {'draining': True}:
            raise RuntimeError('Helper shutdown is unconfirmed; registration preserved')
        self._change('unregisterAndReturnError_')
        if self.status()['state'] != 'not_registered':
            raise RuntimeError('Helper removal was not confirmed')
