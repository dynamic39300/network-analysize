"""Responses protocol and explicit, revocable consent for minimized evidence only."""
import hashlib
import ipaddress
import json
import re
import threading
from urllib.parse import urlsplit

from .model_transport import ModelError, post_json

EVIDENCE_SCHEMA = 'relay-minimal-evidence-v2'


def strict_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError('Duplicate JSON field')
            result[key] = item
        return result
    def invalid(_value):
        raise ValueError('Non-finite JSON number')
    return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)


def endpoint_details(endpoint):
    if not isinstance(endpoint, str) or len(endpoint) > 2048 or any(c.isspace() or ord(c) < 32 for c in endpoint):
        raise ValueError('Invalid model endpoint')
    parsed = urlsplit(endpoint)
    try:
        local = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        local = False
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query
            or parsed.fragment or parsed.scheme not in ('http', 'https')
            or parsed.scheme == 'http' and not local or parsed.port == 0
            or not parsed.path.endswith('/responses')):
        raise ValueError('Model endpoint requires HTTPS or a literal loopback address')
    return local


class ModelConsent:
    """Created by a user action, never by a model tool or imported health profile."""
    def __init__(self, model):
        self.binding = model.binding
        self._model = model
        self.revoked = threading.Event()

    def allows(self, model):
        return self._model is model and not self.revoked.is_set() and self.binding == model.binding

    def revoke(self):
        self.revoked.set()


class ResponsesModel:
    def __init__(self, model, endpoint='https://api.openai.com/v1/responses', api_key='', transport=post_json):
        if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}', model):
            raise ValueError('Explicit model name required')
        self.local = endpoint_details(endpoint)
        if not isinstance(api_key, str) or len(api_key) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in api_key):
            raise ValueError('Invalid model credential')
        self._name, self._endpoint, self._api_key, self.transport = model, endpoint, api_key, transport

    @property
    def name(self):
        return self._name

    @property
    def endpoint(self):
        return self._endpoint

    @property
    def binding(self):
        return hashlib.sha256(json.dumps([self.endpoint, self.name, EVIDENCE_SCHEMA]).encode()).hexdigest()

    @property
    def credential_present(self):
        return bool(self._api_key)

    def forget_credential(self):
        self._api_key = ''

    def respond(self, instructions, history, tools, *, consent, timeout, allowed, maximum_output_tokens):
        if not isinstance(consent, ModelConsent) or not consent.allows(self):
            raise ModelError('consent_required')
        if not self.local and not self._api_key:
            raise ModelError('credential_missing')
        key = self._api_key
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        if key:
            headers['Authorization'] = 'Bearer ' + key
        payload = {'model': self.name, 'instructions': instructions, 'input': history, 'tools': tools,
                   'tool_choice': 'required', 'parallel_tool_calls': False, 'store': False,
                   'include': ['reasoning.encrypted_content'], 'max_output_tokens': maximum_output_tokens}
        try:
            raw = self.transport(self.endpoint, headers, payload, timeout=timeout,
                                 allowed=lambda: allowed() and consent.allows(self))
            document = strict_json(raw)
            if key and key in json.dumps(document, ensure_ascii=False):
                raise ValueError('Model echoed a credential')
            if not isinstance(document, dict) or document.get('status') != 'completed':
                raise ValueError('Incomplete model response')
            output = document.get('output')
            if not isinstance(output, list) or len(output) > 16 or any(not isinstance(row, dict) for row in output):
                raise ValueError('Invalid model output')
            calls = [row for row in output if row.get('type') == 'function_call']
            if len(calls) != 1 or any(row.get('type') not in ('function_call', 'reasoning', 'message') for row in output):
                raise ValueError('One local tool call required')
            if any(row.get('type') == 'message' and row.get('role') != 'assistant' for row in output):
                raise ValueError('Unexpected message authority')
            call = calls[0]
            for field in ('call_id', 'name'):
                if not isinstance(call.get(field), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', call[field]):
                    raise ValueError('Invalid tool identity')
            if not isinstance(call.get('arguments'), str) or len(call['arguments']) > 8192:
                raise ValueError('Invalid tool arguments')
            arguments = strict_json(call['arguments'])
            if not isinstance(arguments, dict):
                raise ValueError('Arguments must be an object')
            usage = document.get('usage')
            if usage is not None and (not isinstance(usage, dict) or any(type(usage.get(key)) is not int
                    or not 0 <= usage[key] <= 10 ** 7 for key in ('input_tokens', 'output_tokens'))):
                raise ValueError('Invalid model usage')
            return {'output': output, 'call_id': call['call_id'], 'name': call['name'], 'arguments': arguments,
                    'usage': {key: usage[key] for key in ('input_tokens', 'output_tokens')} if usage else None}
        except ModelError:
            raise
        except Exception as exc:
            raise ModelError('invalid_response') from exc
