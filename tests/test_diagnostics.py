"""Regression tests use a fake macOS command boundary; never touch real networking."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code'))
from relay.commands import CommandResult, run_command
from relay.engine import DetectionEngine, FixEngine
from relay.checks.vpn import VpnCheck
from relay.checks.dns import DnsCheck
from relay.checks.system_proxy import SystemProxyCheck
from relay_config import ConfigManager, DEFAULT_CONFIG


class FakeMac:
    """Small observable OS with independent reads, writes and fault injection."""
    def __init__(self):
        self.manual_dns = []
        self.effective_dns = ['192.168.0.1']
        self.tunnels = {}
        self.processes = []
        self.anchor_routes = {}
        self.services = {}
        self.proxies = {}
        self.proxy_reachable = True
        self.ipv6 = 'Automatic'
        self.calls, self.mutations = [], []
        self.fail_apply = False
        self.fail_rollback = False
        self.fail_dns_read_after_apply = False
        self.break_network_after_apply = False
        self.http_status = '200'
        self.has_applied = False
        self.curl_exit = 0
        self.pac = False

    def __call__(self, argv, timeout=10):
        self.assert_argv(argv)
        self.calls.append(tuple(argv))
        command, args = Path(argv[0]).name, argv[1:]
        if command == 'ifconfig':
            output = 'lo0: flags=8049<UP,LOOPBACK,RUNNING> mtu 16384\n\tinet 127.0.0.1\n'
            output += 'en0: flags=8863<UP,BROADCAST,RUNNING> mtu 1500\n\tinet 192.168.0.106\n'
            output += 'utun4: flags=8051<UP,POINTOPOINT,RUNNING> mtu 1380\n\tinet6 fe80::1%utun4\n'
            for interface, address in self.tunnels.items():
                family = 'inet6' if ':' in address else 'inet'
                output += f'{interface}: flags=8051<UP,POINTOPOINT,RUNNING> mtu 1380\n\t{family} {address}\n'
            return CommandResult(output)
        if command == 'ps':
            return CommandResult('1 /sbin/launchd\n' + '\n'.join(f'{100+i} /Applications/{name}.app/Contents/MacOS/{name}' for i, name in enumerate(self.processes)))
        if command == 'route':
            return CommandResult('interface: ' + self.anchor_routes.get(args[-1], 'en0'))
        if command == 'nc':
            return CommandResult(returncode=0 if self.proxy_reachable else 1)
        if command == 'scutil':
            if args == ['--dns']:
                if self.has_applied and self.fail_dns_read_after_apply:
                    return CommandResult(stderr='simulated system failure', returncode=1)
                effective = self.manual_dns or self.effective_dns
                return CommandResult('No DNS configuration available' if not effective else 'DNS configuration\nresolver #1\n' + '\n'.join(f'  nameserver[{i}] : {ip}' for i, ip in enumerate(effective)))
            if args == ['--proxy']:
                output = '<dictionary> {\n'
                if self.pac:
                    output += ' ProxyAutoConfigEnable : 1\n'
                for kind, value in self.proxies.items():
                    prefix = kind.upper()
                    output += f' {prefix}Enable : {int(value["enabled"])}\n {prefix}Proxy : {value["host"]}\n {prefix}Port : {value["port"]}\n'
                return CommandResult(output + '}')
            if args[:2] == ['--nc', 'status']:
                interface = self.services.get(args[-1])
                return CommandResult('Connected\nInterfaceName : ' + interface if interface else 'Disconnected')
        if command == 'curl':
            if self.has_applied and self.break_network_after_apply:
                return CommandResult('000', 'timeout', 28)
            return CommandResult(self.http_status, returncode=self.curl_exit)
        if command == 'networksetup':
            option = args[0]
            if option == '-listallhardwareports':
                return CommandResult('Hardware Port: Wi-Fi\nDevice: en0\n')
            if option == '-listnetworkserviceorder':
                return CommandResult('(1) Home Wi-Fi\n(Hardware Port: Wi-Fi, Device: en0)\n')
            if option == '-getairportnetwork':
                return CommandResult('Current Wi-Fi Network: Test-only')
            if option == '-getdnsservers':
                return CommandResult('\n'.join(self.manual_dns) if self.manual_dns else "There aren't any DNS Servers set on Wi-Fi.")
            if option == '-getinfo':
                return CommandResult('IPv6: ' + self.ipv6)
            getters = {'-getwebproxy': 'http', '-getsecurewebproxy': 'https', '-getsocksfirewallproxy': 'socks'}
            if option in getters:
                proxy = self.proxies.get(getters[option], {'enabled': False})
                return CommandResult('Enabled: ' + ('Yes' if proxy['enabled'] else 'No')
                                     + '\nServer: ' + proxy.get('host', '') + '\nPort: ' + str(proxy.get('port', 0)))
            if option.startswith('-set'):
                self.mutations.append(tuple(argv))
                if option == '-setdnsservers':
                    values = [] if args[2:] == ['Empty'] else args[2:]
                    applying = values == ['10.0.0.66', '10.0.0.68']
                    if not applying and self.fail_rollback:
                        return CommandResult(stderr='rollback denied', returncode=1)
                    self.manual_dns = list(values)
                    self.has_applied = applying
                    if applying and self.fail_apply:
                        return CommandResult(stderr='partial command failure', returncode=1)
                elif option == '-setv6off':
                    self.ipv6 = 'Off'
                elif option == '-setv6automatic':
                    self.ipv6 = 'Automatic'
                elif option == '-setv6linklocal':
                    self.ipv6 = 'Link-local only'
                else:
                    setters = {'-setwebproxystate': 'http', '-setsecurewebproxystate': 'https', '-setsocksfirewallproxystate': 'socks'}
                    self.proxies[setters[option]]['enabled'] = args[2] == 'on'
                return CommandResult()
        raise AssertionError('Unexpected command: ' + repr(argv))

    @staticmethod
    def assert_argv(argv):
        assert isinstance(argv, list) and all(isinstance(arg, str) for arg in argv), argv
        assert Path(argv[0]).name not in ('sh', 'bash', 'zsh', 'sudo'), argv


def config(mac, company=False):
    c = ConfigManager('/not-used', runner=mac)
    c.config = copy.deepcopy(DEFAULT_CONFIG)
    c.set('wifi.interface', 'en0')
    c.set('wifi.service_name', 'Wi-Fi')
    if company:
        c.set('general.preset', 'company')
        c.set('vpn.company_dns', ['10.0.0.66', '10.0.0.68'])
        c.set('dns.public_dns', ['8.8.8.8'])
        c.set('dns.enforce_company_dns_on_vpn', True)
        c.config['vpn']['known_clients'][0]['service_id'] = 'company-service'
        mac.tunnels = {'utun8': '10.254.254.88'}
        mac.processes = ['UniVPN']
        mac.anchor_routes = {'10.0.0.66': 'utun8', '10.0.0.68': 'utun8'}
        mac.services = {'company-service': 'utun8'}
        mac.manual_dns = ['8.8.8.8']
    return c


class DiagnosticsTests(unittest.TestCase):
    def test_new_install_is_neutral_even_when_checks_enabled(self):
        mac = FakeMac()
        with tempfile.TemporaryDirectory() as tmp:
            c = ConfigManager(Path(tmp) / 'config.json', runner=mac)
            c.load()
            self.assertEqual(c.get('general.preset'), 'observe')
            self.assertEqual(c.get('vpn.company_dns'), [])
            self.assertFalse(c.get('dns.enforce_company_dns_on_vpn'))
            self.assertEqual(c.get('ipv6.should_be'), 'observe')
            self.assertEqual(c.get('wifi.service_name'), 'Home Wi-Fi')
            self.assertEqual(mac.mutations, [])

    def test_existing_explicit_company_config_is_preserved(self):
        mac = FakeMac()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            original = {'general': {'preset': 'company'}, 'vpn': {'company_dns': ['10.0.0.66']},
                        'dns': {'enforce_company_dns_on_vpn': True}, 'ipv6': {'should_be': 'off'}}
            path.write_text(json.dumps(original))
            c = ConfigManager(path, runner=mac)
            self.assertTrue(c.load())
            c.auto_detect()
            self.assertEqual(c.get('vpn.company_dns'), ['10.0.0.66'])
            self.assertTrue(c.get('dns.enforce_company_dns_on_vpn'))
            self.assertEqual(c.get('ipv6.should_be'), 'off')
            self.assertEqual(json.loads(path.read_text()), original)

    def test_invalid_config_is_preserved_and_not_healthy(self):
        mac = FakeMac()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text('{broken')
            c = ConfigManager(path, runner=mac)
            self.assertFalse(c.load())
            engine = DetectionEngine(c, runner=mac)
            engine.run_all()
            self.assertEqual(engine.get_overall_status(), 'unknown')
            self.assertEqual(path.read_text(), '{broken')

    def test_dynamic_route_uses_utun8_and_ignores_legacy_utun4(self):
        mac = FakeMac(); c = config(mac, company=True)
        c.config['vpn']['known_clients'][0]['interface'] = 'utun4'
        state = {}; issues = VpnCheck(c, mac).check(state)
        self.assertEqual(state['vpn'], 'ok')
        self.assertEqual(state['vpn_evidence']['interface'], 'utun8')
        self.assertEqual(state['vpn_client'], 'UniVPN')
        self.assertEqual(issues, [])

    def test_route_and_process_without_ownership_do_not_claim_univpn(self):
        mac = FakeMac(); c = config(mac, company=True)
        del c.config['vpn']['known_clients'][0]['service_id']
        mac.tunnels['utun12'] = '10.99.0.2'
        state = {}; VpnCheck(c, mac).check(state)
        self.assertEqual(state['vpn_path'], 'ok')
        self.assertEqual(state['vpn'], 'unknown')
        self.assertIsNone(state['vpn_client'])
        self.assertFalse(state['vpn_evidence']['owner_confirmed'])

    def test_conflicting_routes_cannot_confirm_vpn(self):
        mac = FakeMac(); c = config(mac, company=True)
        mac.tunnels['utun12'] = '10.99.0.2'
        mac.anchor_routes['10.0.0.68'] = 'utun12'
        state = {}; VpnCheck(c, mac).check(state)
        self.assertEqual(state['vpn'], 'unknown')
        self.assertIsNone(state['vpn_evidence']['interface'])

    def test_ipv6_tunnel_with_route_can_be_verified(self):
        mac = FakeMac(); c = config(mac, company=True)
        mac.tunnels = {'utun8': 'fd00:abcd::2'}
        state = {}; VpnCheck(c, mac).check(state)
        self.assertEqual(state['vpn'], 'ok')
        self.assertEqual(state['vpn_ip'], 'fd00:abcd::2')

    def test_route_into_unverified_tunnel_is_not_proof_vpn_is_off(self):
        mac = FakeMac(); c = config(mac, company=True)
        mac.processes = []; mac.tunnels = {}
        state = {}; VpnCheck(c, mac).check(state)
        self.assertEqual(state['vpn'], 'unknown')
        self.assertEqual(state['vpn_path'], 'unknown')

    def test_explicit_company_policy_survives_redetection_with_auto_preset(self):
        mac = FakeMac(); c = config(mac, company=True)
        c.set('general.preset', 'auto')
        c.auto_detect()
        self.assertTrue(c.get('dns.enforce_company_dns_on_vpn'))
        self.assertEqual(c.get('vpn.company_dns'), ['10.0.0.66', '10.0.0.68'])

    def test_dhcp_dns_is_not_empty_dns_failure(self):
        mac = FakeMac(); c = config(mac)
        state = {}; self.assertEqual(DnsCheck(c, mac).check(state), [])
        self.assertEqual(state['dns'], 'ok')
        self.assertEqual(state['dns_mode'], 'automatic')

    def test_remote_proxy_without_known_app_is_not_residue(self):
        mac = FakeMac(); c = config(mac)
        mac.proxies = {'https': {'host': 'proxy.example.test', 'port': 8443, 'enabled': True}}
        state = {'proxy_app': 'off'}
        self.assertEqual(SystemProxyCheck(c, mac).check(state), [])
        self.assertEqual(state['system_proxy'], 'ok')
        self.assertEqual(state['proxy_failed_types'], [])

    def test_plugin_exception_is_unknown_not_healthy(self):
        class Broken:
            name = 'dns'
            def check(self, state):
                state['dns'] = 'ok'
                raise RuntimeError('simulated parser failure')
        mac = FakeMac(); c = config(mac)
        engine = DetectionEngine(c, runner=mac, checks=[Broken()])
        engine.run_all()
        self.assertEqual(engine.status['dns'], 'unknown')
        self.assertEqual(engine.get_overall_status(), 'unknown')
        self.assertIn('dns', engine.snapshot()['check_errors'])

    def test_failed_mandatory_command_is_unknown(self):
        mac = FakeMac(); c = config(mac)
        def failing(argv, timeout=10):
            if argv == ['/usr/sbin/scutil', '--dns']:
                return CommandResult(stderr='permission denied', returncode=1)
            return mac(argv, timeout)
        engine = DetectionEngine(c, runner=failing)
        engine.run_all()
        self.assertEqual(engine.status['dns'], 'unknown')
        self.assertEqual(engine.get_overall_status(), 'unknown')

    def test_empty_dns_command_output_is_unknown_not_assumed_dhcp(self):
        mac = FakeMac(); c = config(mac)
        def empty(argv, timeout=10):
            if '-getdnsservers' in argv:
                return CommandResult('')
            return mac(argv, timeout)
        engine = DetectionEngine(c, runner=empty)
        engine.run_all()
        self.assertEqual(engine.status['dns'], 'unknown')
        self.assertEqual(engine.get_overall_status(), 'unknown')

    def test_401_is_reachable_with_auth_state_and_403_is_not_service_success(self):
        mac = FakeMac(); c = config(mac)
        engine = DetectionEngine(c, runner=mac)
        mac.http_status = '401'; engine.run_all()
        self.assertEqual(engine.status['reachability_results']['openai']['service'], 'auth_required')
        self.assertEqual(engine.status['reachability_results']['openai']['transport'], 'ok')
        mac.http_status = '403'; engine.run_all()
        self.assertEqual(engine.status['openai'], 'warning')
        self.assertIn('service_openai', {i[1] for i in engine.issues})

    def test_atomic_snapshot_and_serial_runs(self):
        entered, release = threading.Event(), threading.Event()
        class Blocking:
            name = 'dns'
            count = 0
            def check(self, state):
                self.count += 1
                state['dns'] = f'round-{self.count}'
                if self.count == 2:
                    entered.set(); release.wait(2)
                return []
        check = Blocking(); mac = FakeMac()
        engine = DetectionEngine(config(mac), runner=mac, checks=[check])
        engine.run_all()
        worker = threading.Thread(target=engine.run_all); worker.start()
        self.assertTrue(entered.wait(1))
        self.assertEqual(engine.snapshot()['status']['dns'], 'round-1')
        release.set(); worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(engine.status['dns'], 'round-2')

    def test_reload_preserves_dependency_order(self):
        mac = FakeMac(); c = config(mac)
        engine = DetectionEngine(c, runner=mac)
        engine.reload_checks()
        self.assertLess([c.name for c in engine.checks].index('vpn'), [c.name for c in engine.checks].index('dns'))


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.mac = FakeMac(); self.config = config(self.mac, company=True)
        self.engine = DetectionEngine(self.config, runner=self.mac)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fix = FixEngine(self.config, self.engine, snapshot_dir=self.temp.name)
        self.engine.run_all()

    def test_success_requires_fresh_diagnosis_readback_and_network(self):
        result = self.fix.fix_all()
        self.assertEqual(self.mac.manual_dns, ['10.0.0.66', '10.0.0.68'])
        self.assertTrue(all('已验证' in line for line in result), result)
        records = list(Path(self.temp.name).glob('*.json'))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text())
        self.assertEqual(record['before'], {'dns': ['8.8.8.8']})
        self.assertEqual(record['phase'], 'verified')
        self.assertEqual(records[0].stat().st_mode & 0o777, 0o600)

    def test_arbitrary_shell_template_is_ignored(self):
        self.config.set('fix_rules.dns_mixed_on_vpn.command', 'touch /tmp/NEVER; networksetup -setdnsservers Wi-Fi 1.1.1.1')
        self.fix.fix_all()
        self.assertEqual(self.mac.manual_dns, ['10.0.0.66', '10.0.0.68'])
        self.assertFalse(any('touch' in str(call) or '/tmp/NEVER' in str(call) for call in self.mac.calls))

    def test_unknown_vpn_never_switches_dns(self):
        del self.config.config['vpn']['known_clients'][0]['service_id']
        self.engine.run_all()
        result = self.fix.fix_all([('medium', 'dns_company_leftover', 'old false positive')])
        self.assertEqual(self.mac.mutations, [])
        self.assertIn('没有可安全', result[0])

    def test_failed_action_never_reports_success_and_rolls_back(self):
        self.mac.fail_apply = True
        result = self.fix.fix_all()
        self.assertEqual(self.mac.manual_dns, ['8.8.8.8'])
        self.assertTrue(any('修复失败' in line for line in result), result)
        self.assertTrue(any('已验证回滚' in line for line in result), result)
        self.assertFalse(any('✅' in line for line in result), result)

    def test_new_connectivity_failure_rolls_back_even_if_dns_issue_disappears(self):
        self.mac.break_network_after_apply = True
        result = self.fix.fix_all()
        self.assertEqual(self.mac.manual_dns, ['8.8.8.8'])
        self.assertTrue(any('路径退化' in line for line in result), result)
        self.assertTrue(any('回滚' in line for line in result), result)

    def test_verification_exception_is_failure_and_rolls_back(self):
        self.mac.fail_dns_read_after_apply = True
        result = self.fix.fix_all()
        self.assertEqual(self.mac.manual_dns, ['8.8.8.8'])
        self.assertTrue(any('诊断未完成' in line for line in result), result)
        self.assertFalse(any('✅' in line for line in result), result)

    def test_rollback_failure_is_reported_separately_with_snapshot(self):
        self.mac.fail_apply = True; self.mac.fail_rollback = True
        result = self.fix.fix_all()
        self.assertTrue(any('回滚失败' in line for line in result), result)
        self.assertTrue(any('恢复快照' in line for line in result), result)
        self.assertFalse(any('已验证回滚' in line for line in result), result)
        record = json.loads(next(Path(self.temp.name).glob('*.json')).read_text())
        self.assertEqual(record['phase'], 'rollback_failed')

    def test_preflight_state_change_does_not_mutate(self):
        self.mac.services = {}
        result = self.fix.fix_all()
        self.assertEqual(self.mac.mutations, [])
        self.assertTrue(any('未执行' in line for line in result), result)

    def test_external_change_after_snapshot_is_not_overwritten_or_called_rollback(self):
        persist = self.fix._persist
        def external_change(record, path=None):
            saved = persist(record, path)
            if record['phase'] == 'prepared':
                self.mac.manual_dns = ['9.9.9.9']
            return saved
        self.fix._persist = external_change
        result = self.fix.fix_all()
        self.assertEqual(self.mac.mutations, [])
        self.assertEqual(self.mac.manual_dns, ['9.9.9.9'])
        self.assertTrue(any('未执行' in line for line in result), result)
        self.assertFalse(any('回滚' in line for line in result), result)

    def test_missing_probe_baseline_blocks_mutation(self):
        self.config.set('reachability.enabled', False)
        self.engine.reload_checks(); self.engine.run_all()
        result = self.fix.fix_all()
        self.assertEqual(self.mac.mutations, [])
        self.assertTrue(any('基线' in line for line in result), result)

    def test_pac_path_cannot_be_replaced_with_direct_repair_baseline(self):
        self.mac.pac = True
        self.engine.run_all()
        result = self.fix.fix_all()
        self.assertEqual(self.mac.mutations, [])
        self.assertTrue(any('PAC/WPAD' in line for line in result), result)

    def test_manual_ipv6_is_not_changed_without_complete_restore_information(self):
        self.mac.manual_dns = ['10.0.0.66', '10.0.0.68']
        self.mac.ipv6 = 'Manual'
        self.config.set('ipv6.should_be', 'off')
        self.engine.run_all()
        result = self.fix.fix_all()
        self.assertEqual(self.mac.mutations, [])
        self.assertTrue(any('IPv6' in line for line in result), result)

    def test_local_proxy_fix_only_disables_failed_proxy_kind(self):
        self.mac.manual_dns = ['10.0.0.66', '10.0.0.68']
        self.mac.proxy_reachable = False
        self.mac.proxies = {'http': {'enabled': True, 'host': '127.0.0.1', 'port': 7890}}
        self.engine.run_all()
        result = self.fix.fix_all()
        self.assertFalse(self.mac.proxies['http']['enabled'])
        self.assertEqual(len(self.mac.mutations), 1)
        self.assertEqual(self.mac.mutations[0][1], '-setwebproxystate')
        self.assertTrue(any('已验证' in line for line in result), result)


    def install_runner(self, runner):
        self.fix.runner = runner
        self.engine.runner = runner
        for check in self.engine.checks:
            check.runner = runner

    def test_vpn_ownership_change_after_prepare_blocks_dns_write(self):
        persist = self.fix._persist
        def changed(record, path=None):
            saved = persist(record, path)
            if record['phase'] == 'prepared':
                self.mac.services = {}
            return saved
        self.fix._persist = changed
        result = self.fix.fix_all()
        self.assertEqual(self.mac.mutations, [])
        self.assertEqual(self.mac.manual_dns, ['8.8.8.8'])
        self.assertTrue(any('VPN' in line and '未执行' in line for line in result), result)

    def test_vpn_ownership_lost_after_dns_write_rolls_back_not_success(self):
        def changed(argv, timeout=10):
            result = self.mac(argv, timeout)
            if '-setdnsservers' in argv and self.mac.has_applied:
                self.mac.services = {}
            return result
        self.install_runner(changed)
        result = self.fix.fix_all()
        self.assertEqual(self.mac.manual_dns, ['8.8.8.8'])
        self.assertFalse(any('✅' in line for line in result), result)
        self.assertTrue(any('VPN' in line for line in result), result)
        self.assertTrue(any('已验证回滚' in line for line in result), result)

    def test_vpn_interface_change_still_connected_is_not_original_approval(self):
        def changed(argv, timeout=10):
            result = self.mac(argv, timeout)
            if '-setdnsservers' in argv and self.mac.has_applied:
                self.mac.tunnels = {'utun12': '10.254.254.99'}
                self.mac.anchor_routes = dict.fromkeys(self.mac.anchor_routes, 'utun12')
                self.mac.services = {'company-service': 'utun12'}
            return result
        self.install_runner(changed)
        result = self.fix.fix_all()
        self.assertEqual(self.mac.manual_dns, ['8.8.8.8'])
        self.assertEqual(self.engine.status['vpn'], 'ok')
        self.assertEqual(self.engine.status['vpn_evidence']['interface'], 'utun12')
        self.assertFalse(any('✅' in line for line in result), result)

    def test_final_vpn_check_detects_change_during_slow_network_verification(self):
        def changed(argv, timeout=10):
            result = self.mac(argv, timeout)
            if Path(argv[0]).name == 'curl' and self.mac.has_applied:
                self.mac.services = {}
            return result
        self.install_runner(changed)
        result = self.fix.fix_all()
        self.assertEqual(self.mac.manual_dns, ['8.8.8.8'])
        self.assertFalse(any('✅' in line for line in result), result)
        self.assertTrue(any('VPN' in line for line in result), result)

    def test_public_dns_repair_cancelled_if_vpn_connects_before_write(self):
        self.mac.manual_dns = ['10.0.0.66', '10.0.0.68']
        self.mac.tunnels = {}; self.mac.services = {}; self.mac.anchor_routes = {}; self.mac.processes = []
        self.engine.run_all()
        persist = self.fix._persist
        def changed(record, path=None):
            saved = persist(record, path)
            if record['phase'] == 'prepared':
                self.mac.tunnels = {'utun8': '10.254.254.88'}
                self.mac.services = {'company-service': 'utun8'}
                self.mac.anchor_routes = {'10.0.0.66': 'utun8', '10.0.0.68': 'utun8'}
            return saved
        self.fix._persist = changed
        result = self.fix.fix_all()
        self.assertEqual(self.mac.mutations, [])
        self.assertEqual(self.mac.manual_dns, ['10.0.0.66', '10.0.0.68'])
        self.assertTrue(any('未执行' in line for line in result), result)

    def test_external_dns_change_after_write_is_preserved_during_rollback(self):
        def changed(argv, timeout=10):
            result = self.mac(argv, timeout)
            if '-setdnsservers' in argv and self.mac.has_applied:
                self.mac.manual_dns = ['9.9.9.9']
            return result
        self.install_runner(changed)
        result = self.fix.fix_all()
        self.assertEqual(self.mac.manual_dns, ['9.9.9.9'])
        self.assertEqual(len(self.mac.mutations), 1)
        self.assertTrue(any('未覆盖' in line for line in result), result)
        self.assertTrue(any('恢复快照' in line for line in result), result)
        self.assertFalse(any('已验证回滚' in line or '✅' in line for line in result), result)

    def test_different_service_proxy_endpoint_is_never_modified(self):
        self.mac.manual_dns = ['10.0.0.66', '10.0.0.68']
        self.mac.proxies = {'http': {'enabled': True, 'host': 'healthy.example.test', 'port': 8080}}
        def different_service(argv, timeout=10):
            if argv == ['/usr/sbin/scutil', '--proxy']:
                return CommandResult('<dictionary> {\n HTTPEnable : 1\n HTTPProxy : 127.0.0.1\n HTTPPort : 7890\n}')
            if Path(argv[0]).name == 'nc':
                return CommandResult(returncode=1)
            return self.mac(argv, timeout)
        self.install_runner(different_service)
        self.engine.run_all()
        result = self.fix.fix_all()
        self.assertEqual(self.mac.mutations, [])
        self.assertTrue(self.mac.proxies['http']['enabled'])
        self.assertTrue(any('代理端点' in line for line in result), result)

    def test_proxy_recovery_before_write_cancels_mutation(self):
        self.mac.manual_dns = ['10.0.0.66', '10.0.0.68']
        self.mac.proxies = {'http': {'enabled': True, 'host': '127.0.0.1', 'port': 7890}}
        self.mac.proxy_reachable = False
        self.engine.run_all()
        persist = self.fix._persist
        def recovered(record, path=None):
            saved = persist(record, path)
            if record['phase'] == 'prepared':
                self.mac.proxy_reachable = True
            return saved
        self.fix._persist = recovered
        result = self.fix.fix_all()
        self.assertEqual(self.mac.mutations, [])
        self.assertTrue(self.mac.proxies['http']['enabled'])
        self.assertTrue(any('代理端点已恢复' in line for line in result), result)

    def test_external_proxy_endpoint_change_does_not_reenable_new_endpoint(self):
        self.mac.manual_dns = ['10.0.0.66', '10.0.0.68']
        self.mac.proxies = {'http': {'enabled': True, 'host': '127.0.0.1', 'port': 7890}}
        self.mac.proxy_reachable = False
        def changed(argv, timeout=10):
            result = self.mac(argv, timeout)
            if '-setwebproxystate' in argv:
                self.mac.proxies['http'].update(host='new.example.test', port=8080)
            return result
        self.install_runner(changed)
        self.engine.run_all()
        result = self.fix.fix_all()
        self.assertEqual(len(self.mac.mutations), 1)
        self.assertEqual(self.mac.proxies['http']['host'], 'new.example.test')
        self.assertFalse(self.mac.proxies['http']['enabled'])
        self.assertTrue(any('未覆盖' in line for line in result), result)
        self.assertFalse(any('已验证回滚' in line or '✅' in line for line in result), result)

    def test_nonprivate_snapshot_directory_blocks_all_mutations(self):
        Path(self.temp.name).chmod(0o755)
        result = self.fix.fix_all()
        self.assertEqual(self.mac.mutations, [])
        self.assertTrue(any('0700' in line for line in result), result)
        self.assertFalse(list(Path(self.temp.name).glob('*.json')))

    def test_snapshot_write_does_not_follow_predictable_temporary_symlink(self):
        target = Path(self.temp.name) / 'sentinel.txt'
        target.write_text('unchanged')
        path = Path(self.temp.name) / 'repair-test.json'
        path.with_suffix('.tmp').symlink_to(target)
        self.fix._persist({'before': {'dns': ['10.0.0.66']}}, path)
        self.assertEqual(target.read_text(), 'unchanged')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.chmod(0o644)
        self.fix._persist({'phase': 'verified'}, path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.read_text(), 'unchanged')
        self.assertFalse(list(Path(self.temp.name).glob('.relay-repair-*.tmp')))

    def test_failed_snapshot_write_prevents_system_mutation(self):
        with patch.object(self.fix, '_persist', side_effect=OSError('disk unavailable')):
            result = self.fix.fix_all()
        self.assertEqual(self.mac.mutations, [])
        self.assertTrue(any('准备失败' in line for line in result), result)

    def test_detection_cannot_interleave_with_repair_transaction(self):
        entered, release, detected = threading.Event(), threading.Event(), threading.Event()
        result = []
        def blocking(argv, timeout=10):
            if '-setdnsservers' in argv and argv[-2:] == ['10.0.0.66', '10.0.0.68']:
                entered.set(); release.wait(2)
            return self.mac(argv, timeout)
        self.install_runner(blocking)
        repair = threading.Thread(target=lambda: result.extend(self.fix.fix_all()))
        repair.start()
        self.assertTrue(entered.wait(1))
        checker = threading.Thread(target=lambda: (self.engine.run_all(), detected.set()))
        checker.start()
        try:
            self.assertFalse(detected.wait(0.05))
        finally:
            release.set(); repair.join(3); checker.join(3)
        self.assertFalse(repair.is_alive())
        self.assertFalse(checker.is_alive())
        self.assertTrue(any('✅' in line for line in result), result)


class CommandBoundaryTests(unittest.TestCase):
    def test_shell_disabled_and_exit_status_preserved(self):
        completed = subprocess.CompletedProcess(['dummy'], 7, 'out', 'err')
        with patch('relay.commands.subprocess.run', return_value=completed) as run:
            result = run_command(['/fake/program', 'value; $(not-code)'])
        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stderr, 'err')
        self.assertFalse(run.call_args.kwargs['shell'])
        self.assertEqual(run.call_args.args[0], ['/fake/program', 'value; $(not-code)'])

    def test_timeout_is_not_empty_success(self):
        with patch('relay.commands.subprocess.run', side_effect=subprocess.TimeoutExpired(['fake'], 1)):
            result = run_command(['/fake/program'])
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)


if __name__ == '__main__':
    unittest.main()
