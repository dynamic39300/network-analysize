"""Local network assurance Agent built around evidence, authorization and verification.

Manual actions and the guard scheduler share this local assurance loop.
Diagnostics and mature verified repairs remain available without a cloud model.
"""
import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import threading
import time
import uuid

from .agent_store import encode
from .private_files import execution_slot


@dataclass(frozen=True)
class HealthTarget:
    id: str
    name: str
    kind: str
    expected: str
    source: str
    trusted: bool = True


@dataclass(frozen=True)
class Observation:
    target_id: str
    state: str
    summary: str
    evidence: object = None
    limitation: str = ""


@dataclass(frozen=True)
class ActionProposal:
    id: str
    issue_types: tuple
    plans: tuple
    actions: tuple = ()
    expires_at: float = 0
    authorization: str = "per_run_confirmation"
    impact: str = "May change local network settings after a fresh preflight."
    reversible: str = "The repair engine saves recoverable values where the action supports rollback."


@dataclass(frozen=True)
class AuthorizationGrant:
    id: str
    run_id: str
    proposal_hash: str
    expires_at: float
    scope: str = 'single_run'
    trust_id: str | None = None


@dataclass
class AgentRun:
    id: str
    incident_id: str
    stage: str
    health: str
    snapshot: dict
    targets: tuple = field(default_factory=tuple)
    observations: tuple = field(default_factory=tuple)
    issues: tuple = field(default_factory=tuple)
    proposals: tuple = field(default_factory=tuple)
    events: list = field(default_factory=list)
    outcome: str | None = None
    results: list = field(default_factory=list)
    stop_reason: str = ""
    grant: dict | None = None
    receipt: dict | None = None
    trigger: str = 'manual'
    budget: dict = field(default_factory=dict)
    model_state: dict = field(default_factory=dict)
    hypotheses: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    tool_steps: list = field(default_factory=list)
    conclusion: dict = field(default_factory=dict)
    execution_history: list = field(default_factory=list)
    manual_review: dict | None = None
    reconciliation: dict | None = None
    authorization_status: dict = field(default_factory=dict)

    def event(self, message, stage=None):
        if stage:
            self.stage = stage
        self.events.append({"time": datetime.now().isoformat(timespec='seconds'), "message": message})


class NetworkAssuranceAgent:
    """A deep module for the PRD-002 local assurance loop.

    Interface:
    - observe(progress=None) performs a fresh read-only check and returns an AgentRun.
    - propose(issue_types=None, progress=None) refreshes evidence and prepares an authorization proposal.
    - investigate(progress=None, budget=None) observes and prepares a guard proposal without executing it.
    - authorize(run) binds an explicit user decision to the exact proposal.
    - execute(run, grant, progress=None) consumes one grant through mature repair tools.

    Callers do not inspect OS commands, profile details or repair safety rules;
    those stay behind DetectionEngine and FixEngine.
    """

    def __init__(self, config, detection_engine, fix_engine, id_factory=None,
                 store=None, clock=time.time, proposal_ttl=300, profiles=None, execution_directory=None):
        self.config = config
        self.detection_engine = detection_engine
        self.fix_engine = fix_engine
        self.id_factory = id_factory or (lambda prefix: prefix + "-" + uuid.uuid4().hex[:12])
        self._active_fingerprint = None
        self._active_incident_id = None
        self._last_run = None
        self.store, self.clock, self.proposal_ttl = store, clock, proposal_ttl
        self.profiles = profiles
        self.dynamic = None
        self.trust = None
        self._reasoning_session = None
        self.execution_directory = execution_directory
        self._proposals = {}
        self._grants = {}
        self._revoked = set()
        self._authorization_lock = threading.RLock()
        self._execution_lock = threading.Lock()
        self.journal_error = ''
        self.profile_error = ''
        self.safety_error = ''
        self.recovery_pending = bool(store and store.needs_reconciliation())
        if store:
            self._active_fingerprint, self._active_incident_id = store.latest_incident()

    def _save(self, run, required=False):
        self._last_run = run
        if self.store:
            try:
                self.store.save(run, _fingerprint(run.snapshot))
                self.journal_error = ''
            except Exception:
                self.journal_error = '处理记录无法保存；修复执行已停用'
                if required:
                    raise

    def _proposal_hash(self, run):
        return hashlib.sha256(encode({'run': run.id, 'issues': run.issues,
                                     'proposals': [asdict(p) for p in run.proposals]}).encode()).hexdigest()

    def authorize(self, run, *, acknowledge_unrestricted=False, trust_id=None, allowed=lambda: True):
        with self._authorization_lock:
            session = self._reasoning_session
            if session is not None and session.run is run and not session.execution_window_open():
                raise PermissionError('本次调查已超过总时限，请重新检测后授权')
            if self._is_dynamic(run) and acknowledge_unrestricted is not True:
                raise PermissionError('动态命令需要单独确认完整内容及当前用户权限风险')
            if trust_id and (self._is_dynamic(run) or not self.trust):
                raise PermissionError('此动作不能使用范围信任')
            if self.safety_error:
                raise PermissionError(self.safety_error)
            if self.profiles and run.snapshot.get('health_profile') != self.profiles.binding():
                raise PermissionError('当前健康档案或版本已变化，请重新生成方案')
            if self.store and self.store.needs_reconciliation():
                raise PermissionError('上次修改中断，需要先核对恢复记录')
            digest = self._proposal_hash(run)
            if (run.stage != 'awaiting_authorization' or not run.proposals
                    or self._proposals.get(run.id) != digest
                    or self.clock() >= run.proposals[0].expires_at):
                raise ValueError('方案已过期或内容改变，请重新检测并确认')
            grant_id = self.id_factory('grant')
            mode, deadline = 'single_run', run.proposals[0].expires_at
            if trust_id:
                mode, trust_deadline = self.trust.reserve(run, trust_id, grant_id, digest, allowed)
                deadline = min(deadline, trust_deadline)
            grant = AuthorizationGrant(grant_id, run.id, digest, deadline, mode, trust_id)
            run.grant = asdict(grant)
            run.event('已按现有范围信任签发本次方案的一次性执行授权' if trust_id else
                      '用户已授权本次方案，授权只用于此任务且五分钟内有效', 'authorized')
            self._save(run, required=True)
            self._grants[grant.id] = grant
            return grant

    def cancel(self, run):
        with self._authorization_lock:
            if run.grant:
                self._revoked.add(run.grant['id'])
            if run.stage in ('awaiting_authorization', 'authorized'):
                self._proposals.pop(run.id, None)
                run.outcome = 'cancelled'
                run.event('已取消当前授权方案，未执行该方案；此前动作以各自收据为准', 'cancelled')
                if self._reasoning_session is not None and self._reasoning_session.run is run:
                    run.model_state.update(state='cancelled', reason='user_cancelled')
                    run.budget = self._reasoning_session.budget.metrics()
                    self._reasoning_session = None
                self._save(run)
                if self.trust:
                    self.trust.finish(run)

    def revoke(self, grant):
        with self._authorization_lock:
            self._revoked.add(grant.id)

    def revoke_all(self, reason='permissions_changed'):
        with self._authorization_lock:
            self._revoked.update(self._grants)
            self._proposals.clear()
            self._reasoning_session = None
            if self.trust:
                self.trust.revoke(reason)

    def _validate_grant(self, run, grant):
        with self._authorization_lock:
            session = self._reasoning_session
            if session is not None and session.run is run and not session.execution_window_open():
                raise PermissionError('本次调查已超过总时限，停止后续动作')
            if self.profiles and run.snapshot.get('health_profile') != self.profiles.binding():
                raise PermissionError('当前健康档案或版本已变化，请重新生成方案')
            if (self.journal_error or self.safety_error or not isinstance(grant, AuthorizationGrant)
                    or self._grants.get(grant.id) != grant or grant.id in self._revoked
                    or self.clock() >= grant.expires_at or grant.run_id != run.id
                    or self._proposal_hash(run) != grant.proposal_hash):
                raise PermissionError('授权缺失、已过期、已撤销或方案发生变化')
            if grant.trust_id:
                self.trust.validate(run, grant)

    @staticmethod
    def _is_dynamic(run):
        return any(action.get('kind') == 'dynamic_command' for proposal in run.proposals for action in proposal.actions)

    def health_profile(self):
        targets = []
        enforce_company = bool(self._get("dns.enforce_company_dns_on_vpn", False))
        company_dns = self._get("vpn.company_dns", []) or []
        public_dns = self._get("dns.public_dns", []) or []
        if enforce_company and company_dns:
            targets.append(HealthTarget(
                "company-dns", "公司资源地址查找", "dns",
                "VPN 已确认时应使用可信公司 DNS", "explicit_profile", True))
        if public_dns:
            targets.append(HealthTarget(
                "daily-dns", "日常网页地址查找", "dns",
                "非 VPN 场景可恢复到用户保存的日常 DNS", "explicit_profile", True))
        ipv6_policy = self._get("ipv6.should_be", "observe")
        if ipv6_policy != "observe":
            targets.append(HealthTarget(
                "ipv6-policy", "联网方式策略", "ipv6",
                f"IPv6 应为 {ipv6_policy}", "explicit_profile", True))
        for target in self._get("reachability.targets", []) or []:
            name = target.get("name", "未命名目标")
            key = target.get('id') or _target_key(name)
            targets.append(HealthTarget(
                f"reachability-{key}", name, "reachability",
                "目标网络路径应可达；认证或服务拒绝需与网络故障区分", "configured_target", True))
        return tuple(targets)

    def assess(self, snapshot=None):
        snapshot = snapshot or self.detection_engine.snapshot()
        targets = self.health_profile()
        observations = self._observations(snapshot, targets)
        issues = tuple(snapshot.get("issues", []))
        health = self._health(snapshot)
        repairable = self.repair_options()
        if not snapshot.get("last_check"):
            stage = "idle"
            next_step = "manual_check"
        elif health == "healthy":
            stage = "guard_ready"
            next_step = "watch"
        elif snapshot.get("check_errors"):
            stage = "needs_evidence"
            next_step = "rerun_or_export_report"
        elif any(issue[1] in repairable for issue in issues):
            stage = "needs_authorization"
            next_step = "propose_repair"
        else:
            stage = "needs_user_or_admin"
            next_step = "guided_handling"
        result = {
            "stage": stage,
            "health": health,
            "next_step": next_step,
            "incident_id": self._incident_id(snapshot) if snapshot.get('last_check') else '',
            "targets": [target.__dict__ for target in targets],
            "observations": [observation.__dict__ for observation in observations],
            "repairableIssues": sorted(repairable),
        }
        if self.profiles:
            result['profile'] = self.profiles.evaluate(snapshot)
        return result

    def repair_options(self):
        if self.safety_error:
            return {}
        try:
            return self.fix_engine.repair_options()
        except Exception:
            return {}

    def observe(self, progress=None, budget=None, trigger=None):
        snapshot = (self.detection_engine.run_all(progress=progress, budget=budget) if budget is not None
                    else self.detection_engine.run_all(progress=progress))
        if self.store and self.recovery_pending and budget is None:
            self.reconcile(snapshot)
        self._record_verification(snapshot)
        run = self._run_from_snapshot(snapshot)
        if budget is not None:
            run.trigger, run.budget = trigger or 'guard', budget.metrics()
        run.event("已采集当前网络环境、配置和访问证据", "observed")
        self._save(run)
        return run

    def _record_verification(self, snapshot):
        if self.profiles:
            try:
                self.profiles.record_verification(snapshot)
                self.profile_error = ''
            except Exception:
                self.profile_error = '健康档案最近验证无法保存；当前观测仍可查看'

    def reconcile(self, snapshot):
        for interrupted in self.store.pending_reconciliation():
            receipt = interrupted.get('receipt') or {}
            if receipt.get('kind') == 'dynamic_command' and self.dynamic is not None:
                try:
                    terminal = self.dynamic.inspect_receipt(receipt)
                    self.store.attach_job_receipt(interrupted['id'], terminal)
                except Exception:
                    pass
            result = ({'outcome': 'needs_attention',
                       'message': '动态命令的任意副作用不能由网络检测自动确认；请核对原始收据和恢复资料，未重放或自动回滚'}
                      if receipt.get('kind') == 'dynamic_command'
                      else self.fix_engine.inspect_recovery(receipt, snapshot))
            self.store.reconcile(interrupted['id'], result)
        self.recovery_pending = self.store.needs_reconciliation()

    def propose(self, issue_types=None, progress=None):
        run = self.observe(progress=progress)
        return self._propose_run(run, issue_types)

    def investigate(self, progress=None, budget=None, model=None, consent=None, reasoning_policy=None, trigger=None):
        self._reasoning_session = None
        run = self.observe(progress=progress, budget=budget, trigger=trigger)
        if model is not None:
            from .reasoning import ReasoningSession
            session = ReasoningSession(self, run, model, consent, budget, reasoning_policy, progress)
            self._reasoning_session = session
            result = session.advance()
            if not session.waiting_hash:
                self._reasoning_session = None
            return result
        return self._local_proposal(run)

    def resume_investigation(self, run, model, consent):
        session = self._reasoning_session
        if (session is None or session.run is not run or session.model is not model or session.consent is not consent):
            raise PermissionError('此任务没有匹配的活动调查，或模型/上传同意已改变')
        result = session.resume()
        if not session.waiting_hash:
            self._reasoning_session = None
        return result

    def can_resume_investigation(self, run):
        session = self._reasoning_session
        return bool(session is not None and session.run is run and session.waiting_hash)

    def _local_proposal(self, run):
        if self.repair_options() and not run.snapshot.get('check_errors') and not self.recovery_pending:
            return self._propose_run(run)
        return run

    def _propose_run(self, run, issue_types=None):
        if self.safety_error:
            run.outcome, run.stop_reason = 'blocked', 'safety_state_unavailable'
            run.event(self.safety_error, 'blocked')
            self._save(run)
            return run
        selected = frozenset(issue_types) if issue_types is not None else None
        issues = tuple(issue for issue in run.issues if selected is None or issue[1] in selected)
        if not issues:
            run.stage = "blocked"
            run.health = self._health(run.snapshot)
            run.outcome = "blocked"
            run.stop_reason = "selected_issues_absent"
            run.event("所选问题已消失或当前证据不足，未生成修改计划", "blocked")
            self._save(run)
            return run
        plans = tuple(self.fix_engine.describe_fixes(issues))
        if plans == ("没有可安全自动修复的问题",):
            run.stage = "blocked"
            run.outcome = "blocked"
            run.stop_reason = "no_mature_repair"
            run.event("没有成熟且可验证的本地修复动作，等待用户或管理员处理", "blocked")
            self._save(run)
            return run
        actions = tuple(self.fix_engine.plan_actions(issues))
        if not actions:
            run.outcome, run.stop_reason = 'blocked', 'missing_action_identity'
            run.event('无法绑定具体修改对象，未生成授权方案', 'blocked')
            self._save(run)
            return run
        proposal = ActionProposal(self.id_factory("proposal"), tuple(dict.fromkeys(i[1] for i in issues)),
                                  plans, actions, self.clock() + self.proposal_ttl)
        run.issues = issues
        run.proposals = (proposal,)
        run.stage = "awaiting_authorization"
        run.event("已形成处理方案，等待逐次授权", "awaiting_authorization")
        self._proposals[run.id] = self._proposal_hash(run)
        self._save(run)
        return run

    def execute(self, run, grant=None, progress=None):
        if not self._execution_lock.acquire(blocking=False):
            raise RuntimeError('已有修复正在执行')
        try:
            self._validate_grant(run, grant)
            if run.stage != 'authorized' or self.store and self.store.needs_reconciliation():
                raise PermissionError('任务未授权或仍有待核对执行')
            if run.receipt and not any(row.get('grant_id') == run.receipt.get('grant_id') for row in run.execution_history):
                run.execution_history.append(copy.deepcopy(run.receipt))
            if self._is_dynamic(run):
                if self.dynamic is None:
                    raise PermissionError('此运行时没有动态命令执行器')
                result = self.dynamic.execute(run, grant)
            else:
                with execution_slot(self.execution_directory):
                    result = self._execute(run, grant, progress)
            if self.can_resume_investigation(run):
                run.model_state.update(state='awaiting_continuation', reason='action_finished')
                self._save(run)
            return result
        finally:
            if isinstance(grant, AuthorizationGrant) and grant.trust_id and self.trust:
                self.trust.release(grant.id)
            self._execution_lock.release()

    def _execute(self, run, grant, progress):
        self._validate_grant(run, grant)
        if run.stage != 'authorized':
            raise PermissionError('该任务不处于已授权状态')
        run.stage = "executing"
        run.event("开始执行已授权方案；执行前会重新核对前提", "executing")
        run.receipt = {'grant_id': grant.id, 'proposal_hash': grant.proposal_hash,
                       'authorization': {'mode': grant.scope, 'trust_id': grant.trust_id},
                       'started_at': self.clock(), 'finished_at': None,
                       'actions': copy.deepcopy([a for p in run.proposals for a in p.actions]),
                       'outcome': 'executing', 'recovery_path': None}
        self._save(run, required=True)
        last_outcome = {"value": None}

        def relay_progress(event):
            if 'changes' in event:
                run.receipt['changes'] = copy.deepcopy(event['changes'])
            if event.get("outcome"):
                last_outcome["value"] = event["outcome"]
            if event.get('recovery_path'):
                run.receipt['recovery_path'] = event['recovery_path']
            if event.get('message'):
                run.event(event['message'])
            self._save(run)
            if progress:
                try:
                    progress(event)
                except Exception:
                    pass

        try:
            results = self.fix_engine.fix_all(
                run.issues, progress=relay_progress, approved_actions=run.receipt['actions'],
                authorize=lambda: self._validate_grant(run, grant), proposal_hash=grant.proposal_hash)
        except BaseException:
            run.outcome, run.stop_reason = 'interrupted', 'execution_interrupted'
            run.receipt.update(outcome='interrupted', finished_at=self.clock())
            self.recovery_pending = True
            run.event('执行中断，需核对实际配置和恢复记录，禁止重放命令', 'needs_reconciliation')
            self._save(run)
            raise
        finally:
            with self._authorization_lock:
                self._grants.pop(grant.id, None)
                self._proposals.pop(run.id, None)
        run.results = list(results)
        run.outcome = last_outcome["value"] or _infer_outcome(results)
        run.receipt.update(outcome=run.outcome, finished_at=self.clock())
        unresolved = run.outcome not in ('verified', 'restored', 'rolled_back', 'blocked', 'not_started')
        run.stage = 'needs_reconciliation' if unresolved else 'finished'
        self.recovery_pending = self.recovery_pending or unresolved
        try:
            run.snapshot = self.detection_engine.snapshot()
            run.health = self._health(run.snapshot)
            run.observations = self._observations(run.snapshot, run.targets)
            if not unresolved:
                self._record_verification(run.snapshot)
        except Exception:
            run.event("执行后快照读取失败，请重新检测确认状态")
        run.event("处理结束，结果来自修复引擎的验证或回退结论", run.stage)
        self._save(run)
        return run

    def _run_from_snapshot(self, snapshot):
        targets = self.health_profile()
        return AgentRun(
            id=self.id_factory("run"),
            incident_id=self._incident_id(snapshot),
            stage="observed",
            health=self._health(snapshot),
            snapshot=snapshot,
            targets=targets,
            observations=self._observations(snapshot, targets),
            issues=tuple(snapshot.get("issues", [])),
        )

    def _incident_id(self, snapshot):
        fingerprint = _fingerprint(snapshot)
        if not fingerprint:
            self._active_fingerprint = None
            self._active_incident_id = None
            return ""
        if fingerprint != self._active_fingerprint:
            self._active_fingerprint = fingerprint
            self._active_incident_id = self.id_factory("incident")
        return self._active_incident_id

    def _health(self, snapshot):
        if not snapshot.get("last_check") or snapshot.get("check_errors"):
            return "unknown"
        issues = snapshot.get("issues", [])
        if any(issue[0] == "high" for issue in issues):
            return "degraded"
        if self.profiles:
            health = self.profiles.evaluate(snapshot)['health']
            if health != 'healthy':
                return health
        if issues:
            return "attention"
        return "healthy"

    def _observations(self, snapshot, targets):
        status = snapshot.get("status", {})
        issues = {issue[1] for issue in snapshot.get("issues", [])}
        observations = []
        if any(target.kind == "dns" for target in targets):
            dns_state = status.get("dns", "unknown" if snapshot.get("last_check") else "pending")
            observations.append(Observation(
                "company-dns" if self._get("dns.enforce_company_dns_on_vpn", False) else "daily-dns",
                dns_state,
                "DNS policy evidence is explicit" if self._get("vpn.company_dns", []) else "No company DNS profile is trusted yet",
                {"servers": status.get("dns_effective_servers", []), "mode": status.get("dns_mode")},
                "" if "dns_unavailable" not in issues else "系统解析器不可用"))
        if any(target.id == "ipv6-policy" for target in targets):
            observations.append(Observation(
                "ipv6-policy", status.get("ipv6", "unknown"),
                "IPv6 mode compared with saved policy",
                {"mode": status.get("ipv6_mode")}))
        results = status.get("reachability_results", {})
        profile_rows = {r['id']: r for r in self.profiles.evaluate(snapshot)['targets']} if self.profiles else {}
        for target in targets:
            if target.kind != "reachability":
                continue
            key = target.id.removeprefix("reachability-")
            result = results.get(key, {})
            if key in profile_rows:
                row = profile_rows[key]
                observations.append(Observation(target.id, row['state'], row['summary'], result))
                continue
            observations.append(Observation(
                target.id,
                "ok" if result.get("transport") == "ok" else result.get("transport", "unknown"),
                _reachability_summary(result),
                result,
                "PAC/WPAD path not fully verified" if result.get("path") == "direct_pac_unresolved" else ""))
        return tuple(observations)

    def _get(self, key, default=None):
        return self.config.get(key, default) if self.config else default


def _target_key(name):
    return str(name).lower().replace(" ", "_")


def _fingerprint(snapshot):
    if not snapshot.get("last_check"):
        return ""
    parts = [issue[1] for issue in snapshot.get("issues", [])]
    parts.extend("check_failed_" + key for key in sorted(snapshot.get("check_errors", {})))
    if not parts:
        return ""
    return hashlib.sha256("\n".join(sorted(parts)).encode()).hexdigest()[:16]


def _reachability_summary(result):
    if not result:
        return "No reachability observation yet"
    if result.get("transport") != "ok":
        return "Transport is not verified"
    service = result.get("service")
    if service == "auth_required":
        return "Network path is reachable; service requires authentication"
    if service == "responding":
        return "Network path and service response are healthy"
    return "Network path is reachable; service response needs attention"


def _infer_outcome(results):
    joined = "\n".join(results)
    if "回滚失败" in joined:
        return "rollback_failed"
    if "已验证回滚" in joined:
        return "rolled_back"
    if "已验证" in joined and "✅" in joined:
        return "verified"
    if "未执行修复" in joined or "没有可安全自动修复" in joined:
        return "blocked"
    return "finished"
