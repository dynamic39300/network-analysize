"""Authorization, changed conditions and crash recovery at the real repair boundary."""
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code'))
from relay.agent import NetworkAssuranceAgent
from relay.agent_store import AgentStore
from relay.engine import DetectionEngine, FixEngine
from test_diagnostics import FakeMac, config


class AgentAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.mac = FakeMac()
        self.config = config(self.mac, company=True)
        self.engine = DetectionEngine(self.config, runner=self.mac)
        self.fix = FixEngine(self.config, self.engine, snapshot_dir=Path(self.temp.name) / 'recovery')
        self.store = AgentStore(Path(self.temp.name) / 'agent')
        self.addCleanup(self.store.close)
        self.now = 1000
        self.agent = NetworkAssuranceAgent(self.config, self.engine, self.fix,
                                           store=self.store, clock=lambda: self.now)

    def propose(self):
        return self.agent.propose(['dns_mixed_on_vpn'])

    def test_no_grant_or_forged_grant_cannot_write(self):
        run = self.propose()
        with self.assertRaises(PermissionError):
            self.agent.execute(run)
        grant = self.agent.authorize(run)
        with self.assertRaises(PermissionError):
            self.agent.execute(run, replace(grant, id='forged'))
        self.assertEqual(self.mac.mutations, [])

    def test_plan_modified_before_or_after_approval_cannot_write(self):
        run = self.propose()
        run.proposals[0].actions[0]['desired'] = ['203.0.113.8']
        with self.assertRaises(ValueError):
            self.agent.authorize(run)
        run = self.propose()
        grant = self.agent.authorize(run)
        run.issues = (('medium', 'proxy_leftover', 'changed'),)
        with self.assertRaises(PermissionError):
            self.agent.execute(run, grant)
        self.assertEqual(self.mac.mutations, [])

    def test_expiry_revocation_and_replay_are_rejected(self):
        run = self.propose()
        grant = self.agent.authorize(run)
        self.now += 301
        with self.assertRaises(PermissionError):
            self.agent.execute(run, grant)
        run = self.propose()
        grant = self.agent.authorize(run)
        self.agent.revoke(grant)
        with self.assertRaises(PermissionError):
            self.agent.execute(run, grant)
        self.assertEqual(self.mac.mutations, [])
        run = self.propose()
        grant = self.agent.authorize(run)
        self.agent.execute(run, grant)
        count = len(self.mac.mutations)
        with self.assertRaises(PermissionError):
            self.agent.execute(run, grant)
        self.assertEqual(len(self.mac.mutations), count)

    def test_changed_profile_target_is_not_implicitly_authorized(self):
        run = self.propose()
        grant = self.agent.authorize(run)
        self.config.config['vpn']['company_dns'] = ['10.1.0.53']
        self.agent.execute(run, grant)
        self.assertEqual(run.outcome, 'blocked')
        self.assertEqual(self.mac.mutations, [])

    def test_changed_existing_configuration_requires_new_plan(self):
        run = self.propose()
        grant = self.agent.authorize(run)
        self.mac.manual_dns = ['8.8.8.8', '1.1.1.1']
        self.agent.execute(run, grant)
        self.assertEqual(run.outcome, 'blocked')
        self.assertEqual(self.mac.mutations, [])

    def test_revocation_during_preflight_blocks_first_write(self):
        run = self.propose()
        grant = self.agent.authorize(run)
        def progress(event):
            if event['phase'] == 'applying':
                self.agent.revoke(grant)
        self.agent.execute(run, grant, progress)
        self.assertEqual(run.outcome, 'blocked')
        self.assertEqual(self.mac.mutations, [])

    def test_journal_failure_prevents_network_write(self):
        run = self.propose()
        grant = self.agent.authorize(run)
        with patch.object(self.store, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.agent.execute(run, grant)
        self.assertEqual(self.mac.mutations, [])

    def test_receipt_links_exact_proposal_and_durable_recovery_data(self):
        run = self.propose()
        grant = self.agent.authorize(run)
        self.agent.execute(run, grant)
        stored = self.store.recent()[0]
        self.assertEqual(stored['outcome'], 'verified')
        self.assertEqual(stored['receipt']['proposal_hash'], grant.proposal_hash)
        self.assertTrue(Path(stored['receipt']['recovery_path']).exists())
        self.assertEqual(stored['receipt']['actions'][0]['desired'], ['10.0.0.66', '10.0.0.68'])

    def test_restart_invalidates_pending_approval_without_replaying_commands(self):
        run = self.propose()
        self.agent.authorize(run)
        recovered = self.store.recover_interrupted()
        self.assertEqual(recovered[0]['stage'], 'cancelled')
        restarted = NetworkAssuranceAgent(self.config, self.engine, self.fix, store=self.store)
        observed = restarted.observe()
        self.assertEqual(observed.incident_id, run.incident_id)
        self.assertEqual(self.mac.mutations, [])

    def test_abrupt_exit_after_write_preserves_reconciliation_and_before_values(self):
        run = self.propose()
        grant = self.agent.authorize(run)
        write = self.fix._write_field
        def interrupted(*args):
            write(*args)
            raise SystemExit('simulated abrupt exit')
        with patch.object(self.fix, '_write_field', side_effect=interrupted):
            with self.assertRaises(SystemExit):
                self.agent.execute(run, grant)
        stored = self.store.recent()[0]
        self.assertEqual(stored['stage'], 'needs_reconciliation')
        import json
        recovery = json.loads(Path(stored['receipt']['recovery_path']).read_text())
        self.assertEqual(recovery['before']['dns'], ['8.8.8.8'])
        self.assertEqual(recovery['phase'], 'applying')
        mutations = list(self.mac.mutations)
        self.store.recover_interrupted()
        self.assertEqual(self.mac.mutations, mutations)

    def test_unsafe_storage_permissions_are_rejected(self):
        self.store.path.chmod(0o644)
        with self.assertRaises(PermissionError):
            AgentStore(self.store.directory)

    def test_second_runtime_cannot_take_over_live_tasks(self):
        self.propose()
        with self.assertRaises(BlockingIOError):
            AgentStore(self.store.directory)
        self.assertEqual(self.store.recent()[0]['stage'], 'awaiting_authorization')

    def test_restart_after_unhandled_process_death_blocks_new_writes(self):
        run = self.propose()
        self.agent.authorize(run)
        run.stage = 'executing'
        self.store.save(run)
        self.store.close()
        reopened = AgentStore(self.store.directory)
        self.addCleanup(reopened.close)
        recovered = reopened.recover_interrupted()
        self.assertEqual(recovered[0]['stage'], 'needs_reconciliation')
        restarted = NetworkAssuranceAgent(self.config, self.engine, self.fix, store=reopened)
        proposed = restarted.propose(['dns_mixed_on_vpn'])
        with self.assertRaises(PermissionError):
            restarted.authorize(proposed)
        self.assertEqual(self.mac.mutations, [])

    def test_reconciliation_verifies_completed_write_without_reexecuting_it(self):
        run = self.propose()
        grant = self.agent.authorize(run)
        write = self.fix._write_field
        def interrupted(*args):
            write(*args)
            raise SystemExit()
        with patch.object(self.fix, '_write_field', side_effect=interrupted):
            with self.assertRaises(SystemExit):
                self.agent.execute(run, grant)
        restarted = NetworkAssuranceAgent(self.config, self.engine, self.fix, store=self.store)
        mutations = list(self.mac.mutations)
        restarted.observe()
        self.assertFalse(restarted.recovery_pending)
        stored = next(r for r in self.store.recent() if r['id'] == run.id)
        self.assertEqual(stored['reconciliation']['outcome'], 'verified')
        self.assertEqual(self.mac.mutations, mutations)

    def test_reconciliation_preserves_external_change_and_blocks_conflicting_work(self):
        run = self.propose()
        grant = self.agent.authorize(run)
        write = self.fix._write_field
        def interrupted(*args):
            write(*args)
            raise SystemExit()
        with patch.object(self.fix, '_write_field', side_effect=interrupted):
            with self.assertRaises(SystemExit):
                self.agent.execute(run, grant)
        self.mac.manual_dns = ['192.0.2.1']
        restarted = NetworkAssuranceAgent(self.config, self.engine, self.fix, store=self.store)
        mutations = list(self.mac.mutations)
        restarted.observe()
        self.assertTrue(restarted.recovery_pending)
        self.assertEqual(self.mac.manual_dns, ['192.0.2.1'])
        self.assertEqual(self.mac.mutations, mutations)

    def test_redacted_records_do_not_export_raw_snapshots_or_recovery_paths(self):
        from relay.agent_records import records_report, records_summary
        from relay.commercial.history import redact
        run = self.propose()
        self.agent.execute(run, self.agent.authorize(run))
        report = records_report(self.store.recent(), redact)
        summary = records_summary(report)
        self.assertIn('已验证恢复', summary)
        self.assertNotIn('10.0.0.66', summary)
        self.assertNotIn('snapshot', report['records'][0])
        self.assertNotIn('recovery_path', report['records'][0]['receipt'])
        fallback = records_report(self.store.recent(), None)
        self.assertEqual(set(fallback['records'][0]), {'stage', 'outcome', 'events'})

    def test_retention_bounds_finished_history_and_preserves_unresolved_recovery(self):
        import time
        from dataclasses import asdict
        from relay.agent_store import encode
        run = self.propose()
        payload = asdict(run)
        payload.update(stage='finished', outcome='verified')
        now = time.time()
        with self.store._connect() as db:
            db.executemany('INSERT INTO runs VALUES (?, ?, ?, ?, ?)',
                           [(f'history-{i}', now, 'finished', '', encode({**payload, 'id': f'history-{i}'}))
                            for i in range(1005)])
            db.execute('INSERT INTO runs VALUES (?, ?, ?, ?, ?)',
                       ('unresolved', now - 40 * 86400, 'needs_reconciliation', '',
                        encode({**payload, 'id': 'unresolved', 'stage': 'needs_reconciliation'})))
        self.agent.cancel(run)
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 1001)
        self.assertTrue(self.store.needs_reconciliation())


if __name__ == '__main__':
    unittest.main()
