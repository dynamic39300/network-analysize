"""Typed mature mutations; authentication and elevation belong to the transport."""
import copy
import re


class MutationUnconfirmed(RuntimeError):
    def __init__(self, receipt):
        self.receipt = copy.deepcopy(receipt)
        super().__init__('系统写入未确认；保留操作编号，需回读或恢复，不能重发修改')


class MacMutationWriter:
    def __init__(self, transport, target_resolver, coordinator=None):
        self.transport, self.target_resolver = transport, target_resolver
        self.coordinator = coordinator

    def target(self, service, interface):
        target = self.target_resolver(service, interface)
        if (not isinstance(target, dict) or set(target) != {'service_id', 'name', 'interface'}
                or target['name'] != service or target['interface'] != interface
                or not all(isinstance(value, str) and 0 < len(value) <= 256 for value in target.values())):
            raise ValueError('无法唯一确认当前系统网络服务标识')
        return copy.deepcopy(target)

    @staticmethod
    def request(action, expected, desired, *, operation_id, proposal_hash, restore_of=''):
        for value, length in ((operation_id, 32), (proposal_hash, 64)):
            if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{' + str(length) + '}', value):
                raise ValueError('Invalid mutation identity')
        if restore_of != '' and (not isinstance(restore_of, str) or not re.fullmatch('[0-9a-f]{32}', restore_of)):
            raise ValueError('Invalid restore identity')
        return {'version': 1, 'id': operation_id, 'proposal': proposal_hash,
                   'target': copy.deepcopy(action['platform_target']), 'field': action['field'],
                   'expected': copy.deepcopy(expected), 'desired': copy.deepcopy(desired),
                   'endpoint': copy.deepcopy(action.get('proxy_endpoint', {})), 'restore_of': restore_of}

    def requests(self, actions, saved, record):
        requests = []
        for action in actions:
            ids = record['operations'][action['field']]
            apply = self.request(action, saved[action['field']], action['desired'],
                                 operation_id=ids['apply_id'], proposal_hash=record['proposal_hash'])
            restore = self.request(action, action['desired'], saved[action['field']],
                operation_id=ids['restore_id'], proposal_hash=record['proposal_hash'], restore_of=ids['apply_id'])
            requests.append({'apply': apply, 'restore': restore})
        return requests

    def begin(self, actions, saved, record):
        if not self.coordinator:
            return
        requests = self.requests(actions, saved, record)
        self.coordinator.begin(record['batch_id'], requests)

    def inspect_recovery(self, actions, saved, record):
        if not self.coordinator:
            raise PermissionError('Authenticated helper recovery is unavailable')
        requests = self.requests(actions, saved, record)
        result = self.coordinator.inspect_recovery(record['batch_id'], requests=requests)
        if result['requests'] != requests:
            raise PermissionError('Helper recovery does not match the saved proposal')
        return result

    def recover(self, review, choice):
        if not self.coordinator:
            raise PermissionError('Authenticated helper recovery is unavailable')
        return self.coordinator.recover(review, choice)

    def end_recovery(self):
        if not self.coordinator:
            raise PermissionError('Authenticated helper recovery is unavailable')
        return self.coordinator.end_recovery()

    def end(self, outcome):
        if self.coordinator:
            self.coordinator.end(outcome)

    def write(self, action, expected, desired, *, operation_id, proposal_hash, restore_of=''):
        request = self.request(action, expected, desired, operation_id=operation_id,
                               proposal_hash=proposal_hash, restore_of=restore_of)
        try:
            response = self.transport(request)
        except Exception:
            raise MutationUnconfirmed({'id': operation_id, 'state': 'unconfirmed'}) from None
        if not isinstance(response, dict):
            raise MutationUnconfirmed({'id': operation_id, 'state': 'invalid_response'})
        if (set(response) != {'id', 'state', 'actual', 'applied', 'network_verified'}
                or response['id'] != operation_id or response['state'] != 'configured'
                or response['actual'] != desired or response['applied'] is not True
                or response['network_verified'] is not False):
            # Fixed projection: untrusted transport errors cannot enter reports.
            reason = response.get('error')
            allowed = {'configuration_changed', 'endpoint_changed', 'target_changed', 'executor_busy',
                       'configuration_busy', 'invalid_restore', 'interrupted_operation', 'journal_capacity'}
            raise MutationUnconfirmed({'id': operation_id, 'state': 'unconfirmed',
                'reason': reason if isinstance(reason, str) and reason in allowed else 'operation_unconfirmed'})
        return copy.deepcopy(response)
