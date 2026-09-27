#!/usr/bin/env python3
"""Verify only idle launch/stop in a temporary data directory; no service registration."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code'))
from relay.lifecycle import CoreLifecycle, core_environment
from relay_config import DEFAULT_CONFIG


def main():
    binary = ROOT / 'dist/macos/NetCare.app/Contents/MacOS/NetCare'
    with tempfile.TemporaryDirectory(prefix='relay-idle-bundle-', dir='/tmp') as temporary:
        data = Path(temporary)
        config = copy.deepcopy(DEFAULT_CONFIG)
        config['wifi'].update(interface='en0', service_name='Wi-Fi')
        path = data / 'config.json'
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        manager = CoreLifecycle(data)
        def command(operation):
            result = subprocess.run([str(binary), '--lifecycle', operation, '--data-dir', str(data)],
                capture_output=True, text=True, timeout=100, env=core_environment())
            if result.returncode != 0:
                raise AssertionError('Frozen lifecycle failed: ' + operation + '\n' + result.stdout + result.stderr)
            return json.loads(result.stdout)
        try:
            first = command('start')
            assert first['ready'] and not first['guard']['enabled']
            assert first['snapshot']['last_check'] is None
            assert not first['model']['consented'] and first['trust']['grant'] is None
            assert command('start')['core_instance'] == first['core_instance']
            assert command('status')['background']['state'] == 'unavailable'
            receipt = command('stop')
            assert receipt['core_instance'] == first['core_instance'] and receipt['state'] == 'stopped'
            assert command('status')['core'] == 'stopped' and manager.owners_idle()
            assert (data / 'agent').is_dir()
            print(json.dumps({'frozen_lifecycle': 'passed', 'guard_enabled': False, 'check_started': False,
                'service_registered': False, 'data_preserved': True}))
        finally:
            # Only the disposable data directory is addressed; no installed core is discovered.
            if not manager.owners_idle():
                manager.stop(timeout=90)
            time.sleep(0.15)


if __name__ == '__main__':
    main()
