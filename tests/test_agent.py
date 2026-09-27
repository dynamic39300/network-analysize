"""Network assurance Agent tests: PRD-002 loop without background guard side effects."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code'))
from relay.agent import NetworkAssuranceAgent
from relay.engine import DetectionEngine, FixEngine
from test_diagnostics import FakeMac, config


class NetworkAssuranceAgentTests(unittest.TestCase):
    def make_agent(self, company=True):
        mac = FakeMac()
        profile = config(mac, company=company)
        engine = DetectionEngine(profile, runner=mac)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        fix = FixEngine(profile, engine, snapshot_dir=temp.name)
        agent = NetworkAssuranceAgent(profile, engine, fix)
        return mac, agent

    def test_propose_requires_authorization_and_does_not_mutate_network(self):
        mac, agent = self.make_agent(company=True)
        run = agent.propose(['dns_mixed_on_vpn'])
        self.assertEqual(run.stage, 'awaiting_authorization')
        self.assertEqual(run.proposals[0].issue_types, ('dns_mixed_on_vpn',))
        self.assertIn('逐次授权', run.events[-1]['message'])
        self.assertEqual(mac.mutations, [])
        self.assertEqual(mac.manual_dns, ['8.8.8.8'])

    def test_execute_uses_mature_repair_and_reports_verified_outcome(self):
        mac, agent = self.make_agent(company=True)
        run = agent.propose(['dns_mixed_on_vpn'])
        run = agent.execute(run, agent.authorize(run))
        self.assertEqual(run.stage, 'finished')
        self.assertEqual(run.outcome, 'verified')
        self.assertEqual(mac.manual_dns, ['10.0.0.66', '10.0.0.68'])
        self.assertTrue(any('处理结束' in event['message'] for event in run.events))

    def test_service_denial_is_not_treated_as_local_repair(self):
        mac, agent = self.make_agent(company=False)
        mac.http_status = '403'
        run = agent.propose()
        self.assertEqual(run.stage, 'blocked')
        self.assertEqual(run.outcome, 'blocked')
        self.assertEqual(run.stop_reason, 'no_mature_repair')
        self.assertEqual(mac.mutations, [])
        self.assertTrue(any(issue[1].startswith('service_') for issue in run.issues))

    def test_same_problem_reuses_incident_until_health_restores(self):
        _, agent = self.make_agent(company=True)
        first = agent.observe()
        second = agent.observe()
        self.assertTrue(first.incident_id)
        self.assertEqual(first.incident_id, second.incident_id)
        proposed = agent.propose(['dns_mixed_on_vpn'])
        restored = agent.execute(proposed, agent.authorize(proposed))
        self.assertEqual(restored.outcome, 'verified')
        self.assertEqual(agent.assess(restored.snapshot)['incident_id'], '')


if __name__ == '__main__':
    unittest.main()
