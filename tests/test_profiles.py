"""Health expectations never come from observed failures or imported authority."""
import copy
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code'))
from relay.agent import NetworkAssuranceAgent
from relay.agent_store import AgentStore
from relay.checks.reachability import ReachabilityCheck
from relay.commands import CommandResult
from relay.engine import DetectionEngine, FixEngine
from relay.guard import GuardPolicy, ProbeBudget
from relay.profiles import HealthProfiles, ProfileConfig, SCHEMA, expire_assessment, normalize_document, parse_import, scope_state
from test_diagnostics import FakeMac, config


def document(**target):
    return {'schema': SCHEMA, 'name': '办公网络', 'targets': [
        {'name': '公司服务', 'url': 'https://work.example.test', **target}]}


class ProfileSchemaTests(unittest.TestCase):
    def test_import_has_new_ids_and_no_implicit_authority(self):
        original = normalize_document(document())
        imported = parse_import(json.dumps(original))
        self.assertNotEqual(original['targets'][0]['id'], imported['targets'][0]['id'])
        for field in ('source', 'confirmed_at', 'grant', 'commands', 'last_verified'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                parse_import(json.dumps({**original, field: 'untrusted'}))

    def test_strict_file_and_target_boundaries(self):
        for invalid in ([], {}, {'schema': SCHEMA, 'name': '', 'targets': []},
                        document(url='file:///etc/hosts'), document(url='https://u:p@example.test'),
                        document(url='https://example.test/#token'), document(url='https://example.test:99999'),
                        document(url='https://example.test/\nsecret'), document(url='https://example.test:0'),
                        document(expected_path=[]), document(when='company'), document(requirement='guess'),
                        document(timeout=True), document(timeout=0), document(timeout=31),
                        document(shell='networksetup -setdnsservers'), document(name='')):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_document(invalid)
        with self.assertRaises(ValueError):
            parse_import('{"schema":"a","schema":"b"}')
        with self.assertRaises(ValueError):
            parse_import(' ' * (256 * 1024 + 1))

    def test_duplicate_names_are_distinct_and_duplicate_ids_rejected(self):
        value = document()
        value['targets'] *= 2
        normalized = normalize_document(value)
        self.assertNotEqual(*[t['id'] for t in normalized['targets']])
        normalized['targets'][1]['id'] = normalized['targets'][0]['id']
        with self.assertRaises(ValueError):
            normalize_document(normalized, retain_ids=True)

    def test_unknown_environment_is_not_out_of_scope(self):
        target = {'when': 'vpn_connected'}
        self.assertIsNone(scope_state(target, {'vpn': 'unknown'}))
        self.assertIsNone(scope_state(target, {'vpn': 'ok', 'vpn_evidence': {'owner_confirmed': False}}))
        self.assertIs(scope_state(target, {'vpn': 'off', 'vpn_path': 'off'}), False)
        self.assertIs(scope_state(target, {'vpn': 'ok', 'vpn_evidence': {'owner_confirmed': True}}), True)


class ProfilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.mac = FakeMac()
        self.config = config(self.mac)
        self.store = AgentStore(Path(self.temp.name) / 'agent')
        self.addCleanup(self.store.close)
        self.profiles = HealthProfiles(self.store, self.config)
        self.runtime = ProfileConfig(self.config, self.profiles)
        self.engine = DetectionEngine(self.runtime, runner=self.mac)
        self.fix = FixEngine(self.runtime, self.engine, snapshot_dir=Path(self.temp.name) / 'recovery')
        self.agent = NetworkAssuranceAgent(self.runtime, self.engine, self.fix,
                                           store=self.store, profiles=self.profiles)

    def test_seed_preserves_config_and_does_not_learn_current_dns(self):
        original = copy.deepcopy(self.config.config)
        self.mac.manual_dns = ['203.0.113.53']
        again = HealthProfiles(self.store, self.config)
        self.assertEqual(len(again.profiles), 1)
        self.assertEqual(again.active['source'], 'configured_targets')
        self.assertIsNone(again.active['confirmed_at'])
        self.assertIsNone(again.baseline)
        self.assertEqual(self.runtime.get('dns.public_dns'), [])
        self.assertEqual(self.config.config, original)
        self.runtime.get('reachability.targets').clear()
        self.assertTrue(self.profiles.active['targets'])

    def test_save_revise_switch_remove_and_restart(self):
        old_id = self.profiles.active['id']
        profile = self.profiles.save(document())
        self.assertEqual(profile['revision'], 1)
        draft = self.profiles.document(profile['id'])
        draft['name'] = '修订后的办公网络'
        revised = self.profiles.save(draft, profile['id'], 1)
        self.assertEqual(revised['revision'], 2)
        self.assertEqual(profile['targets'][0]['id'], revised['targets'][0]['id'])
        with self.assertRaises(ValueError):
            self.profiles.save(draft, profile['id'], 1)
        with self.assertRaises(ValueError):
            self.profiles.remove(profile['id'])
        self.profiles.activate(old_id)
        self.profiles.remove(profile['id'])
        reloaded = HealthProfiles(self.store, self.config)
        self.assertEqual(reloaded.active['id'], old_id)
        self.assertEqual(len(reloaded.profiles), 1)

    def test_save_and_activation_are_atomic_on_database_failure(self):
        import sqlite3
        before = copy.deepcopy(self.profiles.active)
        with self.store._connect() as db:
            db.execute("CREATE TRIGGER reject_activation BEFORE UPDATE ON agent_settings "
                       "BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.profiles.save(document())
        self.profiles.reload()
        self.assertEqual(self.profiles.active, before)
        self.assertEqual(len(self.profiles.profiles), 1)

    def test_only_fresh_matching_healthy_observations_become_last_verification(self):
        self.profiles.save(document(requirement='service'))
        snapshot = self.engine.run_all()
        result = self.profiles.record_verification(snapshot)
        self.assertEqual(result['health'], 'healthy')
        saved = self.profiles.baseline
        self.assertIsNotNone(saved)
        self.mac.http_status = '401'
        failed = self.agent.observe()
        self.assertEqual(failed.health, 'attention')
        self.assertTrue(failed.incident_id)
        self.assertEqual(self.agent.assess()['profile']['targets'][0]['state'], 'auth_required')
        self.assertEqual(self.profiles.baseline, saved)
        self.assertEqual(self.profiles.active['targets'][0]['requirement'], 'service')
        self.assertEqual(failed.observations[-1].state, 'auth_required')
        for change in ({'last_check': datetime.now() - timedelta(seconds=601)},
                       {'last_check': datetime.now() + timedelta(seconds=10)},
                       {'health_profile': {'id': 'other', 'revision': 1}}):
            with self.subTest(change=change):
                self.assertEqual(self.profiles.record_verification({**snapshot, **change})['health'], 'unknown')
                self.assertEqual(self.profiles.baseline, saved)
        self.profiles.save(document(url='https://new.example.test'))
        self.assertEqual(self.profiles.evaluate(snapshot)['health'], 'unknown')
        self.assertIsNone(self.profiles.baseline)

    def test_observation_hash_prevents_reuse_on_target_mutation(self):
        snapshot = self.engine.run_all()
        self.profiles.active['targets'][0]['url'] = 'https://elsewhere.example.test'
        result = self.profiles.record_verification(snapshot)
        self.assertEqual(result['health'], 'unknown')
        self.assertIsNone(self.profiles.baseline)

    def test_cached_ui_result_expires_without_another_probe(self):
        snapshot = self.engine.run_all()
        value = self.profiles.evaluate(snapshot)
        self.assertEqual(value['health'], 'healthy')
        self.assertEqual(expire_assessment(copy.deepcopy(value), value['observed_at'] + 600)['health'], 'healthy')
        expired = expire_assessment(value, value['observed_at'] + 601)
        self.assertEqual(expired['health'], 'unknown')
        self.assertEqual(expired['covered'], 0)
        self.assertEqual({r['state'] for r in expired['targets']}, {'stale'})

    def test_failed_or_disabled_probes_do_not_create_baseline(self):
        self.mac.curl_exit = 28
        self.agent.observe()
        self.assertIsNone(self.profiles.baseline)
        self.mac.curl_exit = 0
        self.config.set('reachability.enabled', False)
        self.engine.reload_checks()
        self.assertEqual(self.agent.observe().health, 'unknown')
        self.assertIsNone(self.profiles.baseline)

    def test_invalid_stored_profile_does_not_silently_restore_defaults(self):
        with patch.object(self.store, 'load_profiles', return_value=(self.profiles.profiles, 'missing')):
            with self.assertRaises(ValueError):
                HealthProfiles(self.store, self.config)
        broken = copy.deepcopy(self.profiles.active)
        broken['targets'][0]['expected_path'] = 'invented'
        with patch.object(self.store, 'load_profiles', return_value=([broken], broken['id'])):
            with self.assertRaises(ValueError):
                HealthProfiles(self.store, self.config)

    def test_not_applicable_only_profile_is_not_healthy(self):
        self.profiles.save(document(when='vpn_connected'))
        self.assertEqual(self.agent.observe().health, 'unknown')
        self.assertIsNone(self.profiles.baseline)
        self.assertFalse(any(Path(a[0]).name == 'curl' for a in self.mac.calls))

    def test_baseline_failure_is_reported_independently_of_journal(self):
        with patch.object(self.store, 'save_baseline', side_effect=OSError('full')):
            self.agent.observe()
        self.assertTrue(self.agent.profile_error)
        self.assertEqual(self.agent.journal_error, '')
        self.agent.observe()
        self.assertEqual(self.agent.profile_error, '')

    def test_login_redirect_does_not_prove_service_success(self):
        self.profiles.save(document(requirement='service'))
        self.mac.http_status = '302'
        run = self.agent.observe()
        self.assertEqual(run.health, 'degraded')
        self.assertTrue(run.incident_id)
        self.assertIsNone(self.profiles.baseline)

    def test_changed_profile_rejects_authorization_and_consumption(self):
        self.config = config(self.mac, company=True)
        self.runtime = ProfileConfig(self.config, self.profiles)
        self.engine = DetectionEngine(self.runtime, runner=self.mac)
        self.fix = FixEngine(self.runtime, self.engine, snapshot_dir=Path(self.temp.name) / 'recovery')
        self.agent = NetworkAssuranceAgent(self.runtime, self.engine, self.fix, store=self.store, profiles=self.profiles)
        run = self.agent.propose(['dns_mixed_on_vpn'])
        self.assertTrue(run.proposals)
        self.profiles.save(document())
        with self.assertRaises(PermissionError):
            self.agent.authorize(run)
        run = self.agent.propose(['dns_mixed_on_vpn'])
        grant = self.agent.authorize(run)
        self.profiles.save(document(requirement='service'))
        with self.assertRaises(PermissionError):
            self.agent.execute(run, grant)
        self.assertEqual(self.mac.mutations, [])

    def test_service_auth_regression_rolls_back_verified_configuration_write(self):
        self.config = config(self.mac, company=True)
        self.profiles.save(document(requirement='service'))
        self.runtime = ProfileConfig(self.config, self.profiles)
        def boundary(argv, timeout=10):
            if Path(argv[0]).name == 'curl' and self.mac.has_applied:
                return CommandResult('401')
            return self.mac(argv, timeout)
        self.engine = DetectionEngine(self.runtime, runner=boundary)
        self.fix = FixEngine(self.runtime, self.engine, snapshot_dir=Path(self.temp.name) / 'recovery')
        self.agent = NetworkAssuranceAgent(self.runtime, self.engine, self.fix, store=self.store, profiles=self.profiles)
        run = self.agent.propose(['dns_mixed_on_vpn'])
        self.agent.execute(run, self.agent.authorize(run))
        self.assertEqual(run.outcome, 'rolled_back')
        self.assertEqual(self.mac.manual_dns, ['8.8.8.8'])
        self.assertTrue(any('退化' in line for line in run.results))

    def test_public_target_dns_evidence_requests_exact_repair_and_verifies(self):
        self.profiles.save({'schema': SCHEMA, 'name': '上网', 'targets': [
            {'name': 'Google', 'url': 'https://www.google.com', 'expected_path': 'system'}]})
        self.mac.manual_dns = ['223.5.5.5']
        self.mac.effective_dns = ['223.5.5.5']
        self.runtime = ProfileConfig(self.config, self.profiles)
        def boundary(argv, timeout=10):
            name = Path(argv[0]).name
            if name == 'dscacheutil':
                return CommandResult('name: www.google.com\nip_address: 69.171.235.22\n')
            if name == 'dig':
                return CommandResult('142.251.153.119\n')
            if name == 'curl' and 'https://www.google.com' in argv:
                if '--resolve' in argv or self.mac.manual_dns == ['1.1.1.1']:
                    return CommandResult('200')
                return CommandResult('000', 'timeout', 28)
            return self.mac(argv, timeout)
        self.engine = DetectionEngine(self.runtime, runner=boundary)
        self.fix = FixEngine(self.runtime, self.engine, snapshot_dir=Path(self.temp.name) / 'recovery')
        self.agent = NetworkAssuranceAgent(self.runtime, self.engine, self.fix,
                                           store=self.store, profiles=self.profiles)
        run = self.agent.investigate()
        self.assertEqual(run.stage, 'awaiting_authorization')
        self.assertEqual(run.proposals[0].actions[0]['desired'], ['1.1.1.1'])
        self.assertEqual(self.mac.mutations, [])
        self.agent.execute(run, self.agent.authorize(run))
        self.assertEqual(run.outcome, 'verified')
        self.assertEqual(self.mac.manual_dns, ['1.1.1.1'])
        self.assertEqual(self.profiles.evaluate(self.engine.snapshot())['health'], 'healthy')

    def test_private_target_never_queries_public_reference_resolver(self):
        self.profiles.save(document())
        self.mac.manual_dns = ['223.5.5.5']
        self.mac.curl_exit = 28
        self.runtime = ProfileConfig(self.config, self.profiles)
        self.engine = DetectionEngine(self.runtime, runner=self.mac)
        snapshot = self.engine.run_all()
        self.assertFalse(any(Path(call[0]).name == 'dig' for call in self.mac.calls))
        self.assertFalse(any(kind.startswith('dns_reference_') for _, kind, _ in snapshot['issues']))

    def test_reference_dns_is_only_a_proposal_when_pinned_access_fails(self):
        self.profiles.save({'schema': SCHEMA, 'name': '上网', 'targets': [
            {'name': 'Google', 'url': 'https://www.google.com', 'expected_path': 'system'}]})
        self.mac.manual_dns = ['223.5.5.5']
        self.runtime = ProfileConfig(self.config, self.profiles)
        def boundary(argv, timeout=10):
            name = Path(argv[0]).name
            if name == 'dscacheutil':
                return CommandResult('ip_address: 69.171.235.22\n')
            if name == 'dig':
                return CommandResult('142.251.153.119\n')
            if name == 'curl' and 'https://www.google.com' in argv:
                return CommandResult('000', 'timeout', 28)
            return self.mac(argv, timeout)
        self.engine = DetectionEngine(self.runtime, runner=boundary)
        self.fix = FixEngine(self.runtime, self.engine)
        run = NetworkAssuranceAgent(self.runtime, self.engine, self.fix,
                                    store=self.store, profiles=self.profiles).investigate()
        self.assertFalse(run.proposals)
        self.assertEqual(self.fix.repair_options(), {})
        self.assertEqual(self.mac.mutations, [])

    def test_dns_reference_repair_rolls_back_if_google_remains_unreachable(self):
        self.profiles.save({'schema': SCHEMA, 'name': '上网', 'targets': [
            {'name': 'Google', 'url': 'https://www.google.com', 'expected_path': 'system'}]})
        self.mac.manual_dns = ['223.5.5.5']
        self.runtime = ProfileConfig(self.config, self.profiles)
        def boundary(argv, timeout=10):
            name = Path(argv[0]).name
            if name == 'dscacheutil':
                return CommandResult('ip_address: 69.171.235.22\n')
            if name == 'dig':
                return CommandResult('142.251.153.119\n')
            if name == 'curl' and 'https://www.google.com' in argv:
                if '--resolve' in argv:
                    return CommandResult('200')
                return CommandResult('000', 'timeout', 28)
            return self.mac(argv, timeout)
        self.engine = DetectionEngine(self.runtime, runner=boundary)
        self.fix = FixEngine(self.runtime, self.engine, snapshot_dir=Path(self.temp.name) / 'recovery')
        self.agent = NetworkAssuranceAgent(self.runtime, self.engine, self.fix,
                                           store=self.store, profiles=self.profiles)
        run = self.agent.investigate()
        self.assertEqual(run.stage, 'awaiting_authorization')
        self.agent.execute(run, self.agent.authorize(run))
        self.assertEqual(run.outcome, 'rolled_back')
        self.assertEqual(self.mac.manual_dns, ['223.5.5.5'])


class TargetPathTests(unittest.TestCase):
    def setUp(self):
        self.mac = FakeMac()
        self.config = config(self.mac)
        self.status = {'vpn': 'ok', 'vpn_path': 'ok', 'vpn_client': 'Test VPN',
                       'vpn_evidence': {'owner_confirmed': True, 'interface': 'utun8'}}
        self.calls = []
        self.route = 'utun8'
        self.remote = '10.12.0.20'
        self.code = '200'
        self.drift = False
        self.routes = 0

    def runner(self, argv, timeout=10):
        self.calls.append(list(argv))
        command = Path(argv[0]).name
        if command == 'dscacheutil':
            return CommandResult('name: work.example.test\nip_address: 10.12.0.20\n')
        if command == 'route':
            self.routes += 1
            return CommandResult('interface: ' + ('en0' if self.drift and self.routes > 1 else self.route))
        if command == 'curl':
            return CommandResult(self.code + (' ' + self.remote if '%{http_code} %{remote_ip}' in argv else ''))
        raise AssertionError(argv)

    def check(self, **fields):
        target = normalize_document(document(**fields))['targets'][0]
        self.config.set('reachability.targets', [target])
        check = ReachabilityCheck(self.config, runner=self.runner)
        issues = check.check(self.status)
        return self.status['reachability_results'][target['id']], issues

    def test_out_of_scope_and_unknown_scope_do_not_request(self):
        observation, issues = self.check(when='vpn_disconnected')
        self.assertIs(observation['applicable'], False)
        self.assertEqual(self.calls, [])
        self.status['vpn'] = 'unknown'
        observation, issues = self.check(when='vpn_connected')
        self.assertIsNone(observation['applicable'])
        self.assertTrue(issues)
        self.assertEqual(self.calls, [])

    def test_missing_proxy_never_falls_back_to_direct(self):
        observation, issues = self.check(expected_path='system_proxy')
        self.assertFalse(observation['path_verified'])
        self.assertTrue(issues)
        self.assertEqual(self.calls, [])

    def test_pac_system_path_is_unknown_but_explicit_direct_is_valid(self):
        self.status['proxy_pac'] = True
        observation, issues = self.check(expected_path='system')
        self.assertIsNone(observation['path_verified'])
        self.assertTrue(issues)
        observation, _ = self.check(expected_path='direct')
        self.assertTrue(observation['path_verified'])
        self.assertIn('--noproxy', self.calls[-1])

    def test_system_proxy_exceptions_and_scoped_paths_are_not_guessed(self):
        self.status['proxy_details'] = {'https': {'host': '127.0.0.1', 'port': 7890}}
        self.status['proxy_constraints'] = {'exceptions': True}
        observation, issues = self.check(expected_path='system')
        self.assertIsNone(observation['path_verified'])
        self.assertTrue(issues)
        self.assertEqual(self.calls, [])
        observation, _ = self.check(expected_path='system_proxy')
        self.assertTrue(observation['path_verified'])
        self.assertIn('http://127.0.0.1:7890', self.calls[-1])
        self.calls.clear()
        self.status['proxy_details'] = {}
        self.status['proxy_constraints'] = {'scoped': True}
        observation, _ = self.check(expected_path='system')
        self.assertIsNone(observation['path_verified'])
        self.assertEqual(self.calls, [])

    def test_vpn_requires_owner_route_pin_and_actual_remote_ip(self):
        observation, issues = self.check(expected_path='vpn')
        self.assertEqual(issues, [])
        self.assertTrue(observation['path_verified'])
        args = next(a for a in self.calls if Path(a[0]).name == 'curl')
        self.assertEqual(args[1], '--disable')
        self.assertIn('--globoff', args)
        self.assertIn('work.example.test:443:10.12.0.20', args)
        self.assertIn('if!utun8', args)
        self.assertIn('--noproxy', args)
        self.assertNotIn('--location', args)
        self.remote = '198.51.100.9'
        observation, issues = self.check(expected_path='vpn')
        self.assertFalse(observation['path_verified'])
        self.assertTrue(issues)

    def test_vpn_unknown_owner_never_resolves_or_requests(self):
        self.status['vpn_evidence']['owner_confirmed'] = False
        observation, issues = self.check(expected_path='vpn')
        self.assertIsNone(observation['path_verified'])
        self.assertTrue(issues)
        self.assertEqual(self.calls, [])

    def test_wrong_route_never_requests_and_drift_is_not_healthy(self):
        self.route = 'en0'
        observation, _ = self.check(expected_path='vpn')
        self.assertIsNone(observation['path_verified'])
        self.assertFalse(any(Path(a[0]).name == 'curl' for a in self.calls))
        self.route, self.routes, self.drift = 'utun8', 0, True
        observation, _ = self.check(expected_path='vpn')
        self.assertFalse(observation['path_verified'])

    def test_ipv6_literal_and_domain_pin_formats(self):
        from relay.target_paths import pinned_host_option
        self.assertEqual(pinned_host_option('https://[2001:db8::1]', '2001:db8::1'), [])
        self.assertEqual(pinned_host_option('https://work.example.test:8443', '2001:db8::1'),
                         ['--resolve', 'work.example.test:8443:[2001:db8::1]'])

    def test_guard_body_budget_preserves_disabled_curlrc_and_remote_evidence(self):
        budget = ProbeBudget(GuardPolicy())
        calls = []
        def runner(argv, timeout=10):
            calls.append(argv)
            return CommandResult('200 10.12.0.20', returncode=63)
        result = budget.run(runner, ['/usr/bin/curl', '--disable', '--globoff', '--', 'https://example.test'])
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout, '200 10.12.0.20')
        self.assertEqual(calls[0][1], '--disable')
        self.assertIn('--max-filesize', calls[0])
        macos_limit = budget.run(lambda argv, timeout: CommandResult('200',
            'Exceeded the maximum allowed file size (65536) with 65536 bytes', 56),
            ['/usr/bin/curl', '--disable', '--globoff', '--', 'https://example.test'])
        self.assertTrue(macos_limit.ok)
        unrelated_failure = budget.run(lambda argv, timeout: CommandResult('200', 'connection reset', 56),
            ['/usr/bin/curl', '--disable', '--globoff', '--', 'https://example.test'])
        self.assertFalse(unrelated_failure.ok)

    def test_repair_regression_detects_path_scope_and_definition_changes(self):
        original = {'transport': 'ok', 'service': 'responding', 'applicable': True,
                    'definition_hash': 'one', 'path_verified': True}
        for change in ({'path_verified': False}, {'applicable': False}, {'definition_hash': 'two'}):
            with self.subTest(change=change):
                self.assertTrue(FixEngine._regressed({'t': original}, {'t': {**original, **change}}))

    @unittest.skipUnless(sys.platform == 'darwin' and Path('/usr/bin/curl').is_file(), 'macOS curl loopback check')
    def test_real_curl_pin_interface_glob_and_config_limits_on_loopback(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from relay.commands import run_command
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                requests.append(self.path)
                self.send_response(302)
                self.send_header('Location', '/redirect')
                self.send_header('Content-Length', '0')
                self.end_headers()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            target = normalize_document(document(expected_path='vpn',
                url=f'http://work.example.test:{server.server_port}/[1-9]'))['targets'][0]
            self.config.set('reachability.targets', [target])
            destination = {'address': '127.0.0.1', 'interface': 'lo0', 'owner': 'test-only'}
            budget = ProbeBudget(GuardPolicy())
            with tempfile.TemporaryDirectory() as tmp:
                Path(tmp, '.curlrc').write_text(f'location\nurl = "http://127.0.0.1:{server.server_port}/extra"\n')
                with patch.dict(os.environ, {'CURL_HOME': tmp}), \
                        patch('relay.checks.reachability.vpn_destination', return_value=(destination, '')), \
                        patch('relay.checks.reachability.route_interface', return_value='lo0'):
                    check = ReachabilityCheck(self.config, runner=lambda argv, timeout=10: budget.run(run_command, argv, timeout))
                    self.assertEqual(check.check(self.status), [])
            observation = self.status['reachability_results'][target['id']]
            self.assertTrue(observation['path_verified'])
            self.assertEqual(observation['remote_ip'], '127.0.0.1')
            self.assertEqual(observation['http_status'], 302)
            self.assertEqual(requests, ['/[1-9]'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    unittest.main()
