"""Authenticated native helper calls; never registers or elevates implicitly."""
import copy
import threading
import time
import uuid

from .mac_identity import native_bridge, production_requirement

HELPER_LABEL = 'com.wangxinlei.relay.helper'


def trusted_pair():
    if not production_requirement():
        raise PermissionError('The privileged helper requires a Developer ID build')
    pair = dict(native_bridge().trustedPair())
    if set(pair) != {'app_requirement', 'helper_requirement', 'app_hash', 'helper_hash', 'team'}:
        raise PermissionError('The signed helper pair is unavailable')
    return pair


class MacHelperTransport:
    def __init__(self, *, requirement=None, endpoint=None, peer_uid=0):
        self.requirement, self.endpoint, self.peer_uid = requirement, endpoint, peer_uid
        self.client_id = uuid.uuid4().hex
        self.context = None
        self.recovery_context = None
        self.last_instance = None

    def call(self, method, params=None, *, expected_instance=None):
        from Foundation import NSXPCConnection, NSXPCConnectionPrivileged
        from .mac_xpc import BRIDGE, encoded, decoded
        from .ipc import HEX
        requirement = self.requirement or trusted_pair()['helper_requirement']
        connection = (NSXPCConnection.alloc().initWithListenerEndpoint_(self.endpoint) if self.endpoint is not None
                      else NSXPCConnection.alloc().initWithMachServiceName_options_(HELPER_LABEL, NSXPCConnectionPrivileged))
        connection.setCodeSigningRequirement_(requirement)
        connection.setRemoteObjectInterface_(BRIDGE.interface())
        connection.resume()
        deadline = time.monotonic() + 5
        try:
            def exchange(value):
                done, answers, errors = threading.Event(), [], []
                def received(data):
                    answers.append(data)
                    done.set()
                def failed(error):
                    errors.append(int(error.code()))
                    done.set()
                connection.remoteObjectProxyWithErrorHandler_(failed).exchange_reply_(encoded(value), received)
                if not done.wait(max(0, deadline - time.monotonic())):
                    raise TimeoutError('Helper response unconfirmed; no request replayed')
                if errors or not answers:
                    raise ConnectionError('Helper identity or connection unavailable')
                return decoded(answers[0])
            nonce, request_id = uuid.uuid4().hex, uuid.uuid4().hex
            hello = exchange({'hello': nonce})
            if (set(hello) != {'hello', 'nonce', 'version', 'instance'} or hello['hello'] != nonce
                    or hello['version'] != 1 or int(connection.effectiveUserIdentifier()) != self.peer_uid
                    or not isinstance(hello['instance'], str) or not HEX.fullmatch(hello['instance'])
                    or not isinstance(hello['nonce'], str) or not HEX.fullmatch(hello['nonce'])
                    or expected_instance is not None and hello['instance'] != expected_instance):
                raise PermissionError('Helper identity or instance changed')
            self.last_instance = hello['instance']
            response = exchange({'version': 1, 'instance': hello['instance'], 'nonce': hello['nonce'],
                'id': request_id, 'client': self.client_id, 'method': method, 'params': params or {}})
            if (set(response) != {'instance', 'nonce', 'id', 'result'} or response['id'] != request_id
                    or response['instance'] != hello['instance'] or response['nonce'] != hello['nonce']
                    or not isinstance(response['result'], dict)):
                raise ValueError('Invalid helper response')
            return response['result']
        finally:
            connection.invalidate()

    def begin(self, batch_id, requests):
        if self.context or self.recovery_context:
            raise PermissionError('Previous helper batch is unresolved')
        self.context = {'batch_id': batch_id, 'instance': None, 'requests': copy.deepcopy(requests)}
        try:
            result = self.call('begin', {'batch_id': batch_id, 'requests': requests})
        finally:
            self.context['instance'] = self.last_instance
        if result != {'batch_id': batch_id}:
            self.context = None
            raise PermissionError('系统执行服务未接受任务；请核对系统批准、其他任务或恢复记录')

    def perform(self, request):
        if not self.context:
            raise PermissionError('A helper batch is required')
        return self.call('perform', {'request': request}, expected_instance=self.context['instance'])

    def end(self, outcome):
        if not self.context:
            return
        if outcome not in ('verified', 'rolled_back', 'blocked'):
            raise PermissionError('Helper recovery remains unresolved')
        result = self.call('end', {'batch_id': self.context['batch_id'], 'outcome': outcome},
                           expected_instance=self.context['instance'])
        if result != {'settled': True}:
            raise PermissionError('Helper batch closure is unconfirmed')
        self.context = None

    def inspect_recovery(self, batch_id, *, requests=None):
        from .ipc import HEX
        if not isinstance(batch_id, str) or not HEX.fullmatch(batch_id):
            raise ValueError('Invalid helper batch identity')
        params = {'batch_id': batch_id}
        if requests is not None:
            params['requests'] = copy.deepcopy(requests)
        result = self.call('recovery_inspect', params)
        if (result.get('batch_id') != batch_id or result.get('state') not in ('pending', 'finished')
                or not isinstance(result.get('requests'), list)
                or requests is not None and result['requests'] != requests):
            raise PermissionError('Helper recovery record is unavailable')
        if result['state'] == 'pending':
            if (not isinstance(result.get('review_id'), str) or not HEX.fullmatch(result['review_id'])
                    or result.get('helper_instance') != self.last_instance
                    or any(type(result.get(key)) is not bool for key in ('eligible', 'can_restore', 'can_retain'))):
                raise ValueError('Invalid helper recovery review')
        elif result.get('disposition') not in ('restored', 'retained', 'not_started') or result.get('network_verified') is not False:
            raise ValueError('Invalid helper recovery closure')
        if result['state'] == 'finished':
            for context in (self.context, self.recovery_context):
                if context and context['batch_id'] == batch_id and context['requests'] != result['requests']:
                    raise ValueError('Helper closure does not match the pending manifest')
            self._forget_batch(batch_id)
        return {**result, 'helper_instance': self.last_instance}

    def _forget_batch(self, batch_id):
        for name in ('context', 'recovery_context'):
            context = getattr(self, name)
            if context and context['batch_id'] == batch_id:
                setattr(self, name, None)

    def recover(self, review, choice):
        if (choice not in ('restore', 'retain') or review.get('state') != 'pending'
                or review.get('can_' + choice) is not True):
            raise PermissionError('Recovery requires a fresh eligible review')
        self.recovery_context = {'batch_id': review['batch_id'], 'recovery_id': review['review_id'],
                                 'instance': review['helper_instance'], 'requests': copy.deepcopy(review['requests'])}
        result = self.call('recovery_apply', {'batch_id': review['batch_id'], 'review_id': review['review_id'],
                                            'choice': choice}, expected_instance=review['helper_instance'])
        if (result.get('recovery_id') != review['review_id']
                or result.get('state') not in ('ready_to_settle', 'needs_verification')):
            self.recovery_context = None
            raise PermissionError('Recovery was not accepted; inspect again without replaying changes')
        return result

    def end_recovery(self):
        if not self.recovery_context:
            raise PermissionError('No recovery is owned by this client')
        context = self.recovery_context
        result = self.call('recovery_end', {key: context[key] for key in ('batch_id', 'recovery_id')},
                           expected_instance=context['instance'])
        if (result.get('state') != 'finished' or result.get('batch_id') != context['batch_id']
                or result.get('recovery_id') != context['recovery_id']
                or result.get('requests') != context['requests']
                or result.get('disposition') not in ('restored', 'retained', 'not_started')
                or result.get('network_verified') is not False):
            raise PermissionError('Recovery closure is unconfirmed; the batch remains blocked')
        self._forget_batch(context['batch_id'])
        return result


def mutation_writer():
    if not production_requirement():
        return None
    from .mutations import MacMutationWriter
    transport = MacHelperTransport()
    def resolve(service, interface):
        value = native_bridge().targetNamed_interface_(service, interface)
        return dict(value) if value else None
    return MacMutationWriter(transport.perform, resolve, coordinator=transport)
