"""Exact dynamic command proposals and receipts, never inferred read-only authority."""
import copy
from datetime import datetime
import hashlib
import time
from pathlib import Path

from jsonschema import Draft202012Validator

from .agent_store import encode
from .guard import GuardPolicy, ProbeBudget
from .jobs import current_identity, executable_identity, job_environment, run_job
from .private_files import check_private, create_private_directory, execution_directory


REQUEST_SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'argv': {'type': 'array', 'minItems': 1, 'maxItems': 64,
             'items': {'type': 'string', 'minLength': 1, 'maxLength': 4096}},
    'stdin': {'type': 'string', 'maxLength': 32768},
    'purpose': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
    'expected_effect': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
    'recovery_notes': {'type': 'string', 'minLength': 1, 'maxLength': 2000},
    'target_ids': {'type': 'array', 'minItems': 1, 'maxItems': 32, 'uniqueItems': True,
                   'items': {'type': 'string', 'minLength': 1, 'maxLength': 100}},
    'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 30},
    'output_limit': {'type': 'integer', 'minimum': 1024, 'maximum': 65536}}}
REQUEST_SCHEMA['required'] = list(REQUEST_SCHEMA['properties'])
VALIDATOR = Draft202012Validator(REQUEST_SCHEMA)
RISK = ('unrestricted_current_user; no_network_only_sandbox; external_dependencies_unbound; '
        'background_escape_possible; child_privilege_helpers_not_sandboxed; no_generic_automatic_rollback')


def digest(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


class DynamicCommands:
    def __init__(self, agent, *, runner=run_job, allowed=lambda: True, coordination_dir=None):
        self.agent, self.runner, self.allowed = agent, runner, allowed
        self.coordination_dir = str(Path(coordination_dir or execution_directory()).resolve())

    def _context(self, snapshot):
        agent = self.agent
        stamp = snapshot.get('last_check')
        if (not isinstance(stamp, datetime) or not 0 <= time.time() - stamp.timestamp() <= 60
                or snapshot.get('check_errors') or 'target_selection' in snapshot
                or not agent.profiles or snapshot.get('health_profile') != agent.profiles.binding()):
            raise PermissionError('Fresh complete profile evidence is required')
        environment = copy.deepcopy(snapshot.get('environment', {}))
        environment.pop('captured_at', None)
        status = snapshot.get('status', {})
        return {'profile': agent.profiles.binding(), 'environment': environment,
            'status': {key: copy.deepcopy(status.get(key)) for key in (
                'wifi_ip', 'wifi_network', 'default_interface', 'vpn', 'vpn_path', 'vpn_client', 'vpn_evidence',
                'dns_manual_servers', 'dns_effective_servers', 'dns_mode', 'ipv6_mode',
                'proxy_details', 'proxy_constraints', 'proxy_pac')},
            'configuration': {key: copy.deepcopy(agent.config.get(key)) for key in (
                'reachability.targets', 'wifi.service_name', 'vpn.company_dns', 'dns.public_dns',
                'dns.enforce_company_dns_on_vpn', 'ipv6.should_be')}}

    def prepare(self, run, request, origin='user'):
        from .agent import ActionProposal
        agent = self.agent
        if not agent.store or agent.journal_error or agent.safety_error or agent.store.needs_reconciliation():
            raise PermissionError('A healthy journal and reconciled prior operations are required')
        if not self.allowed() or run.stage not in ('observed', 'investigating'):
            raise PermissionError('This task cannot prepare another command')
        VALIDATOR.validate(request)
        if origin not in ('user', 'model') or any('\x00' in arg for arg in request['argv']):
            raise ValueError('Invalid command content')
        if len(encode(request).encode('utf-8')) > 65536:
            raise ValueError('Command document exceeds 64 KiB')
        targets = {target['id'] for target in agent.config.get('reachability.targets', [])}
        if not set(request['target_ids']) <= targets:
            raise ValueError('Command target references are outside the active profile')
        context = self._context(run.snapshot)
        identity = current_identity()
        executable = executable_identity(request['argv'][0])
        job_id = agent.id_factory('job')
        cwd = (agent.store.directory / 'jobs' / job_id).resolve()
        create_private_directory(cwd.parent)
        create_private_directory(cwd)
        receipts = (agent.store.directory / 'job-receipts').resolve()
        create_private_directory(receipts)
        argv = [executable['path'], *request['argv'][1:]]
        spec = {'argv': argv, 'stdin': request['stdin'], 'cwd': str(cwd), 'identity': identity,
                'executable': executable, 'environment': job_environment(cwd), 'timeout': request['timeout'],
                'output_limit': request['output_limit'], 'coordination_dir': self.coordination_dir,
                'receipt_path': str(receipts / (job_id + '.json'))}
        action = {'kind': 'dynamic_command', 'id': job_id, 'origin': origin, 'spec': spec,
                  'request': copy.deepcopy(request), 'context': context, 'risk': RISK,
                  'rollback': 'manual_review_only', 'job_hash': digest(spec)}
        proposal = ActionProposal(agent.id_factory('proposal'), ('dynamic_command',),
            ('动态命令需要查看完整内容并单独确认当前用户权限风险；不保证只影响网络。',),
            (action,), agent.clock() + agent.proposal_ttl,
            authorization='explicit_unrestricted_command',
            impact='Arbitrary current-user effects are possible; declared targets are intent, not confinement.',
            reversible='Pre-execution evidence is saved; generic command effects require manual review, not automatic rollback.')
        run.proposals = (proposal,)
        run.event('已准备动态命令，等待查看完整参数、脚本和风险后单独授权', 'awaiting_authorization')
        agent._proposals[run.id] = agent._proposal_hash(run)
        agent._save(run, required=True)
        return run

    def execute(self, run, grant):
        agent = self.agent
        agent._validate_grant(run, grant)
        if run.stage != 'authorized' or len(run.proposals) != 1 or len(run.proposals[0].actions) != 1:
            raise PermissionError('One authorized dynamic job is required')
        action = copy.deepcopy(run.proposals[0].actions[0])
        spec = action['spec']
        if action['job_hash'] != digest(spec):
            raise PermissionError('Command content changed')
        launch_intent = False
        run.receipt = {'kind': 'dynamic_command', 'job_id': action['id'], 'job_hash': action['job_hash'],
            'grant_id': grant.id, 'proposal_hash': grant.proposal_hash, 'spec': copy.deepcopy(spec),
            'risk_acknowledged': True, 'started_at': agent.clock(), 'finished_at': None,
            'outcome': 'preflight', 'launch_intent': False, 'job': None,
            'recovery': {'automatic': False, 'notes': action['request']['recovery_notes']}}

        def allowed():
            try:
                agent._validate_grant(run, grant)
                return self.allowed()
            except Exception:
                return False

        def ready():
            nonlocal launch_intent
            if not allowed() or agent.store.needs_reconciliation():
                raise PermissionError('Authorization or recovery state changed')
            before = agent.detection_engine.run_all(budget=ProbeBudget(GuardPolicy(), allowed=allowed))
            if self._context(before) != action['context']:
                raise PermissionError('Observed environment or expected configuration changed')
            if current_identity() != spec['identity'] or executable_identity(spec['argv'][0]) != spec['executable']:
                raise PermissionError('Execution identity or executable changed')
            check_private(spec['cwd'], directory=True)
            receipt_path = Path(spec['receipt_path'])
            check_private(receipt_path.parent, directory=True)
            if receipt_path.exists() or receipt_path.is_symlink():
                raise PermissionError('A terminal receipt already exists; this job cannot be replayed')
            if not allowed():
                raise PermissionError('Launch authorization was revoked')
            run.receipt['recovery']['before_snapshot'] = copy.deepcopy(before)
            run.receipt['recovery']['targets'] = agent.profiles.evaluate(before)['targets']
            run.receipt.update(outcome='launch_intent', launch_intent=True)
            run.event('执行前证据已核对；保存启动意图后才允许动态命令启动', 'executing')
            agent._save(run, required=True)
            launch_intent = True

        def started(info):
            run.receipt['job'] = {'state': 'running', **info}
            agent._save(run, required=True)

        try:
            job = self.runner(spec, allowed=allowed, on_ready=ready, on_started=started)
            run.receipt['job'] = job
            if job.get('pid') is None:
                launch_intent = False
            if launch_intent:
                run.event('动态命令已停止；正在只读复查，不将退出码当作恢复证明')
                agent._save(run, required=True)
                after = agent.detection_engine.run_all(budget=ProbeBudget(GuardPolicy(), allowed=self.allowed))
                run.snapshot, run.health = after, agent._health(after)
                run.observations = agent._observations(after, run.targets)
                before = run.receipt['recovery']['before_snapshot']
                targets = {row['id']: row for row in agent.profiles.evaluate(after)['targets']}
                run.receipt['verification'] = {
                    'level': 'network_observations_only', 'health': run.health,
                    'profile_matches': after.get('health_profile') == before.get('health_profile'),
                    'original_issues_remaining': sorted({i[1] for i in before['issues']} & {i[1] for i in after['issues']}),
                    'check_errors': sorted(after.get('check_errors', {})),
                    'protected_target_regressions': [row['id'] for row in run.receipt['recovery']['targets']
                        if row['state'] == 'healthy' and targets.get(row['id'], {}).get('state') != 'healthy'],
                    'command_effects_verified': False}
        except BaseException as exc:
            run.receipt['error'] = 'job_or_preflight_interrupted'
            if not launch_intent:
                run.stop_reason = 'dynamic_preflight_failed'
            else:
                run.stop_reason = 'dynamic_execution_interrupted'
            if not isinstance(exc, Exception):
                raise
        finally:
            with agent._authorization_lock:
                agent._grants.pop(grant.id, None)
                agent._proposals.pop(run.id, None)
            run.outcome = 'needs_review' if launch_intent else 'not_started'
            if run.stop_reason not in ('dynamic_preflight_failed', 'dynamic_execution_interrupted'):
                run.stop_reason = 'dynamic_effects_need_review' if launch_intent else 'dynamic_not_started'
            run.receipt.update(outcome=run.outcome, finished_at=agent.clock())
            run.event('动态命令可能产生副作用；已保留前后证据和收据，需人工核对，未自动回滚' if launch_intent
                      else '动态命令未启动；需要重新检查并确认方案',
                      'needs_reconciliation' if launch_intent else 'finished')
            agent.recovery_pending = agent.recovery_pending or launch_intent
            agent._save(run)
        return run

    def inspect_receipt(self, receipt):
        from .models import strict_json
        path = Path(receipt.get('spec', {}).get('receipt_path', ''))
        expected = (self.agent.store.directory / 'job-receipts' / (receipt.get('job_id', '') + '.json')).resolve()
        if path != expected or expected.parent != (self.agent.store.directory / 'job-receipts').resolve():
            raise PermissionError('Unexpected job receipt path')
        check_private(path.parent, directory=True)
        check_private(path)
        with path.open('rb') as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('Job receipt exceeds limit')
        record = strict_json(raw)
        if (record.get('schema') != 'relay-job-receipt-v1' or record.get('job_hash') != receipt.get('job_hash')
                or receipt.get('job_hash') != digest(receipt['spec']) or not isinstance(record.get('job'), dict)):
            raise ValueError('Job receipt does not match the approved command')
        return record['job']

    def review(self, run_id, receipt_hash, note):
        agent = self.agent
        if not isinstance(note, str) or not note.strip() or len(note) > 2000:
            raise ValueError('A concise explicit human review note is required')
        snapshot = agent.detection_engine.run_all(budget=ProbeBudget(GuardPolicy(), allowed=self.allowed))
        self._context(snapshot)
        if not self.allowed():
            raise PermissionError('Receipt review cancelled')
        agent.store.review_dynamic(run_id, receipt_hash, note, snapshot, current_identity())
        agent.recovery_pending = agent.store.needs_reconciliation()
        return {'run_id': run_id, 'outcome': 'reviewed', 'health': agent._health(snapshot),
                'recovery_pending': agent.recovery_pending, 'command_effects_verified': False}
