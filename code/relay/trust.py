"""Bounded authorization for known repairs, never a model-provided risk judgment."""
import copy
import hashlib
import ipaddress
import threading
import time
import uuid

from .agent_store import encode

CONTRACT = 'relay-repair-trust-v1'
MODES = {'task': {'seconds': 900, 'limit': 3}, 'continuous': {'seconds': 28800, 'limit': 12}}
REASONS = {'not_granted': '尚无范围信任', 'revoked': '已撤销', 'core_restarted': '核心重启，需重新审阅',
           'core_stopped': '核心已停止，需重新审阅', 'permissions_changed': '权限或配置已变化',
           'user_cancelled': '用户已停止任务', 'expired': '信任已到期', 'exhausted': '执行额度已用尽',
           'different_task': '不属于获准任务', 'scope_changed': '目标、档案或环境超出范围',
           'unsupported_action': '动态命令或未知修改需逐次确认', 'execution_not_verified': '上次执行未验证恢复',
           'task_finished': '获准任务已结束', 'journal_unavailable': '授权记录不可用',
           'recovery_pending': '仍有执行结果待核对', 'cancelled': '当前处理已暂停或停止'}


def scope_for(run):
    """Omit only observed old values; all effect and environment conditions stay exact."""
    if run.snapshot.get('check_errors') or not run.snapshot.get('health_profile'):
        raise ValueError('scope_changed')
    status = run.snapshot.get('status', {})
    vpn = {key: status.get(key) for key in ('vpn', 'vpn_path', 'vpn_client')}
    vpn['evidence'] = {key: copy.deepcopy(status.get('vpn_evidence', {}).get(key)) for key in
                       ('owner_confirmed', 'interface', 'routes')}
    if vpn['vpn'] not in ('ok', 'off') or vpn['vpn_path'] != vpn['vpn']:
        raise ValueError('scope_changed')
    actions = []
    for proposal in run.proposals:
        for action in proposal.actions:
            field = action.get('field')
            keys = {'issue_type', 'field', 'desired', 'description', 'service', 'environment', 'health_profile', 'vpn_context'}
            keys |= {'before'} if field in ('dns', 'ipv6') else {'proxy_endpoint'}
            if 'platform_target' in action:
                keys.add('platform_target')
                target = action['platform_target']
                if (not isinstance(target, dict) or set(target) != {'service_id', 'name', 'interface'}
                        or target['name'] != action['service'] or target['interface'] != action['environment']['wifi_interface']
                        or not isinstance(target['service_id'], str) or not target['service_id']):
                    raise ValueError('scope_changed')
            if (set(action) != keys or field not in ('dns', 'ipv6', 'proxy:http', 'proxy:https', 'proxy:socks')
                    or not isinstance(action['service'], str) or not action['service']):
                raise ValueError('unsupported_action')
            environment = action['environment']
            if (set(environment) != {'wifi_interface', 'wifi_ip', 'wifi_network', 'default_interface'}
                    or not environment['wifi_interface'] or action['health_profile'] != run.snapshot['health_profile']):
                raise ValueError('scope_changed')
            try:
                ipaddress.ip_address(environment['wifi_ip'])
            except (ValueError, TypeError):
                raise ValueError('scope_changed') from None
            if field == 'dns':
                if action['issue_type'] not in ('dns_mixed_on_vpn', 'dns_no_company_on_vpn', 'dns_company_leftover'):
                    raise ValueError('unsupported_action')
                for address in action['desired']:
                    ipaddress.ip_address(address)
            elif field == 'ipv6':
                if action['issue_type'] != 'ipv6_enabled' or action['desired'] != 'Off':
                    raise ValueError('unsupported_action')
            elif action['issue_type'] != 'proxy_leftover' or action['desired'] is not False:
                raise ValueError('unsupported_action')
            value = {key: copy.deepcopy(value) for key, value in action.items() if key not in ('before', 'description')}
            if value['issue_type'] in ('dns_mixed_on_vpn', 'dns_no_company_on_vpn'):
                value['issue_type'] = 'company_dns_policy'
            actions.append(value)
    if not actions or len(actions) > 5:
        raise ValueError('unsupported_action')
    return {'contract': CONTRACT, 'profile': copy.deepcopy(run.snapshot['health_profile']),
            'vpn': vpn, 'actions': actions}


class RepairTrust:
    def __init__(self, store, clock=time.time, monotonic=time.monotonic):
        self.store, self.clock, self.monotonic = store, clock, monotonic
        self.lock = threading.RLock()
        self.revision = uuid.uuid4().hex
        self.current, self.reservations = None, {}
        self.deadline, self.failed = 0, False
        previous = store.trust_record()
        if previous:
            self.current = previous
            # Audit is durable; executable authority is deliberately core-instance local.
            if previous.get('state') == 'active':
                self.revoke('core_restarted')

    @staticmethod
    def offer(run):
        try:
            scope = scope_for(run)
        except (ValueError, TypeError, KeyError):
            return {'available': False, 'reason': 'unsupported_action'}
        return {'available': True, 'scope': scope,
                'scope_hash': hashlib.sha256(encode(scope).encode()).hexdigest(), 'modes': copy.deepcopy(MODES)}

    def create(self, run, mode, scope_hash, actor, revision):
        with self.lock:
            offer = self.offer(run)
            if (self.failed or revision != self.revision or mode not in MODES
                    or not offer['available'] or offer['scope_hash'] != scope_hash):
                raise PermissionError('The exact supported scope must be reviewed')
            now = self.clock()
            grant = {'id': uuid.uuid4().hex, 'mode': mode, 'run_id': run.id if mode == 'task' else None,
                     'scope': offer['scope'], 'scope_hash': scope_hash, 'created_at': now,
                     'expires_at': now + MODES[mode]['seconds'], 'limit': MODES[mode]['limit'], 'used': 0,
                     'state': 'active', 'reason': '', 'actor': actor,
                     'replaces': self.current['id'] if self.current else None}
            self._save(grant, 'granted', run.id)
            self.current, self.reservations = grant, {}
            self.revision = uuid.uuid4().hex
            self.deadline = self.monotonic() + MODES[mode]['seconds']
            return copy.deepcopy(grant)

    def _save(self, value, event, run_id=None, proposal_hash=None):
        try:
            self.store.save_trust(value, event, run_id, proposal_hash)
        except Exception:
            self.failed = True
            raise

    def status(self):
        with self.lock:
            value = copy.deepcopy(self.current)
            if value and value['state'] == 'active':
                reason = self._time_reason()
                if reason:
                    value.update(state='suspended', reason=reason)
                elif value['used'] >= value['limit']:
                    value.update(state='exhausted', reason='exhausted')
            return {'grant': value, 'available': not self.failed, 'restart_requires_review': True, 'revision': self.revision}

    def _time_reason(self):
        if self.failed:
            return 'journal_unavailable'
        if not self.current['created_at'] <= self.clock() < self.current['expires_at'] or self.monotonic() >= self.deadline:
            return 'expired'
        return ''

    def reason(self, run, *, reserved=False):
        with self.lock:
            grant = self.current
            if self.failed:
                return 'journal_unavailable'
            if not grant:
                return 'not_granted'
            if grant['state'] != 'active':
                return grant.get('reason') or 'not_granted'
            reason = self._time_reason()
            if reason:
                return reason
            if not reserved and grant['used'] >= grant['limit']:
                return 'exhausted'
            if grant['mode'] == 'task' and grant['run_id'] != run.id:
                return 'different_task'
            try:
                candidate = scope_for(run)
            except (ValueError, TypeError, KeyError):
                return 'unsupported_action'
            expected = grant['scope']
            if (any(candidate[key] != expected[key] for key in ('contract', 'profile', 'vpn'))
                    or any(action not in expected['actions'] for action in candidate['actions'])):
                return 'scope_changed'
            return ''

    def reserve(self, run, policy_id, grant_id, proposal_hash, allowed):
        with self.lock:
            if not allowed() or self.reason(run) or not self.current or self.current['id'] != policy_id:
                raise PermissionError('No matching active trust')
            value = copy.deepcopy(self.current)
            value['used'] += 1
            # Reservation is committed before a one-use execution grant can exist; failures do not refund it.
            self._save(value, 'reserved', run.id, proposal_hash)
            self.current = value
            self.reservations[grant_id] = (run.id, proposal_hash, allowed)
            return value['mode'], value['expires_at']

    def validate(self, run, grant):
        with self.lock:
            reservation = self.reservations.get(grant.id)
            if (not self.current or self.current['id'] != grant.trust_id or not reservation
                    or reservation[:2] != (run.id, grant.proposal_hash) or not reservation[2]()
                    or self.reason(run, reserved=True)):
                raise PermissionError('Trust was revoked, expired, stopped or changed')

    def revoke(self, reason='revoked'):
        with self.lock:
            self.revision = uuid.uuid4().hex
            self.reservations.clear()
            if not self.current or self.current['state'] != 'active':
                return
            self.current.update(state='revoked' if reason in ('revoked', 'user_cancelled') else 'suspended', reason=reason)
            self._save(self.current, reason)

    def finish(self, run):
        with self.lock:
            if self.current and self.current['mode'] == 'task' and self.current['run_id'] == run.id:
                self.revoke('task_finished')

    def release(self, grant_id):
        with self.lock:
            self.reservations.pop(grant_id, None)
