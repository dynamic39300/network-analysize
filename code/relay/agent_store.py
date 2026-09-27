"""Private, durable task journal. Recovery never replays a network write."""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .private_files import check_private, create_private_directory, lock_runtime, open_private_file
from .preferences import DEFAULT_PREFERENCES, validate_preferences


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default)


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Unsupported journal value: {type(value).__name__}")


class AgentStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        create_private_directory(self.directory)
        self._private(self.directory, directory=True)
        self.path = self.directory / 'agent.sqlite3'
        fd = open_private_file(self.path)
        os.close(fd)
        self._private(self.path)
        lock_path = self.directory / 'owner.lock'
        self._lock_fd = open_private_file(lock_path)
        try:
            self._private(lock_path)
            lock_runtime(self._lock_fd)
        except BaseException:
            os.close(self._lock_fd)
            self._lock_fd = None
            raise
        try:
            self._initialize()
        except BaseException:
            self.close()
            raise

    def _initialize(self):
        with self._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS runs '
                       '(id TEXT PRIMARY KEY, updated REAL NOT NULL, stage TEXT NOT NULL, '
                       'fingerprint TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS guard_checks (started REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS model_calls '
                       '(id TEXT PRIMARY KEY, started REAL NOT NULL, run_id TEXT NOT NULL, '
                       'binding TEXT NOT NULL, reserved_output INTEGER NOT NULL, usage TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS ipc_requests '
                       '(id TEXT PRIMARY KEY, created REAL NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS privacy_events '
                       '(created REAL NOT NULL, action TEXT NOT NULL, binding TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS authorization_events '
                       '(created REAL NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS profiles '
                       '(id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS profile_versions '
                       '(id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (id, revision))')
            db.execute('CREATE TABLE IF NOT EXISTS agent_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS profile_baselines '
                       '(id TEXT NOT NULL, revision INTEGER NOT NULL, verified REAL NOT NULL, '
                       'payload TEXT NOT NULL, PRIMARY KEY (id, revision))')

    def load_profiles(self):
        with self._connect() as db:
            rows = db.execute('SELECT payload FROM profiles ORDER BY rowid').fetchall()
            active = db.execute("SELECT value FROM agent_settings WHERE key='active_profile'").fetchone()
        return [json.loads(row[0]) for row in rows], active[0] if active else None

    def core_guard_enabled(self):
        with self._connect() as db:
            row = db.execute("SELECT value FROM agent_settings WHERE key='core_guard_enabled'").fetchone()
        value = json.loads(row[0]) if row else False
        if type(value) is not bool:
            raise ValueError('Invalid saved guard choice')
        return value

    def save_core_guard_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('Guard choice must be boolean')
        with self._connect() as db:
            db.execute("INSERT INTO agent_settings VALUES ('core_guard_enabled', ?) ON CONFLICT(key) "
                       'DO UPDATE SET value=excluded.value', (encode(enabled),))

    @staticmethod
    def _preferences(db):
        row = db.execute("SELECT value FROM agent_settings WHERE key='desktop_preferences'").fetchone()
        value = json.loads(row[0]) if row else {'revision': 0, 'values': DEFAULT_PREFERENCES}
        if (set(value) != {'revision', 'values'} or type(value['revision']) is not int or value['revision'] < 0):
            raise ValueError('Invalid preference record')
        return {'revision': value['revision'], 'values': validate_preferences(value['values'])}

    def preferences(self):
        with self._connect() as db:
            return self._preferences(db)

    def save_preferences(self, values, revision):
        values = validate_preferences(values)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self._preferences(db)
            if type(revision) is not int or current['revision'] != revision:
                raise ValueError('Preferences changed; reload before saving')
            saved = {'revision': revision + 1, 'values': values}
            db.execute("INSERT INTO agent_settings VALUES ('desktop_preferences', ?) ON CONFLICT(key) "
                       'DO UPDATE SET value=excluded.value', (encode(saved),))
        return saved

    def model_configuration(self):
        with self._connect() as db:
            row = db.execute("SELECT value FROM agent_settings WHERE key='model_configuration'").fetchone()
        if not row:
            return None
        value = json.loads(row[0])
        if not isinstance(value, dict) or set(value) != {'name', 'endpoint'}:
            raise ValueError('Invalid saved model configuration')
        return value

    def save_model_configuration(self, name, endpoint):
        with self._connect() as db:
            db.execute("INSERT INTO agent_settings VALUES ('model_configuration', ?) ON CONFLICT(key) "
                       'DO UPDATE SET value=excluded.value', (encode({'name': name, 'endpoint': endpoint}),))

    def privacy_event(self, action, binding):
        if action not in ('configured', 'consent_granted', 'consent_revoked', 'credential_forgotten'):
            raise ValueError('Unsupported privacy event')
        with self._connect() as db:
            db.execute('INSERT INTO privacy_events VALUES (?, ?, ?)', (time.time(), action, binding))
            db.execute('DELETE FROM privacy_events WHERE rowid NOT IN '
                       '(SELECT rowid FROM privacy_events ORDER BY rowid DESC LIMIT 1000)')

    def privacy_history(self):
        with self._connect() as db:
            rows = db.execute('SELECT created, action, binding FROM privacy_events ORDER BY rowid DESC LIMIT 10').fetchall()
        return [{'time': row[0], 'action': row[1], 'binding': row[2]} for row in rows]

    def ipc_request(self, request_id):
        with self._connect() as db:
            row = db.execute('SELECT fingerprint, payload FROM ipc_requests WHERE id=?', (request_id,)).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def trust_record(self):
        with self._connect() as db:
            row = db.execute("SELECT value FROM agent_settings WHERE key='repair_trust'").fetchone()
        return json.loads(row[0]) if row else None

    def save_trust(self, grant, event, run_id=None, proposal_hash=None):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("INSERT INTO agent_settings VALUES ('repair_trust', ?) ON CONFLICT(key) "
                       'DO UPDATE SET value=excluded.value', (encode(grant),))
            db.execute('INSERT INTO authorization_events VALUES (?, ?)', (time.time(), encode({
                'grant_id': grant['id'], 'event': event, 'mode': grant['mode'], 'scope_hash': grant['scope_hash'],
                'used': grant['used'], 'run_id': run_id, 'proposal_hash': proposal_hash,
                'replaces': grant.get('replaces') if event == 'granted' else None})))
            db.execute('DELETE FROM authorization_events WHERE rowid NOT IN '
                       '(SELECT rowid FROM authorization_events ORDER BY rowid DESC LIMIT 1000)')

    def trust_history(self):
        with self._connect() as db:
            rows = db.execute('SELECT created, payload FROM authorization_events ORDER BY rowid DESC LIMIT 10').fetchall()
        return [{'time': row[0], **json.loads(row[1])} for row in rows]

    def save_ipc_request(self, request_id, fingerprint, payload, *, create=False):
        with self._connect() as db:
            if create:
                db.execute('DELETE FROM ipc_requests WHERE created < ?', (time.time() - 30 * 86400,))
                if db.execute('SELECT COUNT(*) FROM ipc_requests').fetchone()[0] >= 10000:
                    raise ValueError('Local request retention capacity reached')
                db.execute('INSERT INTO ipc_requests VALUES (?, ?, ?, ?)',
                           (request_id, time.time(), fingerprint, encode(payload)))
            else:
                db.execute('UPDATE ipc_requests SET payload=? WHERE id=? AND fingerprint=?',
                           (encode(payload), request_id, fingerprint))

    def interrupt_ipc_requests(self):
        with self._connect() as db:
            rows = db.execute('SELECT id, payload FROM ipc_requests').fetchall()
            for request_id, payload in rows:
                state = json.loads(payload)
                if state.get('state') in ('running', 'accepted'):
                    state.update(state='interrupted', error='core_restarted')
                    db.execute('UPDATE ipc_requests SET payload=? WHERE id=?', (encode(state), request_id))

    def save_profile(self, document, profile_id=None, revision=None, source='user', activate=False):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if profile_id:
                current = db.execute('SELECT revision FROM profiles WHERE id=?', (profile_id,)).fetchone()
                if not current or current[0] != revision:
                    raise ValueError('档案已被更新或删除，请重新打开后再修改')
                next_revision = revision + 1
            else:
                profile_id, next_revision = 'profile-' + uuid.uuid4().hex[:12], 1
            profile = {**document, 'id': profile_id, 'revision': next_revision,
                       'source': source, 'confirmed_at': time.time() if source != 'configured_targets' else None}
            payload = encode(profile)
            db.execute('INSERT INTO profiles VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE '
                       'SET revision=excluded.revision, payload=excluded.payload', (profile_id, next_revision, payload))
            db.execute('INSERT INTO profile_versions VALUES (?, ?, ?)', (profile_id, next_revision, payload))
            if activate:
                db.execute("INSERT INTO agent_settings VALUES ('active_profile', ?) ON CONFLICT(key) "
                           'DO UPDATE SET value=excluded.value', (profile_id,))
        return profile

    def activate_profile(self, profile_id):
        with self._connect() as db:
            if not db.execute('SELECT 1 FROM profiles WHERE id=?', (profile_id,)).fetchone():
                raise ValueError('档案不存在')
            db.execute("INSERT INTO agent_settings VALUES ('active_profile', ?) ON CONFLICT(key) "
                       'DO UPDATE SET value=excluded.value', (profile_id,))

    def remove_profile(self, profile_id):
        with self._connect() as db:
            active = db.execute("SELECT value FROM agent_settings WHERE key='active_profile'").fetchone()
            if active and active[0] == profile_id:
                raise ValueError('请先切换到另一份档案，再移除此档案')
            db.execute('DELETE FROM profiles WHERE id=?', (profile_id,))

    def save_baseline(self, profile_id, revision, verified_at, targets):
        with self._connect() as db:
            current = db.execute('SELECT revision FROM profiles WHERE id=?', (profile_id,)).fetchone()
            if not current or current[0] != revision:
                raise ValueError('档案版本变化，未保存旧检测为正常记录')
            db.execute('INSERT INTO profile_baselines VALUES (?, ?, ?, ?) ON CONFLICT(id, revision) '
                       'DO UPDATE SET verified=excluded.verified, payload=excluded.payload',
                       (profile_id, revision, verified_at, encode(targets)))

    def load_baseline(self, profile_id, revision):
        with self._connect() as db:
            row = db.execute('SELECT verified, payload FROM profile_baselines WHERE id=? AND revision=?',
                             (profile_id, revision)).fetchone()
        return {'verified_at': row[0], 'targets': json.loads(row[1])} if row else None

    def guard_check_ages(self):
        now = time.time()
        with self._connect() as db:
            rows = db.execute('SELECT started FROM guard_checks WHERE started > ? ORDER BY started',
                              (now - 3600,)).fetchall()
        return [max(0, now - row[0]) for row in rows]

    def reserve_guard_check(self, limit):
        now = time.time()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM guard_checks WHERE started <= ?', (now - 3600,))
            count = db.execute('SELECT COUNT(*) FROM guard_checks').fetchone()[0]
            if count >= limit:
                return False
            db.execute('INSERT INTO guard_checks VALUES (?)', (now,))
        return True

    def close(self):
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def reserve_model_call(self, run_id, binding, call_limit, output_tokens, output_limit):
        now = time.time()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM model_calls WHERE started <= ?', (now - 3600,))
            count, reserved = db.execute('SELECT COUNT(*), COALESCE(SUM(reserved_output), 0) FROM model_calls').fetchone()
            if count >= call_limit or reserved + output_tokens > output_limit:
                return None
            call_id = 'model-' + uuid.uuid4().hex[:12]
            db.execute('INSERT INTO model_calls VALUES (?, ?, ?, ?, ?, NULL)',
                       (call_id, now, run_id, binding, output_tokens))
            return call_id

    def record_model_usage(self, call_id, usage):
        with self._connect() as db:
            db.execute('UPDATE model_calls SET usage=? WHERE id=?', (encode(usage), call_id))

    def __del__(self):
        if getattr(self, '_lock_fd', None) is not None:
            self.close()

    @staticmethod
    def _private(path, directory=False):
        check_private(path, directory)

    @contextmanager
    def _connect(self):
        if self._lock_fd is None:
            raise RuntimeError('Agent journal is closed')
        self._private(self.directory, directory=True)
        self._private(self.path)
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    def save(self, run, fingerprint=''):
        payload = encode(asdict(run))
        with self._connect() as db:
            db.execute('INSERT INTO runs VALUES (?, ?, ?, ?, ?) '
                       'ON CONFLICT(id) DO UPDATE SET updated=excluded.updated, '
                       'stage=excluded.stage, fingerprint=excluded.fingerprint, payload=excluded.payload',
                       (run.id, time.time(), run.stage, fingerprint, payload))
            terminal = "('observed', 'finished', 'cancelled', 'blocked', 'needs_participation', 'model_limited')"
            preferences = self._preferences(db)['values']
            db.execute(f'DELETE FROM runs WHERE stage IN {terminal} AND updated < ?',
                       (time.time() - preferences['history_days'] * 86400,))
            db.execute(f'DELETE FROM runs WHERE id IN (SELECT id FROM runs WHERE stage IN {terminal} '
                       'ORDER BY updated DESC LIMIT -1 OFFSET ?)', (preferences['history_limit'],))

    def recent(self, limit=50):
        with self._connect() as db:
            rows = db.execute('SELECT payload FROM runs ORDER BY updated DESC LIMIT ?',
                              (max(1, min(int(limit), 500)),)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def get(self, run_id):
        with self._connect() as db:
            row = db.execute('SELECT payload FROM runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise ValueError('Task record not found')
        return json.loads(row[0])

    def review_dynamic(self, run_id, receipt_hash, note, snapshot, identity):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT payload FROM runs WHERE id=? AND stage='needs_reconciliation'", (run_id,)).fetchone()
            if row is None:
                raise ValueError('Task does not require reconciliation')
            run = json.loads(row[0])
            receipt = run.get('receipt') or {}
            if receipt.get('kind') != 'dynamic_command' or hashlib.sha256(encode(receipt).encode()).hexdigest() != receipt_hash:
                raise PermissionError('Exact dynamic receipt review is required')
            run['manual_review'] = {'note': note, 'receipt_hash': receipt_hash, 'identity': identity,
                                    'reviewed_at': time.time(), 'snapshot': snapshot,
                                    'command_effects_verified': False}
            run.update(stage='finished', outcome='reviewed', stop_reason='human_review_not_automatic_verification')
            run['events'].append({'time': datetime.now().isoformat(timespec='seconds'),
                                  'message': '用户已记录人工核对并解除此任务的执行阻止；这不表示 Agent 证实全部命令副作用'})
            db.execute('UPDATE runs SET stage=?, updated=?, payload=? WHERE id=?',
                       (run['stage'], time.time(), encode(run), run_id))

    def attach_job_receipt(self, run_id, terminal):
        with self._connect() as db:
            row = db.execute("SELECT payload FROM runs WHERE id=? AND stage='needs_reconciliation'", (run_id,)).fetchone()
            if row is None:
                return
            run = json.loads(row[0])
            if run.get('receipt', {}).get('kind') != 'dynamic_command':
                raise ValueError('Not a dynamic job')
            run['receipt']['job'] = {**terminal, 'terminal_record_saved': True}
            db.execute('UPDATE runs SET payload=? WHERE id=?', (encode(run), run_id))

    def latest_incident(self):
        with self._connect() as db:
            row = db.execute('SELECT fingerprint, payload FROM runs ORDER BY updated DESC LIMIT 1').fetchone()
        if row:
            return row[0], json.loads(row[1]).get('incident_id')
        return '', None

    def needs_reconciliation(self):
        with self._connect() as db:
            return bool(db.execute("SELECT 1 FROM runs WHERE stage='needs_reconciliation' LIMIT 1").fetchone())

    def pending_reconciliation(self):
        with self._connect() as db:
            rows = db.execute("SELECT payload FROM runs WHERE stage='needs_reconciliation'").fetchall()
        return [json.loads(row[0]) for row in rows]

    def reconcile(self, run_id, result):
        with self._connect() as db:
            row = db.execute("SELECT payload FROM runs WHERE id=? AND stage='needs_reconciliation'",
                             (run_id,)).fetchone()
            if not row:
                return
            run = json.loads(row[0])
            run['reconciliation'] = result
            run['events'].append({'time': datetime.now().isoformat(timespec='seconds'),
                                  'message': result['message']})
            if result['outcome'] in ('verified', 'restored', 'not_started'):
                run.update(stage='finished', outcome=result['outcome'], stop_reason='')
            db.execute('UPDATE runs SET stage=?, payload=? WHERE id=?',
                       (run['stage'], encode(run), run_id))

    def recover_interrupted(self):
        """Invalidate pending consent, flag possibly partial writes for reconciliation."""
        recovered = []
        with self._connect() as db:
            rows = db.execute("SELECT id, stage, payload FROM runs WHERE stage IN "
                              "('awaiting_authorization', 'authorized', 'executing', 'investigating', 'finished', 'needs_reconciliation')").fetchall()
            for run_id, stage, payload in rows:
                run = json.loads(payload)
                if stage in ('finished', 'needs_reconciliation'):
                    if run.get('model_state', {}).get('state') not in ('waiting_authorization', 'awaiting_continuation', 'running'):
                        continue
                    run['model_state'].update(state='interrupted', reason='process_restarted')
                    run['events'].append({'time': datetime.now().isoformat(timespec='seconds'),
                        'message': '进程重启：保留已有执行收据和核对状态；未恢复模型上下文、重放命令或自动上传'})
                    db.execute('UPDATE runs SET payload=? WHERE id=?', (encode(run), run_id))
                    recovered.append(run)
                    continue
                interrupted_write = stage == 'executing'
                run.update(stage='needs_reconciliation' if interrupted_write else 'cancelled',
                           outcome='interrupted' if interrupted_write else 'cancelled',
                           stop_reason='process_restarted')
                if stage == 'investigating':
                    run['model_state'] = {**run.get('model_state', {}), 'state': 'interrupted',
                                          'reason': 'process_restarted'}
                run['events'].append({'time': datetime.now().isoformat(timespec='seconds'),
                                      'message': '进程重启：需核对修改和恢复记录，未重放命令' if interrupted_write
                                      else '进程重启：旧授权方案已失效，请重新检测'})
                db.execute('UPDATE runs SET stage=?, payload=? WHERE id=?',
                           (run['stage'], encode(run), run_id))
                recovered.append(run)
        return recovered
