#!/usr/bin/env python3
"""Explicit Agent desktop/service entry; legacy desktop remains available."""
import argparse
import multiprocessing
from pathlib import Path

from relay.core import default_directory
from relay.lifecycle import CoreLifecycle, inhibited
from relay.mac_service import MacBackgroundService, MacPrivilegedService


def main(argv=None):
    parser = argparse.ArgumentParser(description='NetCare Agent lifecycle')
    role = parser.add_mutually_exclusive_group(required=True)
    role.add_argument('--agent-desktop', action='store_true')
    role.add_argument('--core-service', action='store_true')
    role.add_argument('--lifecycle', choices=('status', 'start', 'stop', 'enable-background', 'disable-background',
                                            'enable-helper', 'disable-helper', 'prepare-uninstall'))
    parser.add_argument('--data-dir', type=Path, default=default_directory())
    parser.add_argument('--launch-id')
    parser.add_argument('--managed', action='store_true')
    args = parser.parse_args(argv)
    from relay_core import emit, main as core_main
    try:
        if args.core_service:
            if inhibited(args.data_dir):
                return 0
            options = ['--data-dir', str(args.data_dir), 'serve', '--restore-guard']
            if args.managed:
                options += ['--managed']
            if args.launch_id:
                options += ['--launch-id', args.launch_id]
            return core_main(options)
        lifecycle = CoreLifecycle(args.data_dir, service=MacBackgroundService(args.data_dir), helper=MacPrivilegedService(args.data_dir))
        if args.agent_desktop:
            from relay.remote_desktop import run_desktop
            return run_desktop(args.data_dir, lifecycle=lifecycle)
        operations = {'status': lifecycle.status, 'start': lambda: lifecycle.start(explicit=True),
            'stop': lifecycle.stop, 'enable-background': lambda: lifecycle.background(True),
            'disable-background': lambda: lifecycle.background(False), 'prepare-uninstall': lifecycle.prepare_uninstall,
            'enable-helper': lambda: lifecycle.privileged(True), 'disable-helper': lambda: lifecycle.privileged(False)}
        emit(operations[args.lifecycle]())
        return 0
    except Exception as exc:
        emit({'error': type(exc).__name__, 'message': '生命周期操作未确认完成；未强制停止或删除数据。'})
        return 1


if __name__ == '__main__':
    multiprocessing.freeze_support()
    raise SystemExit(main())
