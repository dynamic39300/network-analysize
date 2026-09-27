"""Windows contracts tested with structured evidence, never with real network writes."""
import base64
import copy
import ctypes
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path, PureWindowsPath
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code'))
from relay.agent_store import AgentStore
from relay.commands import CommandResult
from relay.core import CoreRuntime, public_report
from relay.guard import GuardPolicy, ProbeBudget
from relay.platforms.windows import SECTIONS, WindowsObserver, WindowsProxyReader, parse_inventory, powershell_argv, read_json
from relay.platforms.windows_engine import manual_proxy_endpoints
from relay.platforms.windows_events import IPHelperSubscriptions, WindowsNetworkEvents, _LIVE_SUBSCRIPTIONS
from relay.private_files import WindowsPrivateFiles
from relay.profiles import SCHEMA
from relay_config import ConfigManager, DEFAULT_CONFIG
from test_guard import Clock

SID = 'S-1-5-21-100-200-300-1001'
SCOPE = {'user_sid': SID, 'session_id': 2}
ETHERNET = '11111111-2222-3333-4444-555555555555'
TUNNEL = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
SYSTEM = Path('/test-system32')


def inventory():
    sections = {key: {'state': 'ok', 'items': []} for key in SECTIONS}
    sections['interfaces']['items'] = [
        {'id': '{' + ETHERNET + '}', 'name': 'Private office', 'type': 'Ethernet', 'state': 'Up', 'ipv4_index': 7, 'ipv6_index': 7},
        {'id': TUNNEL, 'name': 'Vendor VPN', 'type': 'Ppp', 'state': 'Up', 'ipv4_index': 12, 'ipv6_index': None}]
    sections['addresses']['items'] = [
        {'index': 7, 'family': 'IPv4', 'address': '192.0.2.10', 'prefix_length': 24, 'state': 'Preferred', 'origin': 'Dhcp'},
        {'index': 7, 'family': 'IPv6', 'address': '2001:db8::10', 'prefix_length': 64, 'state': 'Preferred', 'origin': 'RouterAdvertisement'},
        {'index': 12, 'family': 'IPv4', 'address': '10.12.0.20', 'prefix_length': 32, 'state': 'Preferred', 'origin': 'Manual'}]
    sections['ip_interfaces']['items'] = [
        {'index': 7, 'family': 'IPv4', 'metric': 25, 'dhcp': 'Enabled', 'state': 'Connected'},
        {'index': 12, 'family': 'IPv4', 'metric': 1, 'dhcp': 'Disabled', 'state': 'Connected'}]
    sections['routes']['items'] = [
        {'index': 7, 'family': 'IPv4', 'destination': '0.0.0.0/0', 'next_hop': '192.0.2.1', 'metric': 0, 'protocol': 'Dhcp'},
        {'index': 7, 'family': 'IPv6', 'destination': '::/0', 'next_hop': 'fe80::1', 'metric': 0, 'protocol': 'RouterAdvertisement'},
        {'index': 12, 'family': 'IPv4', 'destination': '10.12.0.0/16', 'next_hop': '0.0.0.0', 'metric': 1, 'protocol': 'NetMgmt'}]
    sections['dns']['items'] = [
        {'index': 7, 'family': 2, 'servers': ['192.0.2.53']},
        {'index': 7, 'family': 23, 'servers': ['2001:db8::53']},
        {'index': 12, 'family': 2, 'servers': ['10.12.0.53']}]
    sections['dns_clients']['items'] = [{'index': 7, 'suffix': 'private.example.test'}, {'index': 12, 'suffix': ''}]
    sections['nrpt']['items'] = [{'namespaces': ['.private.example.test'], 'servers': ['10.12.0.53'], 'dnssec_required': True}]
    sections['vpn_user']['items'] = [{'id': TUNNEL, 'name': 'Private VPN', 'state': 'Connected', 'split_tunnel': True, 'tunnel_type': 'Ikev2'}]
    return {'schema': 'relay-windows-inventory-v1', 'captured_at': datetime.now(timezone.utc).isoformat(),
            'scope': {**SCOPE, 'interactive': True, 'elevated': False}, 'sections': sections}


class FakeWindows:
    def __init__(self):
        self.data = inventory()
        self.proxies = [
            {'scope': 'process_user_wininet', 'state': 'observed', 'proxy': '', 'pac_url': '', 'auto_detect': False, 'bypass': ''},
            {'scope': 'winhttp_static_default', 'state': 'observed', 'proxy': 'private-machine-proxy:8888', 'bypass': '', 'access_type': 3}]
        self.calls = []
        self.status, self.exit = '200', 0
        self.observer = WindowsObserver(self, self, SYSTEM, expected_scope=SCOPE)

    def __call__(self, argv, timeout=10):
        self.calls.append((list(argv), timeout))
        name = PureWindowsPath(argv[0]).name.lower()
        if name == 'powershell.exe':
            return CommandResult(json.dumps(self.data))
        if name == 'curl.exe':
            return CommandResult(self.status, returncode=self.exit)
        raise AssertionError('Unexpected platform command: ' + repr(argv))

    def read(self):
        return copy.deepcopy(self.proxies)

    def config(self):
        config = ConfigManager('/unused', runner=self)
        config.config = copy.deepcopy(DEFAULT_CONFIG)
        config.config['reachability']['targets'] = [{'name': 'Private service', 'url': 'https://private.example.test', 'timeout': 5}]
        return config

    @property
    def requests(self):
        return [args for args, _timeout in self.calls if PureWindowsPath(args[0]).name.lower() == 'curl.exe']


class InventoryTests(unittest.TestCase):
    def test_structured_multiple_interfaces_and_scopes_do_not_claim_vpn_ownership(self):
        value = parse_inventory(inventory(), SCOPE)
        self.assertEqual(value.interfaces[0]['id'], ETHERNET)
        self.assertEqual({a['family'] for a in value.interfaces[0]['addresses']}, {'IPv4', 'IPv6'})
        self.assertEqual(value.interfaces[1]['ip_configuration'][0]['metric'], 1)
        self.assertEqual(value.routes[-1]['interface_id'], TUNNEL)
        self.assertEqual(value.resolvers[-1]['scope'], 'namespace')
        self.assertEqual(value.resolvers[-1]['ownership'], 'policy_source_unknown')
        self.assertFalse(value.vpns[0]['owner_confirmed'])
        self.assertIsNone(value.vpns[0]['interface_id'])
        self.assertEqual(value.capabilities['target_routes'], 'not_implemented')
        self.assertEqual(value.scope['network_compartment'], 'process_default')

    def test_interface_identity_survives_rename_and_index_reassignment(self):
        before = parse_inventory(inventory())
        changed = inventory()
        changed['sections']['interfaces']['items'][0].update(name='Renamed', ipv4_index=90, ipv6_index=90)
        for key in ('addresses', 'ip_interfaces', 'routes', 'dns', 'dns_clients'):
            for row in changed['sections'][key]['items']:
                if row['index'] == 7:
                    row['index'] = 90
        after = parse_inventory(changed)
        self.assertEqual(before.interfaces[0]['id'], after.interfaces[0]['id'])
        self.assertEqual(after.routes[0]['interface_id'], ETHERNET)
        self.assertEqual(after.resolvers[0]['interface_index'], 90)

    def test_absent_section_is_unknown_not_a_confirmed_empty_list(self):
        data = inventory()
        data['sections'].pop('nrpt')
        data['sections']['vpn_machine'] = {'state': 'unknown', 'items': [], 'error': 'sensitive text'}
        parsed = parse_inventory(data)
        self.assertEqual(parsed.capabilities['nrpt'], 'unknown')
        self.assertIn('vpn_machine', parsed.errors)
        self.assertNotIn('sensitive text', json.dumps(parsed.to_dict()))

    def test_dnssec_only_nrpt_rule_has_no_server_but_retains_namespace_policy(self):
        data = inventory()
        data['sections']['nrpt']['items'][0]['servers'] = []
        data['sections']['dns']['items'][1]['servers'] = []
        result = parse_inventory(data)
        self.assertEqual(result.resolvers[-1]['servers'], [])
        self.assertTrue(result.resolvers[-1]['dnssec_required'])
        self.assertEqual(result.resolvers[-1]['ownership'], 'policy_source_unknown')
        self.assertFalse(result.errors)
        data['sections']['nrpt']['items'][0]['namespaces'] = []
        with self.assertRaises(ValueError):
            parse_inventory(data)

    def test_unknown_address_interface_is_not_attributed_to_another_interface(self):
        data = inventory()
        data['sections']['addresses']['items'][0]['index'] = 99
        parsed = parse_inventory(data)
        self.assertIn('interface_mapping', parsed.errors)
        self.assertEqual(len(parsed.interfaces[0]['addresses']), 1)

    def test_unmapped_route_or_resolver_is_incomplete_not_silently_global(self):
        for section in ('routes', 'dns', 'ip_interfaces'):
            data = inventory()
            data['sections'][section]['items'][0]['index'] = 99
            parsed = parse_inventory(data)
            self.assertIn('interface_mapping', parsed.errors)

    def test_invalid_or_stale_identity_fails_closed(self):
        variants = []
        for key, value in (('user_sid', ''), ('session_id', True), ('interactive', 'true'), ('elevated', 0)):
            data = inventory()
            data['scope'][key] = value
            variants.append(data)
        for value in ((datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat(), datetime.now().isoformat()):
            data = inventory()
            data['captured_at'] = value
            variants.append(data)
        for data in variants:
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_inventory(data)
        for scope in ({**SCOPE, 'session_id': 3}, {**SCOPE, 'user_sid': 'S-1-5-18'}):
            with self.assertRaises(ValueError):
                parse_inventory(inventory(), scope)

    def test_duplicate_identity_and_bad_numeric_or_family_fields_are_rejected(self):
        cases = [('interfaces', 'id', ETHERNET, 1), ('interfaces', 'ipv4_index', 7, 1),
                 ('addresses', 'prefix_length', 33, 0), ('routes', 'metric', True, 0),
                 ('dns', 'servers', ['2001:db8::53'], 0), ('routes', 'family', 'IPv6', 0)]
        for section, key, value, index in cases:
            data = inventory()
            data['sections'][section]['items'][index][key] = value
            with self.subTest(section=section, key=key), self.assertRaises(ValueError):
                parse_inventory(data)

    def test_json_limits_duplicate_fields_and_bom(self):
        self.assertEqual(read_json('\ufeff{"ok": 1}'), {'ok': 1})
        for value in ('{"x": 1, "x": 2}', ' ' * (2 * 1024 * 1024 + 1)):
            with self.assertRaises(ValueError):
                read_json(value)

    def test_powershell_is_fixed_encoded_script_without_policy_bypass(self):
        windows = FakeWindows()
        windows.observer.observe()
        args = windows.calls[0][0]
        script = base64.b64decode(args[-1]).decode('utf-16-le')
        self.assertEqual(args, powershell_argv(script, SYSTEM))
        self.assertNotIn('-ExecutionPolicy', args)
        self.assertNotIn('private.example.test', script)
        self.assertNotIn('Set-Net', script)
        self.assertIn('Get-DnsClientNrptPolicy -Effective', script)
        self.assertIn('-NoProfile', args)

    def test_proxy_scopes_are_required_and_read_failure_remains_unknown(self):
        windows = FakeWindows()
        for proxies in ([], [windows.proxies[0]], [windows.proxies[0]] * 2, [None, None]):
            with self.subTest(proxies=proxies):
                windows.proxies = proxies
                observed = windows.observer.observe()
                self.assertEqual(observed.capabilities['proxy_configuration'], 'unknown')
                self.assertIn('proxy_configuration', observed.errors)
        windows = FakeWindows()
        windows.proxies[1]['state'] = 'unknown'
        observed = windows.observer.observe()
        self.assertEqual(observed.capabilities['proxy_configuration'], 'partial')
        self.assertIn('proxy_configuration', observed.errors)

    def test_manual_proxy_formats_are_explicit_and_never_credentials_or_socks_guesses(self):
        self.assertEqual(manual_proxy_endpoints('host.test:8080')['https']['port'], 8080)
        self.assertEqual(manual_proxy_endpoints('http=host.test:80;https=[::1]:8443')['https']['host'], '::1')
        for value in ('host.test', 'host:0', 'host:99999', 'https=host:8080;https=else:8080',
                      'u:p@host:80', 'host:80/path', 'socks=host:1080', 'host:80;'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                manual_proxy_endpoints(value)

    def test_native_proxy_reader_releases_os_allocations_with_global_free(self):
        allocated, freed = [], []
        def fill_user(pointer):
            for key, text in (('proxy', 'user-proxy:80'), ('bypass', '*.example.test'), ('pac_url', 'http://example.test/proxy.pac')):
                buffer = ctypes.create_unicode_buffer(text)
                allocated.append(buffer)
                setattr(pointer._obj, key, ctypes.addressof(buffer))
            pointer._obj.auto_detect = True
            return 1
        def fill_machine(pointer):
            buffer = ctypes.create_unicode_buffer('machine-proxy:8080')
            allocated.append(buffer)
            pointer._obj.proxy = ctypes.addressof(buffer)
            pointer._obj.access_type = 3
            return 1
        def free(pointer):
            freed.append(pointer)
        winhttp = SimpleNamespace(WinHttpGetIEProxyConfigForCurrentUser=fill_user,
                                 WinHttpGetDefaultProxyConfiguration=fill_machine)
        kernel = SimpleNamespace(GlobalFree=free)
        with patch.object(ctypes, 'WinDLL', create=True, side_effect=lambda name, **_: winhttp if name == 'winhttp' else kernel):
            results = WindowsProxyReader().read()
        self.assertEqual(results[0]['proxy'], 'user-proxy:80')
        self.assertEqual(results[1]['proxy'], 'machine-proxy:8080')
        self.assertEqual(set(freed), {ctypes.addressof(buffer) for buffer in allocated})
        self.assertEqual(len(freed), 4)


class FakeEvents:
    def __init__(self, changed):
        self.changed, self.available = changed, False
        self.started = self.closed = False
    def start(self):
        self.available = self.started = True
    def close(self):
        self.available, self.closed = False, True


class WindowsCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.windows, self.clock, self.reports = FakeWindows(), Clock(), []
        self.core = CoreRuntime(Path(self.temp.name) / 'core', platform='win32', runner=self.windows,
            config=self.windows.config(), observer=self.windows.observer, directory=SYSTEM, scope=SCOPE,
            events_factory=FakeEvents, on_report=self.reports.append, clock=self.clock, guard_threaded=False)
        self.addCleanup(self.core.close)

    def target(self, **options):
        return self.core.profiles.save({'schema': SCHEMA, 'name': 'Private profile', 'targets': [
            {'name': 'Private service', 'url': 'https://private.example.test', **options}]})

    def test_check_uses_actual_windows_commands_and_no_machine_proxy_fallback(self):
        run, report = self.core.check()
        self.assertEqual(run.health, 'healthy')
        self.assertEqual(report['network_writes'], 0)
        self.assertEqual(run.snapshot['environment']['platform'], 'windows')
        self.assertTrue(self.core.profiles.baseline)
        self.assertEqual(len(self.windows.requests), 1)
        args = self.windows.requests[0]
        self.assertEqual(args[1:3], ['--disable', '--globoff'])
        self.assertIn('NUL', args)
        self.assertIn('--noproxy', args)
        self.assertNotIn('--proxy', args)
        self.assertEqual(self.core.agent.repair_options(), {})
        self.assertFalse(run.proposals)
        with self.assertRaises(PermissionError):
            self.core.fix.fix_all()

    def test_system_proxy_is_process_user_scope_and_application_claims_remain_unknown(self):
        self.windows.proxies[0]['proxy'] = 'https=127.0.0.1:8888'
        self.target(expected_path='system_proxy')
        run, _ = self.core.check()
        self.assertEqual(run.health, 'healthy')
        self.assertIn('http://127.0.0.1:8888', self.windows.requests[0])
        self.assertEqual(run.snapshot['environment']['capabilities']['application_paths'], 'unknown')

    def test_pac_bypass_or_unreadable_user_proxy_cannot_be_replaced_by_direct(self):
        for settings in ({'pac_url': 'http://private.example.test/proxy.pac'}, {'auto_detect': True},
                         {'proxy': 'host.test:80', 'bypass': '*.private.example.test'}, {'state': 'unknown'}):
            original = copy.deepcopy(self.windows.proxies[0])
            self.windows.proxies[0].update(settings)
            with self.subTest(settings=settings):
                run, _ = self.core.check()
                self.assertEqual(run.health, 'unknown')
                self.assertFalse(self.windows.requests)
                self.assertIsNone(self.core.profiles.baseline)
            self.windows.proxies[0] = original

    def test_explicit_direct_target_does_not_inherit_user_proxy_or_pac(self):
        self.windows.proxies[0].update(proxy='host.test:8080', auto_detect=True)
        self.target(expected_path='direct')
        run, _ = self.core.check()
        self.assertEqual(run.health, 'healthy')
        self.assertNotIn('--proxy', self.windows.requests[0])

    def test_unverified_vpn_never_falls_back_to_mac_interface_commands(self):
        for options in ({'expected_path': 'vpn'}, {'when': 'vpn_connected'}, {'when': 'vpn_disconnected'}):
            self.target(**options)
            run, _ = self.core.check()
            self.assertEqual(run.health, 'unknown')
            self.assertFalse(self.windows.requests)
            self.assertIsNone(self.core.profiles.baseline)

    def test_service_or_different_session_does_not_probe_for_interactive_user(self):
        self.windows.data['scope'].update(interactive=False)
        run, report = self.core.check()
        self.assertEqual(run.health, 'unknown')
        self.assertEqual(report['scope'], 'service_or_background')
        self.assertIn('user_scope', run.snapshot['check_errors'])
        self.windows.data['scope']['session_id'] = 3
        run, _ = self.core.check()
        self.assertIn('environment', run.snapshot['check_errors'])
        self.assertFalse(self.windows.requests)

    def test_missing_section_cannot_be_healthy_even_when_http_succeeds(self):
        self.windows.data['sections']['nrpt'] = {'state': 'unknown', 'items': []}
        run, report = self.core.check()
        self.assertEqual(run.health, 'unknown')
        self.assertIn('nrpt', report['incomplete_sections'])
        self.assertIsNone(self.core.profiles.baseline)

    def test_unsupported_legacy_expectations_cannot_be_healthy_only_from_http(self):
        for key, value in (('dns.enforce_company_dns_on_vpn', True), ('dns.public_dns', ['192.0.2.53']),
                           ('ipv6.should_be', 'Off')):
            before = self.core.config.get(key)
            self.core.config.set(key, value)
            with self.subTest(key=key):
                run, report = self.core.check()
                self.assertEqual(run.health, 'unknown')
                self.assertIn('policy_validation', report['diagnostic_errors'])
                self.assertIsNone(self.core.profiles.baseline)
            self.core.config.set(key, before)

    def test_service_requirement_keeps_authentication_separate_from_transport(self):
        self.target(requirement='service')
        self.windows.status = '401'
        run, report = self.core.check()
        self.assertEqual(run.health, 'attention')
        self.assertEqual(report['targets'][0]['state'], 'auth_required')
        self.assertEqual(report['targets'][0]['transport'], 'ok')
        self.assertIsNone(self.core.profiles.baseline)

    def test_public_report_omits_identifiers_and_untrusted_enum_text(self):
        run, report = self.core.check()
        encoded = json.dumps(report)
        for private in (SID, ETHERNET, TUNNEL, 'Private', 'private.example.test', '192.0.2', '10.12.', 'machine-proxy'):
            self.assertNotIn(private, encoded)
        run.snapshot['environment']['interfaces'][0].update(type='private.example.test', state='private.example.test')
        self.assertNotIn('private.example.test', json.dumps(public_report(run, self.core.agent.assess(run.snapshot))))
        self.assertIn('private.example.test', json.dumps(self.core.raw_report(run), default=str))

    def test_guard_requires_explicit_start_and_deduplicates_stable_reports(self):
        self.clock.advance(1000)
        self.core.guard.tick()
        self.assertFalse(self.windows.calls)
        self.assertFalse(self.core.events.started)
        self.assertEqual(self.core.start_guard()['event_source'], 'native')
        self.clock.advance(5)
        self.core.guard.tick()
        self.clock.advance(300)
        self.core.guard.tick()
        self.assertEqual(len(self.reports), 1)
        self.assertEqual(self.reports[0]['budget']['commands'], 2)
        self.assertEqual(self.reports[0]['budget']['requests'], 1)
        self.assertIn('--max-filesize', self.windows.requests[0])
        self.assertEqual(self.windows.requests[0][1], '--disable')
        self.windows.exit = 28
        self.clock.advance(300)
        self.core.guard.tick()
        self.assertEqual(len(self.reports), 2)
        self.assertEqual(self.reports[-1]['health'], 'degraded')

    def test_budget_rejects_more_windows_curl_requests_and_prevents_normal_baseline(self):
        definition = self.core.profiles.document(self.core.profiles.active['id'])
        definition['targets'] = [{key: value for key, value in definition['targets'][0].items() if key != 'id'}] * 9
        self.core.profiles.save(definition)
        self.core.start_guard()
        self.clock.advance(5)
        self.core.guard.tick()
        self.assertEqual(len(self.windows.requests), 8)
        self.assertEqual(self.reports[0]['health'], 'unknown')
        self.assertIsNone(self.core.profiles.baseline)
        self.assertEqual(self.reports[0]['budget']['stop_reason'], 'request_budget')

    def test_paused_guard_does_not_issue_another_command(self):
        self.core.start_guard()
        self.clock.advance(5)
        original = self.core.engine.observer.runner
        def stop_after_inventory(argv, timeout=10):
            result = original(argv, timeout)
            self.core.guard.pause()
            return result
        self.core.engine.observer.runner = stop_after_inventory
        self.core.guard.tick()
        self.assertEqual(len(self.windows.calls), 1)
        self.assertFalse(self.windows.requests)
        self.assertEqual(self.reports[0]['budget']['stop_reason'], 'paused')

    def test_busy_core_defers_guard_without_spending_budget(self):
        self.core.start_guard()
        self.clock.advance(5)
        with self.core.gate:
            self.core.guard.tick()
            self.assertFalse(self.windows.calls)
        self.assertEqual(self.core.guard.schedule.state()['checks_last_hour'], 0)
        self.assertEqual(self.core.store.guard_check_ages(), [])
        self.core.guard.tick()
        self.assertEqual(len(self.reports), 1)

    def test_close_during_manual_check_waits_for_inventory_and_prevents_http_requests(self):
        entered, release, stopping = threading.Event(), threading.Event(), threading.Event()
        original = self.core.engine.observer.runner
        results = []
        def inventory_in_progress(argv, timeout=10):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('test worker failed to stop')
            return original(argv, timeout)
        self.core.engine.observer.runner = inventory_in_progress
        worker = threading.Thread(target=lambda: results.append(self.core.check()))
        closer = threading.Thread(target=self.core.close)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            with patch.object(self.core.events, 'close', side_effect=stopping.set):
                closer.start()
                self.assertTrue(stopping.wait(5))
                self.assertTrue(closer.is_alive())
                self.assertIsNotNone(self.core.store._lock_fd)
                release.set()
                worker.join(5)
                closer.join(5)
            self.assertFalse(worker.is_alive())
            self.assertFalse(closer.is_alive())
            self.assertFalse(self.windows.requests)
            self.assertEqual(results[0][0].health, 'unknown')
            self.assertIsNone(self.core.store._lock_fd)
        finally:
            release.set()
            worker.join(5)
            if closer.ident is not None:
                closer.join(5)

    def test_callback_can_close_core_without_gate_deadlock(self):
        def close_on_report(_report):
            self.assertTrue(self.core.gate.acquire(blocking=False))
            self.core.gate.release()
            self.core.close()
        self.core.on_report = close_on_report
        self.core.start_guard()
        self.clock.advance(5)
        self.core.guard.tick()
        self.assertTrue(self.core.closed)
        self.assertTrue(self.core.events.closed)
        self.assertIsNone(self.core.store._lock_fd)

    def test_failed_guard_reports_unknown_without_leaking_exception(self):
        self.core.start_guard()
        self.clock.advance(5)
        with patch.object(self.core.agent, 'investigate', side_effect=OSError('private.example.test')):
            self.core.guard.tick()
        self.assertEqual(self.reports[0]['health'], 'unknown')
        self.assertNotIn('private.example.test', json.dumps(self.reports))
        self.assertEqual(self.core.guard.schedule.failures, 1)

    def test_journal_and_baseline_failures_are_visible_in_report(self):
        with patch.object(self.core.store, 'save', side_effect=OSError('full')), \
             patch.object(self.core.store, 'save_baseline', side_effect=OSError('full')):
            _, report = self.core.check()
        self.assertEqual(set(report['persistence'].values()), {'unavailable'})

    def test_owner_lock_and_profile_survive_core_restart(self):
        profile = self.target(requirement='service')
        self.core.check()
        with self.assertRaises(OSError):
            AgentStore(self.core.store.directory)
        self.core.close()
        with self.assertRaises(RuntimeError):
            self.core.check()
        store = AgentStore(self.core.store.directory)
        self.addCleanup(store.close)
        self.assertEqual(store.load_profiles()[1], profile['id'])
        self.assertTrue(store.load_baseline(profile['id'], 1))
        self.assertTrue(store.recent())


class WindowsEventsTests(unittest.TestCase):
    def backend(self, fail_at=None, fail_cancel=False):
        backend = SimpleNamespace(callbacks={}, cancelled=[])
        def register(name, changed):
            if len(backend.callbacks) == fail_at:
                raise OSError('unsupported')
            backend.callbacks[name] = changed
            return name
        def cancel(handle):
            backend.callbacks[handle]()
            if fail_cancel:
                raise OSError('still active')
            backend.cancelled.append(handle)
        backend.register, backend.cancel = register, cancel
        return backend

    def test_notifications_only_wake_and_unregister_callbacks_after_stop(self):
        calls, backend = [], self.backend()
        events = WindowsNetworkEvents(lambda: calls.append('wake'), lambda: backend)
        self.assertTrue(events.start())
        self.assertTrue(events.start())
        self.assertEqual(len(events.handles), 3)
        for callback in backend.callbacks.values():
            callback()
        self.assertEqual(len(calls), 3)
        events.close()
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(backend.cancelled), 3)
        self.assertFalse(events.start())

    def test_partial_registration_failure_cleans_up_and_declares_periodic_only(self):
        backend = self.backend(fail_at=1)
        events = WindowsNetworkEvents(lambda: None, lambda: backend)
        self.assertFalse(events.start())
        self.assertEqual(backend.cancelled, ['NotifyIpInterfaceChange'])
        self.assertTrue(events.error)
        self.assertEqual(events.handles, [])

    def test_failed_cancel_keeps_handles_and_suppresses_new_callbacks(self):
        backend, calls = self.backend(fail_cancel=True), []
        events = WindowsNetworkEvents(lambda: calls.append(True), lambda: backend)
        events.start()
        events.close()
        self.assertEqual(len(events.handles), 3)
        self.assertFalse(calls)

    def test_native_callback_is_pinned_until_successful_cancel(self):
        backend = object.__new__(IPHelperSubscriptions)
        backend.callbacks = {123: lambda: None}
        _LIVE_SUBSCRIPTIONS.add(backend)
        self.addCleanup(_LIVE_SUBSCRIPTIONS.discard, backend)
        def reject(_handle):
            return 87
        backend.api = SimpleNamespace(CancelMibChangeNotify2=reject)
        with self.assertRaises(OSError):
            backend.cancel(123)
        self.assertIn(backend, _LIVE_SUBSCRIPTIONS)
        self.assertIn(123, backend.callbacks)
        def succeed(_handle):
            return 0
        backend.api.CancelMibChangeNotify2 = succeed
        backend.cancel(123)
        self.assertNotIn(backend, _LIVE_SUBSCRIPTIONS)
        self.assertFalse(backend.callbacks)


class WindowsStorageContractTests(unittest.TestCase):
    def test_acl_requires_current_owner_protected_directory_and_only_user_system_access(self):
        entries = [((0, 3), 0x1F01FF, SID), ((0, 3), 0x1F01FF, 'S-1-5-18')]
        value = {'entries': entries, 'control': 0x1000, 'owner': SID, 'null': False}
        acl = SimpleNamespace(GetAceCount=lambda: len(value['entries']), GetAce=lambda index: value['entries'][index])
        descriptor = SimpleNamespace(GetSecurityDescriptorOwner=lambda: value['owner'],
            GetSecurityDescriptorControl=lambda: (value['control'], 1),
            GetSecurityDescriptorDacl=lambda: None if value['null'] else acl)
        files = object.__new__(WindowsPrivateFiles)
        files.sid_text = SID
        files.security = SimpleNamespace(SE_FILE_OBJECT=1, OWNER_SECURITY_INFORMATION=1, DACL_SECURITY_INFORMATION=4,
            ACCESS_ALLOWED_ACE_TYPE=0, GetNamedSecurityInfo=lambda *_: descriptor, ConvertSidToStringSid=lambda sid: sid)
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            files.check(directory, directory=True)
            for key, bad in (('owner', 'S-1-5-18'), ('control', 0), ('null', True),
                             ('entries', entries + [((0, 3), 1, 'S-1-1-0')]),
                             ('entries', [((0, 8), 0x1F01FF, SID)]), ('entries', [((0, 0), 1, SID)])):
                original = value[key]
                value[key] = bad
                with self.subTest(key=key, bad=bad), self.assertRaises(PermissionError):
                    files.check(directory, directory=True)
                value[key] = original

    def test_windows_budget_matches_absolute_backslash_and_uppercase_curl_name(self):
        budget = ProbeBudget(replace(GuardPolicy(), scan_requests=1))
        calls = []
        def runner(argv, timeout=10):
            calls.append(argv)
            return CommandResult('200')
        command = [r'C:\Windows\System32\CURL.EXE', '--disable', '--', 'https://example.test']
        budget.run(runner, command)
        with self.assertRaises(RuntimeError):
            budget.run(runner, command)
        self.assertEqual(budget.requests, 1)
        self.assertEqual(calls[0][1], '--disable')

    def test_windows_fresh_config_never_runs_mac_auto_detection(self):
        windows = FakeWindows()
        with tempfile.TemporaryDirectory() as directory, patch('relay_config.sys.platform', 'win32'):
            config = ConfigManager(Path(directory) / 'config.json', runner=windows)
            config.load()
            self.assertFalse(windows.calls)
            self.assertFalse(config.get('guard.enabled'))
            self.assertEqual(config.get('dns.public_dns'), [])


if __name__ == '__main__':
    unittest.main()
