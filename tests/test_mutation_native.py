"""Native transaction code against memory preferences and private temporary files."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code'))


def native_dictionary(value):
    from Foundation import NSJSONSerialization
    data, error = NSJSONSerialization.dataWithJSONObject_options_error_(value, 0, None)
    if error:
        raise ValueError('Fixture is not valid JSON')
    return NSJSONSerialization.JSONObjectWithData_options_error_(data, 0, None)[0]


@unittest.skipUnless(sys.platform == 'darwin' and os.getenv('RELAY_NATIVE_UI_TESTS') == '1', 'opt-in native mutation checks')
class NativeMutationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if hasattr(cls, 'preferences_class'):
            return
        from relay.mac_identity import native_bridge
        # Never replace a Mach-O image already loaded by another native suite.
        if not native_bridge.cache_info().currsize:
            subprocess.run([sys.executable, str(ROOT / 'scripts/build-native-ipc.py')], check=True, capture_output=True)
        native_bridge()
        import objc
        from Foundation import NSObject

        class RelayMemoryPreferences(NSObject, protocols=[objc.protocolNamed('RelayPreferencesAccess')]):
            def readTarget_field_(self, target, field):
                self.reads += 1
                if dict(target) != self.target:
                    raise ValueError('synthetic target mismatch')
                self.field = str(field)
                return self.current()

            def current(self):
                if self.fail_readback and self.writes:
                    raise ValueError('synthetic unreadable state')
                return native_dictionary(copy.deepcopy(self.snapshot))

            def commit_(self, value):
                self.writes += 1
                self.intent = [json.loads(p.read_text()) for p in self.directory.glob('*.json')]
                if self.entered:
                    self.entered.set()
                    self.release.wait(5)
                if self.persist:
                    self.snapshot = json.loads(bytes(__import__('Foundation').NSJSONSerialization.dataWithJSONObject_options_error_(value, 0, None)[0]))
                if self.external:
                    self.snapshot['configuration'].update(self.external)
                if self.on_commit:
                    self.on_commit(copy.deepcopy(self.snapshot))
                return self.apply_ok

            def unlock(self):
                self.unlocks += 1

        cls.preferences_class = RelayMemoryPreferences
        cls.executor_class = objc.lookUpClass('RelayMutationExecutor')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.target = {'service_id': 'EXACT-SERVICE-ID', 'name': 'Wi-Fi', 'interface': 'en0'}
        self.preferences = self.preferences_class.alloc().init()
        self.preferences.target = dict(self.target)
        self.preferences.directory = self.directory
        self.preferences.snapshot = {'configuration': {'ServerAddresses': ['10.0.0.53'], 'SearchDomains': ['kept.invalid']}, 'enabled': True}
        self.preferences.reads = self.preferences.writes = self.preferences.unlocks = 0
        self.preferences.apply_ok = self.preferences.persist = True
        self.preferences.fail_readback = False
        self.preferences.external = None
        self.preferences.on_commit = None
        self.preferences.entered = self.preferences.release = None
        self.executor = self.executor_class.alloc().initWithDirectory_preferences_(self.temp.name, self.preferences)
        self.addCleanup(self.close)

    def close(self):
        self.executor = None

    def request(self, **changes):
        return {'version': 1, 'id': uuid.uuid4().hex, 'proposal': 'a' * 64, 'target': dict(self.target),
                'field': 'dns', 'expected': ['10.0.0.53'], 'desired': ['1.1.1.1'], 'endpoint': {}, 'restore_of': '', **changes}

    def perform(self, request, user=501, session=42):
        from Foundation import NSJSONSerialization
        result = self.executor.perform_user_session_(native_dictionary(request), user, session)
        data, error = NSJSONSerialization.dataWithJSONObject_options_error_(result, 0, None)
        self.assertIsNone(error)
        return json.loads(bytes(data))

    def restore(self, request):
        return {**request, 'id': uuid.uuid4().hex, 'expected': request['desired'],
                'desired': request['expected'], 'restore_of': request['id']}

    def test_intent_precedes_commit_and_receipt_does_not_claim_network_recovery(self):
        request = self.request()
        result = self.perform(request)
        self.assertEqual(result['state'], 'configured')
        self.assertFalse(result['network_verified'])
        self.assertEqual(self.preferences.intent[0]['phase'], 'prepared')
        self.assertNotIn('result', self.preferences.intent[0])
        self.assertEqual(self.preferences.snapshot['configuration']['SearchDomains'], ['kept.invalid'])
        record = json.loads(next(self.directory.glob('*.json')).read_text())
        self.assertEqual(record['before']['configuration']['ServerAddresses'], request['expected'])
        self.assertEqual(record['after']['configuration']['ServerAddresses'], request['desired'])
        self.assertEqual(next(self.directory.glob('*.json')).stat().st_mode & 0o777, 0o600)

    def test_duplicate_and_conflicting_ids_never_repeat_write(self):
        request = self.request()
        first = self.perform(request)
        self.assertEqual(self.perform(request), first)
        self.assertEqual(self.perform({**request, 'desired': ['8.8.8.8']})['error'], 'operation_id_conflict')
        self.assertEqual(self.preferences.writes, 1)

    def test_reopen_preserves_deduplication_and_owner_is_exclusive(self):
        request = self.request()
        result = self.perform(request)
        with self.assertRaises(Exception):
            self.executor_class.alloc().initWithDirectory_preferences_(self.temp.name, self.preferences)
        self.executor = None
        self.executor = self.executor_class.alloc().initWithDirectory_preferences_(self.temp.name, self.preferences)
        self.assertEqual(self.perform(request), result)
        self.assertEqual(self.preferences.writes, 1)

    def test_extended_allow_acl_never_inherits_private_mode_trust(self):
        request = self.request()
        self.perform(request)
        receipt = next(self.directory.glob('*.json'))
        for path in (self.directory, receipt):
            with self.subTest(path=path.name):
                subprocess.run(['/bin/chmod', '+a', 'everyone allow read,write', str(path)], check=True)
                try:
                    self.assertIn('error', self.perform(request))
                finally:
                    subprocess.run(['/bin/chmod', '-a#', '0', str(path)], check=True)
        self.assertEqual(self.preferences.writes, 1)
        subprocess.run(['/bin/chmod', '+a', 'everyone deny delete', str(receipt)], check=True)
        try:
            self.assertEqual(self.perform(request)['state'], 'configured')
        finally:
            subprocess.run(['/bin/chmod', '-a#', '0', str(receipt)], check=True)
        self.executor = None
        owner = self.directory / 'owner.lock'
        subprocess.run(['/bin/chmod', '+a', 'everyone allow read,write', str(owner)], check=True)
        try:
            with self.assertRaises(Exception):
                self.executor_class.alloc().initWithDirectory_preferences_(self.temp.name, self.preferences)
        finally:
            subprocess.run(['/bin/chmod', '-a#', '0', str(owner)], check=True)
        self.executor = self.executor_class.alloc().initWithDirectory_preferences_(self.temp.name, self.preferences)
        self.assertEqual(self.perform(request)['state'], 'configured')

    def test_strict_field_value_target_and_endpoint_schema(self):
        bad = [{'shell': 'anything'}, {'version': True}, {'id': '../outside'}, {'proposal': 'not-a-hash'},
               {'field': 'route'}, {'desired': ['example.invalid']}, {'desired': ['::1%en0']},
               {'desired': ['1.1.1.1'] * 17}, {'desired': True}, {'target': {'name': 'Wi-Fi'}},
               {'field': 'ipv6', 'expected': 'Automatic', 'desired': 'Manual'},
               {'field': 'proxy:http', 'expected': True, 'desired': 0, 'endpoint': {'host': 'localhost', 'port': 8080}},
               {'endpoint': {'host': 'localhost', 'port': 80}}, {'restore_of': 'not-an-id'}]
        for changes in bad:
            with self.subTest(changes=changes):
                self.assertIn('error', self.perform(self.request(**changes)))
        self.assertEqual(self.preferences.writes, 0)
        self.assertEqual(self.preferences.reads, 0)

    def test_drift_wrong_target_and_proxy_endpoint_fail_before_intent(self):
        self.assertEqual(self.perform(self.request(expected=[]))['error'], 'configuration_changed')
        self.assertIn('error', self.perform(self.request(target={**self.target, 'service_id': 'REPLACED'})))
        self.preferences.snapshot = {'configuration': {'HTTPEnable': 1, 'HTTPProxy': '127.0.0.1', 'HTTPPort': 8081}, 'enabled': True}
        request = self.request(field='proxy:http', expected=True, desired=False, endpoint={'host': '127.0.0.1', 'port': 8080})
        self.assertEqual(self.perform(request)['error'], 'endpoint_changed')
        self.assertFalse(list(self.directory.glob('*.json')))
        self.assertEqual(self.preferences.writes, 0)

    def test_restore_is_bound_to_original_operation_and_preserves_other_values(self):
        request = self.request()
        self.perform(request)
        self.preferences.snapshot['configuration']['SearchDomains'] = ['external.invalid']
        restore = self.restore(request)
        self.assertEqual(self.perform({**restore, 'desired': ['8.8.4.4']})['error'], 'invalid_restore')
        self.assertEqual(self.perform({**restore, 'proposal': 'b' * 64})['error'], 'invalid_restore')
        self.assertEqual(self.perform(restore, user=502)['error'], 'invalid_restore')
        self.assertEqual(self.perform(restore, session=43)['error'], 'invalid_restore')
        self.assertEqual(self.perform(restore)['state'], 'configured')
        self.assertEqual(self.preferences.snapshot['configuration'], {'ServerAddresses': ['10.0.0.53'], 'SearchDomains': ['external.invalid']})
        self.assertEqual(self.preferences.writes, 2)

    def test_restore_never_overwrites_third_party_field_value(self):
        request = self.request()
        self.perform(request)
        self.preferences.snapshot['configuration']['ServerAddresses'] = ['8.8.8.8']
        self.assertEqual(self.perform(self.restore(request))['error'], 'configuration_changed')
        self.assertEqual(self.preferences.writes, 1)

    def test_dns_automatic_and_proxy_restore_preserve_absent_keys(self):
        self.preferences.snapshot['configuration'].pop('ServerAddresses')
        request = self.request(expected=[])
        self.assertEqual(self.perform(request)['state'], 'configured')
        self.assertEqual(self.perform(self.restore(request))['state'], 'configured')
        self.assertNotIn('ServerAddresses', self.preferences.snapshot['configuration'])
        self.preferences.snapshot = {'configuration': {'SOCKSProxy': 'localhost', 'SOCKSPort': 1080, 'ExceptionsList': ['*.invalid']}, 'enabled': True}
        request = self.request(field='proxy:socks', expected=False, desired=True, endpoint={'host': 'localhost', 'port': 1080})
        self.assertEqual(self.perform(request)['state'], 'configured')
        self.assertEqual(self.perform(self.restore(request))['state'], 'configured')
        self.assertNotIn('SOCKSEnable', self.preferences.snapshot['configuration'])
        self.assertEqual(self.preferences.snapshot['configuration']['ExceptionsList'], ['*.invalid'])

    def test_ipv6_disable_preserves_original_configuration_and_restore(self):
        self.preferences.snapshot = {'configuration': {'ConfigMethod': 'Automatic', 'SomeFutureKey': 'preserved'}, 'enabled': True}
        request = self.request(field='ipv6', expected='Automatic', desired='Off')
        self.assertEqual(self.perform(request)['state'], 'configured')
        self.assertFalse(self.preferences.snapshot['enabled'])
        self.assertEqual(self.preferences.snapshot['configuration']['ConfigMethod'], 'Automatic')
        self.assertEqual(self.perform(self.restore(request))['state'], 'configured')
        self.assertTrue(self.preferences.snapshot['enabled'])
        self.assertEqual(self.preferences.snapshot['configuration']['SomeFutureKey'], 'preserved')

    def test_commit_apply_and_readback_failures_never_report_configured(self):
        request = self.request()
        self.preferences.apply_ok = False
        result = self.perform(request)
        self.assertEqual(result['state'], 'needs_verification')
        self.assertFalse(result['applied'])
        self.assertEqual(self.perform(request), result)
        self.assertEqual(self.preferences.writes, 1)
        self.assertEqual(self.perform(self.request(expected=request['desired']))['error'], 'interrupted_operation')

    def test_commit_failure_and_different_readback_are_not_success(self):
        self.preferences.persist = self.preferences.apply_ok = False
        request = self.request()
        result = self.perform(request)
        self.assertEqual(result['state'], 'needs_verification')
        self.assertEqual(result['actual'], request['expected'])
        self.assertEqual(self.perform(request), result)
        self.assertEqual(self.preferences.writes, 1)

    def test_changed_readback_is_preserved_and_blocks_further_writes(self):
        self.preferences.external = {'ServerAddresses': ['9.9.9.9']}
        request = self.request()
        result = self.perform(request)
        self.assertEqual(result['state'], 'needs_verification')
        self.assertEqual(result['actual'], ['9.9.9.9'])
        self.assertEqual(self.perform(self.restore(request))['error'], 'configuration_changed')
        self.assertEqual(self.perform(self.request(expected=['9.9.9.9']))['error'], 'interrupted_operation')
        self.assertEqual(self.preferences.writes, 1)

    @unittest.skipIf(os.geteuid() == 0, 'Permission failure requires an ordinary user')
    def test_journal_intent_failure_prevents_commit(self):
        self.directory.chmod(0o500)
        try:
            self.assertEqual(self.perform(self.request())['error'], 'journal_unavailable')
            self.assertEqual(self.preferences.writes, 0)
        finally:
            self.directory.chmod(0o700)

    def test_journal_directory_must_be_private_and_not_a_link(self):
        child = self.directory / 'unsafe'
        child.mkdir(mode=0o755)
        with self.assertRaises(Exception):
            self.executor_class.alloc().initWithDirectory_preferences_(str(child), self.preferences)
        link = self.directory / 'alias'
        link.symlink_to(self.directory)
        with self.assertRaises(Exception):
            self.executor_class.alloc().initWithDirectory_preferences_(str(link), self.preferences)

    def test_scope_keeps_exact_platform_service_identity(self):
        from relay.trust import scope_for
        _mac, agent, _fix = self.build_agent()
        run = agent.propose(['dns_mixed_on_vpn'])
        binding = {'id': 'synthetic-profile', 'revision': 1}
        run.snapshot['health_profile'] = binding
        for proposal in run.proposals:
            for action in proposal.actions:
                action['health_profile'] = binding
        original = scope_for(run)
        run.proposals[0].actions[0]['platform_target']['service_id'] = 'REPLACED'
        self.assertNotEqual(scope_for(run), original)
        run.proposals[0].actions[0]['platform_target']['interface'] = 'en9'
        with self.assertRaises(ValueError):
            scope_for(run)

    def test_unknown_readback_blocks_new_writes_and_restart_does_not_replay(self):
        request = self.request()
        self.preferences.fail_readback = True
        self.assertIn('error', self.perform(request))
        self.preferences.fail_readback = False
        self.executor = None
        self.executor = self.executor_class.alloc().initWithDirectory_preferences_(self.temp.name, self.preferences)
        self.assertEqual(self.perform(request)['error'], 'interrupted_operation')
        self.assertEqual(self.perform(self.request(expected=['1.1.1.1']))['error'], 'interrupted_operation')
        self.assertEqual(self.preferences.writes, 1)
        self.assertEqual(self.perform(self.restore(request))['state'], 'configured')
        self.assertEqual(self.perform(self.request())['state'], 'configured')

    def test_corrupt_linked_or_permissive_records_refuse_writes(self):
        request = self.request()
        path = self.directory / f"501-42-{request['id']}.json"
        path.symlink_to(self.directory / 'missing')
        self.assertIn('error', self.perform(request))
        path.unlink()
        path.write_text('{}')
        path.chmod(0o644)
        self.assertIn('error', self.perform(request))
        path.chmod(0o600)
        path.write_text('not json')
        self.assertIn('error', self.perform(request))
        self.assertEqual(self.preferences.writes, 0)

    def test_other_client_cannot_write_while_operation_is_in_progress(self):
        self.preferences.entered, self.preferences.release = threading.Event(), threading.Event()
        results = []
        thread = threading.Thread(target=lambda: results.append(self.perform(self.request())))
        thread.start()
        try:
            self.assertTrue(self.preferences.entered.wait(2))
            self.assertEqual(self.perform(self.request(), user=502)['error'], 'executor_busy')
        finally:
            self.preferences.release.set()
            thread.join(5)
        self.assertEqual(results[0]['state'], 'configured')
        self.assertEqual(self.preferences.writes, 1)

    def test_no_system_account_or_unknown_session(self):
        for user, session in ((0, 42), (500, 42), (501, 0), (501, 2**32 - 1)):
            self.assertEqual(self.perform(self.request(), user, session)['error'], 'invalid_user_session')
        self.assertEqual(self.preferences.writes, 0)

    def build_agent(self):
        from relay.agent import NetworkAssuranceAgent
        from relay.agent_store import AgentStore
        from relay.engine import DetectionEngine, FixEngine
        from relay.mutations import MacMutationWriter
        from test_diagnostics import FakeMac, config
        mac = FakeMac()
        configuration = config(mac, company=True)
        self.preferences.snapshot['configuration']['ServerAddresses'] = list(mac.manual_dns)
        def committed(snapshot):
            mac.manual_dns = list(snapshot['configuration'].get('ServerAddresses', []))
            mac.has_applied = True
        self.preferences.on_commit = committed
        writer = MacMutationWriter(self.perform, lambda _name, _interface: dict(self.target))
        engine = DetectionEngine(configuration, runner=mac)
        fix = FixEngine(configuration, engine, snapshot_dir=self.directory / 'recovery', mutation_writer=writer)
        store = AgentStore(self.directory / 'agent')
        self.addCleanup(store.close)
        agent = NetworkAssuranceAgent(configuration, engine, fix, store=store,
            execution_directory=self.directory / 'execution')
        return mac, agent, fix

    def test_agent_grant_native_write_readback_and_linked_receipts(self):
        mac, agent, _fix = self.build_agent()
        run = agent.propose(['dns_mixed_on_vpn'])
        self.assertEqual(run.proposals[0].actions[0]['platform_target'], self.target)
        with self.assertRaises(PermissionError):
            agent.execute(run)
        self.assertEqual(self.preferences.writes, 0)
        grant = agent.authorize(run)
        agent.execute(run, grant)
        self.assertEqual(run.outcome, 'verified')
        self.assertEqual(self.preferences.writes, 1)
        self.assertEqual(mac.mutations, [], 'Typed writes must not also run networksetup setters')
        recovery = json.loads(Path(run.receipt['recovery_path']).read_text())
        receipt = recovery['system_mutations']['receipts'][0]
        self.assertEqual(recovery['system_mutations']['proposal_hash'], grant.proposal_hash)
        self.assertEqual(receipt['state'], 'configured')
        self.assertFalse(receipt['network_verified'])
        native = json.loads((self.directory / f"501-42-{receipt['id']}.json").read_text())
        self.assertEqual(native['request']['proposal'], grant.proposal_hash)

    def test_agent_network_regression_uses_bound_native_restore(self):
        mac, agent, _fix = self.build_agent()
        original = list(mac.manual_dns)
        mac.break_network_after_apply = True
        run = agent.propose(['dns_mixed_on_vpn'])
        agent.execute(run, agent.authorize(run))
        self.assertEqual(run.outcome, 'rolled_back')
        self.assertEqual(mac.manual_dns, original)
        self.assertEqual(self.preferences.writes, 2)
        recovery = json.loads(Path(run.receipt['recovery_path']).read_text())
        self.assertEqual(len(recovery['system_mutations']['receipts']), 2)
        receipts = [json.loads(path.read_text()) for path in self.directory.glob('*.json')]
        restore = next(item for item in receipts if item['request']['restore_of'])
        self.assertEqual(restore['request']['restore_of'], recovery['system_mutations']['receipts'][0]['id'])

    def test_same_named_recreated_service_invalidates_authorized_proposal(self):
        _mac, agent, _fix = self.build_agent()
        run = agent.propose(['dns_mixed_on_vpn'])
        grant = agent.authorize(run)
        self.target['service_id'] = 'RECREATED-SERVICE'
        agent.execute(run, grant)
        self.assertEqual(run.outcome, 'blocked')
        self.assertEqual(self.preferences.writes, 0)


if __name__ == '__main__':
    unittest.main()
