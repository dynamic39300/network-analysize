"""Guard budgets and lifecycle with virtual time and simulated network changes."""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code'))
from relay.agent import NetworkAssuranceAgent
from relay.commands import CommandResult, run_command
from relay.engine import DetectionEngine, FixEngine
from relay.guard import GuardPolicy, GuardSchedule, GuardService, ProbeBudget
from test_diagnostics import FakeMac, config


class Clock:
    def __init__(self):
        self.now = 0.0
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += seconds


class GuardScheduleTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.schedule = GuardSchedule(clock=self.clock)

    def first(self):
        self.schedule.enable()
        self.clock.advance(5)
        ticket = self.schedule.claim()
        self.assertIsNotNone(ticket)
        return ticket

    def test_disabled_guard_ignores_events_and_time(self):
        for _ in range(100):
            self.schedule.changed()
            self.clock.advance(3600)
            self.assertIsNone(self.schedule.claim())
        self.assertEqual(self.schedule.state()['checks_last_hour'], 0)

    def test_debounce_then_cooldown_merges_network_event_burst(self):
        self.schedule.enable()
        self.clock.advance(4)
        self.schedule.changed()
        self.clock.advance(4)
        self.schedule.changed()
        self.assertIsNone(self.schedule.claim())
        self.clock.advance(5)
        ticket = self.schedule.claim()
        self.assertEqual(ticket.reason, 'network_changed')
        self.schedule.complete(ticket)
        for _ in range(5):
            self.clock.advance(3)
            self.schedule.changed()
            self.assertIsNone(self.schedule.claim())
        self.clock.advance(45)
        self.assertIsNotNone(self.schedule.claim())

    def test_endless_events_cannot_postpone_checks_forever(self):
        self.schedule.enable()
        for _ in range(31):
            self.schedule.changed()
            self.clock.advance(1)
        self.assertIsNotNone(self.schedule.claim())

    def test_offline_retries_back_off_and_health_resets_interval(self):
        ticket = self.first()
        for expected in (60, 120, 240, 480, 960, 1800):
            self.schedule.complete(ticket, failed=True)
            self.assertEqual(self.schedule.state()['next_check_in'], expected)
            self.clock.advance(expected)
            ticket = self.schedule.claim()
            self.assertIsNotNone(ticket)
        self.schedule.complete(ticket, failed=False)
        self.assertEqual(self.schedule.state()['next_check_in'], 300)

    def test_pause_invalidates_queued_work_and_resume_cannot_overlap_it(self):
        ticket = self.first()
        self.schedule.pause()
        self.assertFalse(self.schedule.valid(ticket))
        self.schedule.enable()
        self.clock.advance(120)
        self.assertIsNone(self.schedule.claim())
        self.schedule.complete(ticket)
        new = self.schedule.claim()
        self.assertIsNotNone(new)
        self.assertNotEqual(ticket.generation, new.generation)

    def test_hourly_budget_survives_pause_resume_and_event_storm(self):
        self.schedule = GuardSchedule(replace(GuardPolicy(), hourly_checks=3), self.clock)
        self.first()
        for _ in range(2):
            self.schedule.complete(self.schedule.active)
            self.schedule.pause()
            self.schedule.enable()
            self.clock.advance(60)
            self.assertIsNotNone(self.schedule.claim())
        self.schedule.complete(self.schedule.active)
        self.schedule.changed()
        self.clock.advance(60)
        self.assertIsNone(self.schedule.claim())
        self.clock.now = 3605
        self.assertIsNotNone(self.schedule.claim())

    def test_busy_worker_defers_without_spending_scan_budget(self):
        service = GuardService(lambda ticket: False, lambda: None, clock=self.clock, threaded=False)
        service.enable()
        self.clock.advance(5)
        for _ in range(20):
            service.tick()
        self.assertEqual(service.schedule.state()['checks_last_hour'], 0)
        self.assertIsNone(service.schedule.active)

    def test_busy_worker_does_not_lose_an_early_network_change(self):
        ticket = self.first()
        self.schedule.complete(ticket)
        self.clock.advance(60)
        self.schedule.changed()
        self.clock.advance(5)
        ticket = self.schedule.claim()
        self.schedule.deferred(ticket)
        self.assertIsNotNone(self.schedule.claim())

    def test_unavailable_ui_does_not_stop_guard_scheduling(self):
        accepted = []
        def closed_ui():
            raise RuntimeError('window closed')
        service = GuardService(lambda ticket: accepted.append(ticket) or True, closed_ui,
                               clock=self.clock, threaded=False)
        service.enable()
        self.clock.advance(5)
        service.tick()
        self.assertEqual(len(accepted), 1)
        service.complete(accepted[0])
        self.assertTrue(service.schedule.state()['enabled'])
        service.close()

    def test_restart_restores_hourly_budget_from_private_journal(self):
        from relay.agent_store import AgentStore
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / 'agent')
            for _ in range(12):
                self.assertTrue(store.reserve_guard_check(12))
            self.assertFalse(store.reserve_guard_check(12))
            store.close()
            restored = AgentStore(Path(directory) / 'agent')
            try:
                self.schedule.restore_ages(restored.guard_check_ages())
                self.schedule.enable()
                self.clock.advance(60)
                self.assertIsNone(self.schedule.claim())
                self.assertEqual(self.schedule.state()['checks_last_hour'], 12)
            finally:
                restored.close()


class GuardBudgetTests(unittest.TestCase):
    def test_pause_during_real_diagnostic_loop_prevents_further_commands(self):
        mac, calls, allowed = FakeMac(), [], [True]
        def runner(argv, timeout=10):
            calls.append(argv)
            result = mac(argv, timeout)
            allowed[0] = False
            return result
        engine = DetectionEngine(config(mac), runner=runner)
        budget = ProbeBudget(GuardPolicy(), allowed=lambda: allowed[0])
        snapshot = engine.run_all(budget=budget)
        self.assertEqual(len(calls), 1)
        self.assertEqual(budget.stop_reason, 'paused')
        self.assertTrue(snapshot['check_errors'])
        self.assertEqual(mac.mutations, [])

    def test_command_deadline_and_pause_prevent_additional_subprocesses(self):
        clock, calls = Clock(), []
        allowed = [True]
        budget = ProbeBudget(replace(GuardPolicy(), scan_seconds=10, scan_commands=2),
                             allowed=lambda: allowed[0], clock=clock)
        def runner(argv, timeout):
            calls.append(timeout)
            return CommandResult('ok')
        budget.run(runner, ['/bin/test'], timeout=20)
        clock.advance(8)
        budget.run(runner, ['/bin/test'], timeout=20)
        with self.assertRaises(RuntimeError):
            budget.run(runner, ['/bin/test'])
        self.assertEqual(calls, [10, 2])
        allowed[0] = False
        stopped = ProbeBudget(GuardPolicy(), allowed=lambda: allowed[0])
        with self.assertRaises(RuntimeError):
            stopped.run(runner, ['/bin/test'])
        self.assertEqual(len(calls), 2)

    def test_guard_creates_proposal_without_any_network_mutation(self):
        mac = FakeMac()
        profile = config(mac, company=True)
        engine = DetectionEngine(profile, runner=mac)
        with tempfile.TemporaryDirectory() as directory:
            fix = FixEngine(profile, engine, snapshot_dir=directory)
            agent = NetworkAssuranceAgent(profile, engine, fix)
            budget = ProbeBudget(GuardPolicy())
            run = agent.investigate(budget=budget)
        self.assertEqual(run.stage, 'awaiting_authorization')
        self.assertEqual(mac.mutations, [])
        self.assertLessEqual(budget.commands, 80)
        self.assertLessEqual(budget.requests, 8)
        self.assertTrue(all(check.runner is mac for check in engine.checks))

    def test_excess_targets_are_unknown_and_never_silently_healthy(self):
        mac = FakeMac()
        profile = config(mac)
        profile.config['reachability']['targets'] *= 20
        engine = DetectionEngine(profile, runner=mac)
        budget = ProbeBudget(GuardPolicy())
        result = engine.run_all(budget=budget)
        self.assertEqual(budget.requests, 8)
        self.assertIn('reachability', result['check_errors'])
        self.assertEqual(engine.get_overall_status(), 'unknown')

    def test_large_local_http_response_retains_status_without_downloading_full_body(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', str(2 * 1024 * 1024))
                self.end_headers()
                try:
                    self.wfile.write(b'x' * (2 * 1024 * 1024))
                except (BrokenPipeError, ConnectionResetError):
                    pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            budget = ProbeBudget(GuardPolicy())
            result = budget.run(run_command, ['/usr/bin/curl', '-sS', '--noproxy', '*', '-o', '/dev/null',
                                             '-w', '%{http_code}', '--', f'http://127.0.0.1:{server.server_port}/'])
            self.assertTrue(result.ok)
            self.assertEqual(result.stdout, '200')
            self.assertIn('limited', result.stderr)
            self.assertEqual(budget.requests, 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    unittest.main()
