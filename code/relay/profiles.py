"""Explicit health expectations, kept separate from observed network state."""
import copy
from datetime import datetime
import hashlib
import json
import time
from urllib.parse import urlsplit
import uuid

SCHEMA = 'relay-health-profile-v1'
PATHS = {'system': '系统路径', 'direct': '不经代理', 'system_proxy': '系统代理', 'vpn': '已确认 VPN'}
SCOPES = {'always': '所有环境', 'vpn_connected': 'VPN 已连接时', 'vpn_disconnected': 'VPN 已断开时'}
REQUIREMENTS = {'transport': '网络可达', 'service': '服务正常响应'}
STATES = {'healthy': '符合预期', 'degraded': '不符合预期', 'unknown': '尚未确认',
          'stale': '结果已过期', 'not_applicable': '当前不适用', 'auth_required': '需要身份认证'}


def new_id(prefix):
    return prefix + '-' + uuid.uuid4().hex[:12]


def target_key(target):
    return target.get('id') or str(target.get('name', 'unknown')).lower().replace(' ', '_')


def definition_hash(target):
    values = {key: target.get(key) for key in ('id', 'name', 'url', 'expected_path', 'when', 'requirement', 'timeout')}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def scope_state(target, status):
    scope = target.get('when', 'always')
    if scope == 'always':
        return True
    vpn = status.get('vpn')
    if vpn == 'ok' and status.get('vpn_evidence', {}).get('owner_confirmed'):
        return scope == 'vpn_connected'
    if vpn == 'off' and status.get('vpn_path') == 'off':
        return scope == 'vpn_disconnected'
    return None


def normalize_document(document, retain_ids=False):
    if not isinstance(document, dict) or document.get('schema') != SCHEMA:
        raise ValueError('这不是受支持的网络健康档案')
    if set(document) - {'schema', 'name', 'targets'}:
        raise ValueError('档案包含未支持的字段')
    name = document.get('name')
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise ValueError('档案名称须为 1–80 个字符')
    raw_targets = document.get('targets')
    if not isinstance(raw_targets, list) or not 1 <= len(raw_targets) <= 32:
        raise ValueError('每份档案需要 1–32 个保护目标')
    targets, ids = [], set()
    for raw in raw_targets:
        if not isinstance(raw, dict) or set(raw) - {'id', 'name', 'url', 'expected_path', 'when', 'requirement', 'timeout'}:
            raise ValueError('保护目标包含未支持的字段')
        label, url = raw.get('name'), raw.get('url')
        if not isinstance(label, str) or not label.strip() or len(label) > 60:
            raise ValueError('目标名称须为 1–60 个字符')
        if (not isinstance(url, str) or len(url) > 2048
                or any(ord(c) < 33 or ord(c) == 127 or c.isspace() for c in url)):
            raise ValueError('目标地址无效')
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or parsed.port == 0):
            raise ValueError('目标必须是无内嵌凭据、无片段的 HTTP(S) 地址')
        target = {'id': raw.get('id') if retain_ids and raw.get('id') else new_id('target'),
                  'name': label.strip(), 'url': url,
                  'expected_path': raw.get('expected_path', 'system'), 'when': raw.get('when', 'always'),
                  'requirement': raw.get('requirement', 'transport'), 'timeout': raw.get('timeout', 8)}
        if (not isinstance(target['id'], str) or not 1 <= len(target['id']) <= 80
                or not all(c.isalnum() or c in '-_' for c in target['id']) or target['id'] in ids):
            raise ValueError('目标标识重复或无效')
        ids.add(target['id'])
        if any(not isinstance(target[field], str) or target[field] not in choices
               for field, choices in (('expected_path', PATHS), ('when', SCOPES), ('requirement', REQUIREMENTS))):
            raise ValueError('目标路径、适用环境或成功条件无效')
        if type(target['timeout']) is not int or not 1 <= target['timeout'] <= 30:
            raise ValueError('访问超时须为 1–30 秒')
        targets.append(target)
    return {'schema': SCHEMA, 'name': name.strip(), 'targets': targets}


def parse_import(text):
    if len(text.encode('utf-8')) > 256 * 1024:
        raise ValueError('档案文件不能超过 256 KiB')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('档案存在重复字段')
            result[key] = value
        return result
    return normalize_document(json.loads(text, object_pairs_hook=pairs))


def evaluate_target(target, snapshot, fresh):
    observation = snapshot.get('status', {}).get('reachability_results', {}).get(target_key(target), {})
    state, detail = 'unknown', '尚无与当前目标匹配的观测'
    if not fresh:
        state, detail = 'stale', '需要按当前档案重新检测'
    elif observation.get('definition_hash') == definition_hash(target):
        scope = observation.get('applicable')
        if scope is False:
            state, detail = 'not_applicable', '当前环境不满足适用条件'
        elif scope is None:
            detail = '无法确认适用环境'
        elif observation.get('path_verified') is not True:
            state = 'degraded' if observation.get('path_verified') is False else 'unknown'
            detail = observation.get('limitation') or '预期访问路径尚未验证'
        elif observation.get('transport') != 'ok':
            state = 'unknown' if observation.get('transport') in ('unknown', 'check_failed') else 'degraded'
            detail = '访问结果：' + str(observation.get('transport', 'unknown'))
        elif target['requirement'] == 'service' and observation.get('service') != 'responding':
            state = 'auth_required' if observation.get('service') == 'auth_required' else 'degraded'
            detail = '服务返回 HTTP ' + str(observation.get('http_status', '未知'))
        else:
            state, detail = 'healthy', '访问路径与成功条件均符合预期'
    return {'id': target['id'], 'name': target['name'], 'state': state, 'label': STATES[state],
            'summary': detail, 'expected_path': target['expected_path'], 'when': target['when'],
            'requirement': target['requirement'], 'observation': copy.deepcopy(observation)}


def expire_assessment(assessment, now=None):
    """Update a UI copy without touching persistence or issuing new probes."""
    stamp = assessment.get('observed_at')
    if stamp and not 0 <= (time.time() if now is None else now) - stamp <= 600:
        assessment['health'] = 'unknown'
        assessment['covered'] = 0
        for row in assessment.get('targets', []):
            row.update(state='stale', label=STATES['stale'], summary='观测已过期，需要重新检测')
    return assessment


class HealthProfiles:
    def __init__(self, store, config, clock=time.time):
        self.store, self.clock = store, clock
        self.active = None
        self.profiles = []
        self.baseline = None
        self.reload()
        if not self.profiles:
            legacy = config.get('reachability.targets', [])
            if legacy:
                document = normalize_document({'schema': SCHEMA, 'name': '现有网络目标', 'targets': legacy})
                self.store.save_profile(document, source='configured_targets', activate=True)
                self.reload()

    def reload(self):
        profiles, active_id = self.store.load_profiles()
        for profile in profiles:
            document = {key: profile.get(key) for key in ('schema', 'name', 'targets')}
            if normalize_document(document, retain_ids=True) != document:
                raise ValueError('网络档案定义不完整')
            if not profile.get('id') or type(profile.get('revision')) is not int or profile['revision'] < 1:
                raise ValueError('网络档案的版本记录无效')
        active = next((p for p in profiles if p['id'] == active_id), None)
        if profiles and active is None:
            raise ValueError('当前网络档案记录不完整')
        self.profiles, self.active = profiles, active
        self.baseline = self.store.load_baseline(self.active['id'], self.active['revision']) if self.active else None

    def binding(self):
        return {'id': self.active['id'], 'revision': self.active['revision']} if self.active else None

    def save(self, document, profile_id=None, revision=None, source='user', activate=True):
        normalized = normalize_document(document, retain_ids=profile_id is not None)
        profile = self.store.save_profile(normalized, profile_id, revision, source, activate=activate)
        self.reload()
        return profile

    def activate(self, profile_id):
        self.store.activate_profile(profile_id)
        self.reload()

    def remove(self, profile_id):
        self.store.remove_profile(profile_id)
        self.reload()

    def document(self, profile_id):
        profile = next((p for p in self.profiles if p['id'] == profile_id), None)
        if profile is None:
            raise ValueError('档案不存在')
        return copy.deepcopy({key: profile[key] for key in ('schema', 'name', 'targets')})

    def evaluate(self, snapshot):
        if not self.active:
            return {'health': 'unknown', 'targets': [], 'covered': 0, 'total': 0}
        stamp = snapshot.get('last_check')
        observed_at = stamp.timestamp() if isinstance(stamp, datetime) else 0
        fresh = (snapshot.get('health_profile') == self.binding() and observed_at
                 and 0 <= self.clock() - observed_at <= 600)
        rows = [evaluate_target(target, snapshot, fresh) for target in self.active['targets']]
        states = {row['state'] for row in rows}
        health = ('degraded' if 'degraded' in states else 'unknown' if states & {'unknown', 'stale'}
                  or states == {'not_applicable'} else 'attention' if 'auth_required' in states else 'healthy')
        return {'id': self.active['id'], 'name': self.active['name'], 'revision': self.active['revision'],
                'health': health, 'targets': rows, 'total': len(rows),
                'observed_at': observed_at if snapshot.get('health_profile') == self.binding() else None,
                'covered': sum(r['state'] == 'healthy' for r in rows),
                'last_verified': self.baseline.get('verified_at') if self.baseline else None}

    def record_verification(self, snapshot):
        result = self.evaluate(snapshot)
        if (result['health'] == 'healthy' and not snapshot.get('check_errors')
                and not any(i[0] in ('high', 'medium') for i in snapshot.get('issues', []))):
            self.store.save_baseline(self.active['id'], self.active['revision'], self.clock(), result['targets'])
            self.baseline = self.store.load_baseline(self.active['id'], self.active['revision'])
        return result


class ProfileConfig:
    """Runtime target overlay; never rewrites the user's legacy config file."""
    def __init__(self, base, profiles):
        self.base, self.profiles = base, profiles

    def get(self, key, default=None):
        if self.profiles and self.profiles.active:
            if key == 'reachability.targets':
                return copy.deepcopy(self.profiles.active['targets'])
            if key == 'health_profile.binding':
                return self.profiles.binding()
        return self.base.get(key, default)

    def __getattr__(self, key):
        return getattr(self.base, key)


class SelectedTargets:
    """Narrow one probe to existing profile IDs; never let a model introduce an address."""
    def __init__(self, base, target_ids):
        self.base = base
        targets = base.get('reachability.targets', [])
        known = {target.get('id') for target in targets}
        if not target_ids or len(set(target_ids)) != len(target_ids) or any(key not in known for key in target_ids):
            raise ValueError('Target selection is outside the active profile')
        self.targets = [copy.deepcopy(target) for target in targets if target.get('id') in target_ids]

    def get(self, key, default=None):
        return copy.deepcopy(self.targets) if key == 'reachability.targets' else self.base.get(key, default)

    def __getattr__(self, key):
        return getattr(self.base, key)
