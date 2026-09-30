"""Bounded guard scheduling, independent of the desktop event loop."""
from collections import deque
from dataclasses import dataclass
from pathlib import PureWindowsPath
import threading
import time

from .commands import CommandResult


@dataclass(frozen=True)
class GuardPolicy:
    settle_seconds: float = 5
    maximum_settle_seconds: float = 30
    minimum_interval: float = 60
    healthy_interval: float = 300
    maximum_interval: float = 1800
    hourly_checks: int = 12
    scan_seconds: float = 60
    scan_commands: int = 80
    scan_requests: int = 8
    response_bytes: int = 65536


@dataclass(frozen=True)
class GuardTicket:
    generation: int
    sequence: int
    reason: str


class GuardSchedule:
    def __init__(self, policy=None, clock=time.monotonic):
        self.policy, self.clock = policy or GuardPolicy(), clock
        self.lock = threading.RLock()
        self.enabled = False
        self.generation = self.sequence = self.failures = 0
        self.next_due = None
        self.change_at = None
        self.first_change_at = None
        self.last_start = None
        self.active = None
        self.starts = deque()
        self.reason = 'enabled'
        self.last_metrics = {}

    def enable(self):
        with self.lock:
            if not self.enabled:
                self.enabled = True
                self.generation += 1
                self.next_due = self.clock() + self.policy.settle_seconds
                self.change_at = None
                self.first_change_at = None
                self.reason = 'enabled'

    def restore_ages(self, ages):
        with self.lock:
            now = self.clock()
            self.starts = deque((now - age, -(index + 1)) for index, age in enumerate(ages) if 0 <= age < 3600)
            self.last_start = self.starts[-1][0] if self.starts else None

    def pause(self):
        with self.lock:
            self.enabled = False
            self.generation += 1
            self.next_due = self.change_at = None
            self.first_change_at = None

    def changed(self):
        with self.lock:
            if self.enabled:
                if self.first_change_at is None:
                    self.first_change_at = self.clock()
                self.change_at = self.clock()
                self.reason = 'network_changed'

    def valid(self, ticket):
        with self.lock:
            return self.enabled and ticket.generation == self.generation and self.active == ticket

    def _due(self, now):
        while self.starts and now - self.starts[0][0] >= 3600:
            self.starts.popleft()
        due = self.next_due if self.next_due is not None else now
        if self.change_at is not None:
            due = min(self.change_at + self.policy.settle_seconds,
                      self.first_change_at + self.policy.maximum_settle_seconds)
        if self.last_start is not None:
            due = max(due, self.last_start + self.policy.minimum_interval)
        if len(self.starts) >= self.policy.hourly_checks:
            due = max(due, self.starts[0][0] + 3600)
        return due

    def claim(self):
        with self.lock:
            now = self.clock()
            if not self.enabled or self.active or now < self._due(now):
                return None
            self.sequence += 1
            ticket = GuardTicket(self.generation, self.sequence, self.reason)
            self.active = ticket
            self.starts.append((now, ticket.sequence))
            self.last_start = now
            self.change_at = None
            self.first_change_at = None
            return ticket

    def deferred(self, ticket):
        with self.lock:
            if self.active != ticket:
                return
            self.active = None
            self.starts = deque(item for item in self.starts if item[1] != ticket.sequence)
            self.last_start = self.starts[-1][0] if self.starts else None
            if self.enabled and ticket.generation == self.generation:
                self.next_due = self.clock()
                self.reason = ticket.reason

    def complete(self, ticket, failed=False, metrics=None):
        with self.lock:
            if self.active != ticket:
                return
            self.active = None
            self.last_metrics = metrics or {}
            if not self.enabled or ticket.generation != self.generation:
                return
            self.failures = min(8, self.failures + 1) if failed else 0
            interval = (min(self.policy.maximum_interval,
                            self.policy.minimum_interval * 2 ** (self.failures - 1))
                        if failed else self.policy.healthy_interval)
            self.next_due = self.clock() + interval
            if self.change_at is None:
                self.reason = 'retry' if failed else 'health_check'

    def state(self):
        with self.lock:
            now = self.clock()
            due = self._due(now) if self.enabled else None
            return {'enabled': self.enabled, 'phase': 'pausing' if self.active and not self.enabled else 'checking' if self.active else
                    'waiting' if self.enabled else 'paused',
                    'next_check_in': max(0, due - now) if due is not None else None,
                    'checks_last_hour': len(self.starts), 'hourly_limit': self.policy.hourly_checks,
                    'failures': self.failures, 'last_metrics': dict(self.last_metrics)}


class ProbeBudget:
    def __init__(self, policy, allowed=lambda: True, clock=time.monotonic):
        self.policy, self.allowed, self.clock = policy, allowed, clock
        self.started = clock()
        self.paused_at = None
        self.wait_seconds = 0
        self.commands = self.requests = 0
        self.stop_reason = ''

    def pause_time(self):
        if self.paused_at is None:
            self.paused_at = self.clock()

    def resume_time(self):
        if self.paused_at is not None:
            self.wait_seconds += self.clock() - self.paused_at
            self.paused_at = None

    def elapsed(self):
        return (self.paused_at if self.paused_at is not None else self.clock()) - self.started - self.wait_seconds

    def run(self, runner, argv, timeout=10):
        if self.paused_at is not None:
            raise RuntimeError('调查正在等待外部动作，不能追加探测')
        remaining = self.policy.scan_seconds - self.elapsed()
        is_request = PureWindowsPath(argv[0]).name.lower() in ('curl', 'curl.exe')
        if not self.allowed():
            self.stop_reason = 'paused'
        elif remaining <= 0 or self.commands >= self.policy.scan_commands:
            self.stop_reason = 'scan_budget'
        elif is_request and self.requests >= self.policy.scan_requests:
            self.stop_reason = 'request_budget'
        if self.stop_reason:
            raise RuntimeError('守护探测已暂停或达到本次预算')
        args = list(argv)
        if is_request:
            self.requests += 1
            index = 2 if len(args) > 1 and args[1] == '--disable' else 1
            args[index:index] = ['--max-filesize', str(self.policy.response_bytes)]
        self.commands += 1
        result = runner(args, timeout=min(timeout, remaining))
        # A size-limited body still provides valid HTTP reachability evidence.
        # The diagnostics never claim to validate response-body correctness.
        status = result.stdout.strip().split()[:1]
        size_limited = (result.returncode == 63 or result.returncode == 56
                        and 'Exceeded the maximum allowed file size' in result.stderr)
        if (is_request and size_limited and not result.timed_out
                and status and status[0].isdigit() and 100 <= int(status[0]) <= 599):
            return CommandResult(result.stdout, 'response body limited by guard budget', 0)
        return result

    def metrics(self):
        return {'commands': self.commands, 'requests': self.requests,
                'elapsed_seconds': round(self.elapsed(), 3),
                'wall_seconds': round(self.clock() - self.started, 3),
                'wait_seconds': round(self.clock() - self.started - self.elapsed(), 3),
                'response_body_budget': self.requests * self.policy.response_bytes,
                'stop_reason': self.stop_reason}


class GuardService:
    def __init__(self, submit, on_state, policy=None, clock=time.monotonic, threaded=True):
        self.schedule = GuardSchedule(policy, clock)
        self.submit, self.on_state = submit, on_state
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.thread = None
        if threaded:
            self.thread = threading.Thread(target=self._loop, name='NetCare guard scheduler', daemon=True)
            self.thread.start()

    def _notify(self):
        try:
            self.on_state()
        except Exception:
            pass

    def enable(self):
        self.schedule.enable()
        self._notify()
        self.wake.set()

    def pause(self):
        self.schedule.pause()
        self._notify()
        self.wake.set()

    def changed(self):
        self.schedule.changed()
        self.wake.set()

    def tick(self):
        ticket = self.schedule.claim()
        if ticket:
            try:
                accepted = self.submit(ticket)
            except Exception:
                accepted = False
            if not accepted:
                self.schedule.deferred(ticket)
            self._notify()

    def _loop(self):
        while not self.stopped.is_set():
            self.tick()
            state = self.schedule.state()
            delay = max(1, min(60, state['next_check_in'])) if state['enabled'] else 60
            self.wake.wait(delay)
            self.wake.clear()

    def complete(self, ticket, failed=False, metrics=None):
        self.schedule.complete(ticket, failed, metrics)
        self._notify()
        self.wake.set()

    def close(self):
        self.stopped.set()
        self.schedule.pause()
        self.wake.set()
