"""Local diagnostics and verified, reversible built-in repair operations.

This module intentionally has no account, license or network-login dependency.
"""
import copy
import json
import os
import re
import stat
import tempfile
from pathlib import Path
import threading
from datetime import datetime
import uuid

from .checks import create_instances, get_load_errors
from .commands import CommandResult, checked, run_command
from .network import dns_list, ip_addresses, key_values
from .environment import macos_environment


def _notify(progress, **event):
    # A closed or failing UI must never interrupt a repair or its rollback.
    if progress is not None:
        try:
            progress(event)
        except Exception:
            pass


class DetectionEngine:
    CHECK_ORDER = ['wifi', 'vpn', 'proxy', 'dns', 'ipv6', 'system_proxy', 'reachability']

    def __init__(self, config=None, runner=None, checks=None):
        self.config = config
        self.runner = runner or run_command
        self.lock = threading.RLock()
        self.checking = False
        self._snapshot = {"status": {}, "issues": [], "last_check": None, "check_errors": {}}
        self._provided_checks = checks
        self._load_checks()

    def _load_checks(self):
        self._load_errors = {}
        if getattr(self.config, 'load_error', None):
            self._load_errors['config'] = self.config.load_error
        try:
            checks = list(self._provided_checks) if self._provided_checks is not None else create_instances(self.config, self.runner)
            if self._provided_checks is None:
                self._load_errors.update(get_load_errors())
        except Exception as exc:
            checks = []
            self._load_errors['plugins'] = str(exc)
        order = {name: i for i, name in enumerate(self.CHECK_ORDER)}
        self.checks = sorted(checks, key=lambda check: order.get(check.name, 999))

    def reload_checks(self, clear_snapshot=False):
        with self.lock:
            self._load_checks()
            if clear_snapshot:
                self._snapshot = {'status': {}, 'issues': [], 'last_check': None, 'check_errors': {}}

    @property
    def status(self):
        return copy.deepcopy(self._snapshot["status"])

    @property
    def issues(self):
        return list(self._snapshot["issues"])

    @property
    def last_check(self):
        return self._snapshot["last_check"]

    def snapshot(self):
        # The whole object is replaced once after a run, never mutated in place.
        return copy.deepcopy(self._snapshot)

    def run_all(self, progress=None, budget=None, target_ids=None):
        with self.lock:
            self.checking = True
            try:
                profile_binding = self.config.get('health_profile.binding') if self.config else None
                status, issues, errors = {}, [], dict(self._load_errors)
                for index, check in enumerate(self.checks):
                    if check.name == 'reachability' and target_ids is not None:
                        from .checks.reachability import ReachabilityCheck
                        from .profiles import SelectedTargets
                        check = ReachabilityCheck(SelectedTargets(self.config, target_ids), self.runner)
                    _notify(progress, check=check.name, phase='running', completed=index,
                            total=len(self.checks), snapshot={"status": copy.deepcopy(status),
                            "issues": list(issues), "check_errors": dict(errors), "last_check": None})
                    candidate = copy.deepcopy(status)
                    original_runner = getattr(check, 'runner', self.runner)
                    try:
                        if budget is not None:
                            if not budget.allowed():
                                budget.stop_reason = 'paused'
                                raise RuntimeError('守护已暂停')
                            check.runner = lambda argv, timeout=10, runner=original_runner: budget.run(runner, argv, timeout)
                        found = check.check(candidate)
                        if not isinstance(found, list) or any(not isinstance(i, (tuple, list)) or len(i) != 3 for i in found):
                            raise ValueError("检测插件返回了无效的问题列表")
                        status = candidate
                        issues.extend(tuple(i) for i in found)
                    except Exception as exc:
                        errors[check.name] = str(exc)
                        status[check.name] = "unknown"
                        if check.name == "proxy":
                            status["proxy_app"] = "unknown"
                        elif check.name == "system_proxy":
                            status["proxy"] = "unknown"
                        elif check.name == "vpn":
                            status["vpn_path"] = "unknown"
                    finally:
                        if budget is not None:
                            check.runner = original_runner
                    _notify(progress, check=check.name, phase='checked', completed=index + 1,
                            total=len(self.checks), snapshot={"status": copy.deepcopy(status),
                            "issues": list(issues), "check_errors": dict(errors), "last_check": None})
                for name, error in errors.items():
                    issues.append(("medium", f"check_failed_{name}", f"{name} 检查未完成：{error}"))
                if not self.checks and not errors:
                    errors["plugins"] = "没有启用的检测插件"
                    issues.append(("medium", "check_failed_plugins", "没有启用的检测插件"))
                self._snapshot = {"status": status, "issues": issues,
                                  "last_check": datetime.now(), "check_errors": errors}
                if profile_binding:
                    self._snapshot['health_profile'] = profile_binding
                if target_ids is not None:
                    self._snapshot['target_selection'] = list(target_ids)
                self._snapshot['environment'] = macos_environment(self._snapshot, self.config)
                return self.snapshot()
            finally:
                self.checking = False

    def get_status_summary(self):
        snapshot = self.snapshot()
        lines = []
        for check in self.checks:
            try:
                lines.extend(check.get_status_lines(snapshot["status"]) or [])
            except Exception:
                lines.append(f"🟡 {check.name}: 摘要不可用")
        issues = snapshot["issues"]
        if issues:
            high = sum(i[0] == "high" for i in issues)
            medium = sum(i[0] == "medium" for i in issues)
            low = sum(i[0] == "low" for i in issues)
            lines.extend(["", f"⚠️ {high} 个严重问题，{medium} 个警告，{low} 个提示"])
        if snapshot["last_check"]:
            lines.extend(["", f"最后检测: {snapshot['last_check'].strftime('%H:%M:%S')}"])
        return lines

    def get_overall_status(self):
        snapshot = self.snapshot()
        if not snapshot["last_check"] or snapshot["check_errors"]:
            return "unknown"
        if any(i[0] == "high" for i in snapshot["issues"]):
            return "error"
        if any(value == 'unknown' for value in snapshot['status'].values()):
            return 'unknown'
        return "warning" if snapshot["issues"] else "ok"

    def get_detailed_report(self):
        snapshot = self.snapshot()
        date = snapshot["last_check"].strftime('%Y-%m-%d %H:%M:%S') if snapshot["last_check"] else "尚未检测"
        lines = ["NetCare 网络诊断报告", f"检测时间: {date}", ""]
        for key, value in snapshot["status"].items():
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value}")
        lines.append("")
        if snapshot["issues"]:
            lines.append("诊断发现:")
            lines.extend(f"• [{kind}] {description}" for _, kind, description in snapshot["issues"])
        elif not snapshot["last_check"]:
            lines.append("尚未检测，不能判断网络健康。")
        else:
            lines.append("已完成的检测未发现问题。")
        return "\n".join(lines)


class FixEngine:
    """Mature repairs use only typed, built-in network operations.

    Config's historical shell templates are never read or executed. All actions
    run under the diagnostic engine's lock, with a fresh preflight and snapshot.
    """
    DNS_TO_COMPANY_ISSUES = {'dns_mixed_on_vpn', 'dns_no_company_on_vpn'}
    DNS_TO_PUBLIC_ISSUES = {'dns_company_leftover'}
    PROXY_GETTERS = {'http': '-getwebproxy', 'https': '-getsecurewebproxy', 'socks': '-getsocksfirewallproxy'}
    PROXY_SETTERS = {'http': '-setwebproxystate', 'https': '-setsecurewebproxystate', 'socks': '-setsocksfirewallproxystate'}

    def __init__(self, config=None, detection_engine=None, runner=None, snapshot_dir=None, mutation_writer=None):
        self.config = config
        self.detection_engine = detection_engine
        self.runner = runner or (detection_engine.runner if detection_engine else run_command)
        self.snapshot_dir = Path(snapshot_dir) if snapshot_dir else Path.home() / 'Library/Application Support/Relay/repair-snapshots'
        self.mutation_writer = mutation_writer

    def _get(self, key, default=None):
        return self.config.get(key, default) if self.config else default

    def _service(self):
        service = self._get('wifi.service_name', 'Wi-Fi')
        if not isinstance(service, str) or not service or service == 'auto' or '\x00' in service:
            raise ValueError('无法确认要修改的网络服务')
        return service

    def _planned_actions(self, issues=None, status=None):
        if not self.detection_engine:
            return []
        snapshot = self.detection_engine.snapshot()
        if snapshot['check_errors']:
            return []
        status = snapshot['status'] if status is None else status
        issues = snapshot['issues'] if issues is None else issues
        actions, seen = [], set()
        for issue in issues:
            if len(issue) != 3 or issue[1] in seen:
                continue
            kind = issue[1]
            seen.add(kind)
            if self._get(f'fix_rules.{kind}.enabled', True) is False:
                continue
            fields = []
            if kind in self.DNS_TO_COMPANY_ISSUES:
                if not (status.get('vpn') == 'ok' and status.get('vpn_path') == 'ok'
                        and status.get('vpn_evidence', {}).get('owner_confirmed')):
                    continue
                servers = ip_addresses(self._get('vpn.company_dns', []))
                if servers:
                    fields = [('dns', servers, '恢复公司 VPN 所需的网站地址设置')]
            elif kind in self.DNS_TO_PUBLIC_ISSUES:
                if status.get('vpn') != 'off' or status.get('vpn_path') != 'off':
                    continue
                servers = ip_addresses(self._get('dns.public_dns', []))
                fields = [('dns', servers, '恢复你保存的日常上网地址设置' if servers else '让网络自动提供网站地址设置')]
            elif kind.startswith('dns_reference_'):
                candidate = status.get('dns_reference_candidate', {})
                if (candidate.get('kind') == 'dns_reference_candidate'
                        and kind == 'dns_reference_' + str(candidate.get('target_id'))
                        and candidate.get('resolver') == '1.1.1.1'
                        and status.get('vpn') == 'off' and status.get('vpn_path') == 'off'
                        and status.get('proxy') == 'off' and status.get('dns_mode') == 'manual'):
                    fields = [('dns', ['1.1.1.1'],
                               '将此网络服务的 DNS 改为已实测可访问的参考解析器；影响全部域名，验证失败会恢复原配置')]
            elif kind == 'ipv6_enabled' and self._get('ipv6.should_be', 'observe') == 'off':
                fields = [('ipv6', 'Off', '按你保存的设置关闭 IPv6')]
            elif kind == 'proxy_leftover':
                for proxy_kind in status.get('proxy_failed_types', []):
                    endpoint = status.get('proxy_details', {}).get(proxy_kind, {})
                    if proxy_kind in self.PROXY_SETTERS and endpoint.get('local') and endpoint.get('state') == 'unreachable':
                        fields.append(('proxy:' + proxy_kind, False, f'关闭没有响应的本机转发（{proxy_kind.upper()}）'))
            for field, desired, description in fields:
                if not any(action['field'] == field for action in actions):
                    action = {'issue_type': kind, 'field': field, 'desired': desired,
                              'description': description, 'service': self._service()}
                    action['environment'] = {key: status.get(key) for key in
                                             ('wifi_interface', 'wifi_ip', 'wifi_network', 'default_interface')}
                    if self.mutation_writer:
                        action['platform_target'] = self.mutation_writer.target(action['service'], status.get('wifi_interface'))
                    action['health_profile'] = self._get('health_profile.binding')
                    if field == 'dns' or status.get('vpn_path') is not None:
                        action['vpn_context'] = self._vpn_context(status)
                    if field == 'dns':
                        action['before'] = copy.deepcopy(status.get('dns_manual_servers'))
                    elif field == 'ipv6':
                        action['before'] = status.get('ipv6_mode')
                    elif field.startswith('proxy:'):
                        endpoint = status['proxy_details'][field.removeprefix('proxy:')]
                        action['proxy_endpoint'] = {'host': endpoint['host'], 'port': endpoint['port']}
                    actions.append(action)
        return actions

    def describe_fixes(self, issues=None):
        try:
            actions = self._planned_actions(issues)
        except (ValueError, TypeError):
            actions = []
        plans = []
        for action in actions:
            target = action['desired']
            if action['field'] == 'dns':
                target = ', '.join(target) if target else '自动获取（DHCP）'
            elif action['field'].startswith('proxy:'):
                target = '关闭'
            elif target == 'Off':
                target = '关闭'
            plans.append(f"{action['service']} · {action['description']}\n目标配置：{target}")
        return plans or ['没有可安全自动修复的问题']

    def plan_actions(self, issues=None):
        """Capture exact targets and values for an authorization proposal."""
        return copy.deepcopy(self._planned_actions(issues))

    def repair_options(self):
        """Read-only eligible actions, grouped by issue for the diagnostic panel."""
        try:
            actions = self._planned_actions()
        except (ValueError, TypeError):
            actions = []
        options = {}
        for action in actions:
            options.setdefault(action['issue_type'], []).append(action['description'])
        return options

    def _read_field(self, field, service):
        if field == 'dns':
            return dns_list(checked(self.runner, ['/usr/sbin/networksetup', '-getdnsservers', service]))
        if field == 'ipv6':
            info = key_values(checked(self.runner, ['/usr/sbin/networksetup', '-getinfo', service]))
            mode = info.get('IPv6')
            if mode not in ('Off', 'Automatic', 'Link-local only', 'Link-local'):
                raise ValueError('无法完整恢复当前 IPv6 模式，因此拒绝修改')
            return 'Link-local only' if mode == 'Link-local' else mode
        return self._read_proxy(field, service)['enabled']

    def _read_proxy(self, field, service):
        proxy_kind = field.removeprefix('proxy:')
        raw = checked(self.runner, ['/usr/sbin/networksetup', self.PROXY_GETTERS[proxy_kind], service])
        fields = key_values(raw)
        enabled, host = fields.get('Enabled'), fields.get('Server')
        try:
            port = int(fields.get('Port', ''))
        except (ValueError, TypeError):
            raise ValueError('无法确认此网络服务的代理端口') from None
        if enabled not in ('Yes', 'No') or not host or not 1 <= port <= 65535:
            raise ValueError('无法确认此网络服务的完整代理配置')
        return {'enabled': enabled == 'Yes', 'host': host, 'port': port}

    @staticmethod
    def _vpn_context(status):
        evidence = status.get('vpn_evidence', {})
        return {'state': status.get('vpn'), 'path': status.get('vpn_path'),
                'owner': status.get('vpn_client'), 'owner_confirmed': evidence.get('owner_confirmed'),
                'interface': evidence.get('interface'), 'routes': copy.deepcopy(evidence.get('routes', {}))}

    def _assert_action_context(self, action, before_write=False):
        if self._service() != action['service']:
            raise RuntimeError('待修复的网络服务已变化，请重新检查并确认')
        if 'platform_target' in action:
            if (not self.mutation_writer or self.mutation_writer.target(action['service'],
                    action['environment']['wifi_interface']) != action['platform_target']):
                raise RuntimeError('系统网络服务标识已变化，请重新检查并确认')
        check = next((c for c in self.detection_engine.checks if c.name == 'wifi'), None)
        if check is not None:
            current = {}
            check.check(current)
            if {key: current.get(key) for key in action['environment']} != action['environment']:
                raise RuntimeError('网络环境已变化，请重新检查并确认')
        if 'vpn_context' in action:
            # Read fresh routing/ownership evidence immediately around each write,
            # rather than relying on the older whole-run snapshot or issue absence.
            check = next((c for c in self.detection_engine.checks if c.name == 'vpn'), None)
            if check is None:
                raise RuntimeError('缺少 VPN 检测，无法验证 DNS 修复前提')
            current = {}
            check.check(current)
            if self._vpn_context(current) != action['vpn_context']:
                raise RuntimeError('VPN 归属、路径或接口证据已变化，修复前提不再成立')
        if action['field'].startswith('proxy:'):
            current = self._read_proxy(action['field'], action['service'])
            expected = action['proxy_endpoint']
            if any(current[key] != expected[key] for key in ('host', 'port')):
                raise RuntimeError('此网络服务的代理端点与获准修复的端点不一致')
            if before_write:
                probe = self.runner(['/usr/bin/nc', '-G', '2', '-z', expected['host'], str(expected['port'])], timeout=3)
                if not isinstance(probe, CommandResult) or probe.returncode != 1 or probe.timed_out:
                    raise RuntimeError('代理端点已恢复或检查未完成，停止修改代理开关')

    def _rollback_field(self, action, saved, record=None, path=None):
        field, service = action['field'], action['service']
        # Restore only values still attributable to this transaction. A third
        # value or changed proxy endpoint belongs to an external actor or an
        # ambiguous partial write; preserve it and require manual reconciliation.
        if field.startswith('proxy:'):
            current = self._read_proxy(field, service)
            if any(current[key] != action['proxy_endpoint'][key] for key in ('host', 'port')):
                raise RuntimeError('代理端点已被其他程序更改，未覆盖其配置；请按快照人工核对')
            actual = current['enabled']
        else:
            actual = self._read_field(field, service)
        if actual == saved:
            return actual
        if actual != action['desired']:
            raise RuntimeError('配置出现外部变更或无法确认的部分写入，未覆盖现值；请按快照人工核对')
        self._write_action(action, actual, saved, record, path, restore=True)
        actual = self._read_field(field, service)
        if actual != saved:
            raise RuntimeError('回滚后的配置与快照不一致')
        return actual

    def _write_action(self, action, expected, value, record, path, *, restore=False):
        if not self.mutation_writer:
            return self._write_field(action['field'], value, action['service'])
        from .mutations import MutationUnconfirmed
        identity = record['system_mutations']['operations'][action['field']]
        try:
            receipt = self.mutation_writer.write(action, expected, value,
                operation_id=identity['restore_id' if restore else 'apply_id'],
                proposal_hash=record['system_mutations']['proposal_hash'],
                restore_of=identity['apply_id'] if restore else '')
        except MutationUnconfirmed as exc:
            record['system_mutations']['receipts'].append(exc.receipt)
            self._persist(record, path)
            raise
        record['system_mutations']['receipts'].append(receipt)
        self._persist(record, path)

    def _write_field(self, field, value, service):
        if field == 'dns':
            args = ['-setdnsservers', service] + (ip_addresses(value) or ['Empty'])
        elif field == 'ipv6':
            option = {'Off': '-setv6off', 'Automatic': '-setv6automatic',
                      'Link-local only': '-setv6linklocal', 'Link-local': '-setv6linklocal'}[value]
            args = [option, service]
        else:
            args = [self.PROXY_SETTERS[field.removeprefix('proxy:')], service, 'on' if value else 'off']
        checked(self.runner, ['/usr/sbin/networksetup'] + args, timeout=15)

    def _persist(self, record, path=None):
        self.snapshot_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.snapshot_dir.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise PermissionError('恢复快照目录须由当前用户拥有且仅本人可访问（权限 0700）')
        path = path or self.snapshot_dir / ('repair-' + uuid.uuid4().hex + '.json')
        if path.is_symlink() or path.parent != self.snapshot_dir:
            raise ValueError('恢复快照路径无效')
        # mkstemp creates an exclusive mode-0600 file. Never truncate a predictable
        # .tmp path, which could be a pre-existing permissive file or symlink.
        fd, temporary_name = tempfile.mkstemp(prefix='.relay-repair-', suffix='.tmp', dir=self.snapshot_dir)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(record, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(self.snapshot_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    @staticmethod
    def _probe_baseline(snapshot):
        return snapshot['status'].get('reachability_results', {})

    def _recovery_record(self, receipt):
        path = Path(receipt.get('recovery_path') or '')
        if path.parent != self.snapshot_dir or path.is_symlink():
            raise ValueError('缺少可信恢复快照')
        for candidate, is_directory in ((self.snapshot_dir, True), (path, False)):
            info = candidate.lstat()
            valid = stat.S_ISDIR(info.st_mode) if is_directory else stat.S_ISREG(info.st_mode)
            if not valid or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise PermissionError('恢复记录权限无效')
        record = json.loads(path.read_text(encoding='utf-8'))
        if record['actions'] != receipt.get('actions'):
            raise ValueError('恢复快照与执行方案不一致')
        return path, record

    def recovery_review(self, receipt):
        """Private, authenticated review; the caller retains its one-use native token."""
        _path, record = self._recovery_record(receipt)
        native = record.get('system_mutations')
        if not native or not self.mutation_writer or native['proposal_hash'] != receipt.get('proposal_hash'):
            raise ValueError('缺少匹配的系统执行凭据')
        return self.mutation_writer.inspect_recovery(record['actions'], record['before'], native)

    def recover_configuration(self, receipt, review, choice, authorize, progress=None):
        """Explicit compensation/retention only; ordinary forward requests are never replayed."""
        with self.detection_engine.lock:
            path, record = self._recovery_record(receipt)
            native = record['system_mutations']
            if (choice not in ('restore', 'retain') or review.get('can_' + choice) is not True
                    or native['proposal_hash'] != receipt.get('proposal_hash')
                    or native['batch_id'] != review['batch_id']
                    or self.mutation_writer.requests(record['actions'], record['before'], native) != review['requests']):
                raise PermissionError('恢复方案与原始系统批次不一致')
            authorize()
            attempt = {'review_id': review['review_id'], 'helper_instance': review['helper_instance'],
                       'choice': choice, 'state': 'prepared'}
            record.setdefault('helper_recoveries', []).append(attempt)
            self._persist(record, path)
            try:
                authorize()
                attempt['result'] = self.mutation_writer.recover(review, choice)
                attempt['state'] = 'verifying'
                self._persist(record, path)
                _notify(progress, phase='verification', message='核对恢复后的配置和受保护连接')
                snapshot = self.detection_engine.run_all(progress=progress)
                authorize()
                if choice == 'retain':
                    verification = self._inspect_recovery_values(record, snapshot)
                    if verification['outcome'] != 'verified':
                        attempt['state'] = 'needs_verification'
                        self._persist(record, path)
                        return {'outcome': 'needs_attention', 'message': '现值尚未通过网络复验，系统批次仍保留；可重新审阅并恢复原值'}, snapshot
                attempt['closure'] = self.mutation_writer.end_recovery()
                attempt['state'] = 'settled'
                self._persist(record, path)
                return self.inspect_recovery(receipt, snapshot), snapshot
            except Exception:
                attempt['state'] = 'needs_verification'
                self._persist(record, path)
                raise

    def inspect_recovery(self, receipt, snapshot):
        """Read back an interrupted transaction; never write or replay commands."""
        try:
            _path, record = self._recovery_record(receipt)
            actions = record['actions']
            native = record.get('system_mutations')
            if native or any('platform_target' in action for action in actions):
                if not native or not self.mutation_writer or native['proposal_hash'] != receipt.get('proposal_hash'):
                    raise ValueError('缺少匹配的系统执行凭据')
                helper = self.mutation_writer.inspect_recovery(actions, record['before'], native)
                if helper['state'] != 'finished':
                    return {'outcome': 'needs_attention', 'message': '系统执行批次尚未收尾，需重新审阅恢复方案；只读检测不会解除该阻止'}
                if helper['disposition'] == 'not_started':
                    return {'outcome': 'not_started', 'message': '辅助服务已确认本批次未开始任何配置写入，未重放修改'}
            return self._inspect_recovery_values(record, snapshot)
        except Exception:
            return {'outcome': 'unknown', 'message': '恢复状态读取未完成，需继续核对；未重放命令'}

    def _inspect_recovery_values(self, record, snapshot):
        actions = record['actions']
        for action in actions:
            if 'platform_target' in action and (not self.mutation_writer or self.mutation_writer.target(
                    action['service'], action['environment']['wifi_interface']) != action['platform_target']):
                raise ValueError('系统网络服务标识已变更')
            if action['field'].startswith('proxy:'):
                endpoint = self._read_proxy(action['field'], action['service'])
                if any(endpoint[k] != action['proxy_endpoint'][k] for k in ('host', 'port')):
                    raise ValueError('代理端点已变更')
        actual = {a['field']: self._read_field(a['field'], a['service']) for a in actions}
        before = record['before']
        if all(actual[a['field']] == before[a['field']] for a in actions):
            return {'outcome': 'restored', 'message': '只读核对完成：涉及的配置已恢复原值；网络健康以本次检测为准'}
        if all(actual[a['field']] == a['desired'] for a in actions):
            for action in actions:
                self._assert_action_context(action)
            requested = {a['issue_type'] for a in actions}
            probes = self._probe_baseline(snapshot)
            if (not snapshot.get('check_errors') and snapshot.get('last_check')
                    and not requested & {i[1] for i in snapshot['issues']}
                    and not any(i[0] in ('high', 'medium') and i[1] not in record.get('issue_types', [])
                                for i in snapshot['issues'])
                    and probes and not self._regressed(record['baseline'], probes)
                    and any(p.get('transport') == 'ok' for p in probes.values())):
                return {'outcome': 'verified', 'message': '只读核对完成：获准配置仍生效，原问题解除且受保护连接未退化'}
        return {'outcome': 'needs_attention', 'message': '配置尚未恢复或出现外部变化，已保留现值，需人工核对'}

    @staticmethod
    def _regressed(before, after):
        for key, old in before.items():
            current = after.get(key, {})
            if old.get('applicable') is False:
                continue
            if (old.get('definition_hash') != current.get('definition_hash')
                    or old.get('applicable') is True and current.get('applicable') is not True
                    or old.get('path_verified') is True and current.get('path_verified') is not True):
                return True
            if (old.get('requirement') == 'service' and old.get('service') == 'responding'
                    and current.get('service') != 'responding'):
                return True
            if old.get('transport') == 'ok' and current.get('transport') != 'ok':
                return True
            if old.get('service') in ('responding', 'auth_required') and current.get('service') not in ('responding', 'auth_required'):
                return True
        return False

    def fix_all(self, issues=None, progress=None, approved_actions=None, authorize=None, proposal_hash=None):
        changes = []
        record, path = None, None
        def finish(lines, outcome='blocked'):
            if self.mutation_writer:
                try:
                    self.mutation_writer.end(outcome)
                except Exception:
                    outcome = 'rollback_failed'
                    lines = [*lines, '系统执行服务尚未确认收尾；保留恢复记录，新的系统修改将暂停。']
                    if record is not None and path is not None:
                        record['phase'] = 'needs_verification'
                        record['helper_settlement'] = 'unconfirmed'
                        try:
                            self._persist(record, path)
                        except Exception:
                            pass
            _notify(progress, phase='finished', outcome=outcome, message='\n'.join(lines), changes=copy.deepcopy(changes))
            return lines

        engine = self.detection_engine
        if not engine:
            return finish(['未执行修复：缺少检测引擎，无法验证结果'])
        with engine.lock:
            try:
                _notify(progress, phase='preflight', message='再次检查网络，确认这些问题仍需要处理')
                approved = (copy.deepcopy(approved_actions) if approved_actions is not None
                            else self._planned_actions(issues))
                if not approved:
                    return finish(['没有可安全自动修复的问题'])
                fresh = engine.run_all()
                if fresh['check_errors']:
                    return finish(['未执行修复：检查未完成，请先解决诊断错误'])
                requested = {a['issue_type'] for a in approved}
                remaining = [i for i in fresh['issues'] if i[1] in requested]
                actions = self._planned_actions(remaining, fresh['status'])
                if not actions:
                    return finish(['未执行修复：重新检测后已无获准的可安全修复问题'])
                if actions != approved:
                    return finish(['未执行修复：网络状态或修复目标已变化，请重新检查并确认'])
                if authorize is not None:
                    authorize()
                baseline = self._probe_baseline(fresh)
                if not baseline or any(p.get('transport') in ('unknown', 'check_failed') for p in baseline.values()):
                    return finish(['未执行修复：缺少完整的连通性基线，无法验证修复是否导致退化'])
                if any(p.get('path') == 'direct_pac_unresolved' for p in baseline.values()):
                    return finish(['未执行修复：尚不能验证 PAC/WPAD 的实际网络路径，请先使用手工诊断'])
                if (all(p.get('applicable') is False for p in baseline.values())
                        or any(p.get('applicable') is True and p.get('path_verified') is None for p in baseline.values())):
                    return finish(['未执行修复：保护目标的适用环境或访问路径尚未确认'])
                _notify(progress, phase='snapshot', message='备份原来的设置，方便未成功时恢复')
                saved = {a['field']: self._read_field(a['field'], a['service']) for a in actions}
                changes = [{'field': a['field'], 'service': a['service'], 'before': saved[a['field']],
                            'requested': a['desired'], 'after': None, 'after_known': False,
                            'readback_phase': 'not_read', 'observed_at': None} for a in actions]
                record = {'created_at': datetime.now().isoformat(), 'phase': 'prepared',
                          'actions': actions, 'before': saved, 'baseline': baseline,
                          'changes': changes,
                          'issue_types': [i[1] for i in fresh['issues']]}
                if self.mutation_writer:
                    if authorize is None or not isinstance(proposal_hash, str) or not re.fullmatch('[0-9a-f]{64}', proposal_hash):
                        raise PermissionError('系统写入须绑定已确认的 Agent 方案')
                    record['system_mutations'] = {'proposal_hash': proposal_hash, 'batch_id': uuid.uuid4().hex, 'receipts': [],
                        'operations': {a['field']: {'apply_id': uuid.uuid4().hex, 'restore_id': uuid.uuid4().hex} for a in actions}}
                path = self._persist(record)
                _notify(progress, phase='snapshot', recovery_path=str(path),
                        message='原配置已保存')
                if self.mutation_writer:
                    authorize()
                    self.mutation_writer.begin(actions, saved, record['system_mutations'])
            except Exception as exc:
                return finish([f'未执行修复：准备失败（{exc}）'])

            attempted = []
            try:
                for action in actions:
                    _notify(progress, phase='applying', message=action['description'])
                    self._assert_action_context(action, before_write=True)
                    if self._read_field(action['field'], action['service']) != saved[action['field']]:
                        raise RuntimeError('网络配置在准备后被其他程序更改，已停止修复')
                    if authorize is not None:
                        authorize()
                    attempted.append(action)
                    record['phase'] = 'applying'
                    record['attempted'] = copy.deepcopy(attempted)
                    self._persist(record, path)
                    if authorize is not None:
                        authorize()
                    self._write_action(action, saved[action['field']], action['desired'], record, path)
                    actual = self._read_field(action['field'], action['service'])
                    if actual != action['desired']:
                        raise RuntimeError('修改后读取的配置与目标不一致')
                    self._assert_action_context(action)
                _notify(progress, phase='verifying', message='检查问题是否解决，以及原来能用的网络是否仍然正常')
                after = engine.run_all()
                if after['check_errors']:
                    raise RuntimeError('修复后的诊断未完成')
                if requested & {i[1] for i in after['issues']}:
                    raise RuntimeError('修复后的目标问题仍存在')
                probes = self._probe_baseline(after)
                if not probes or self._regressed(baseline, probes):
                    raise RuntimeError('修复后原有可用网络路径退化')
                if not any(p.get('transport') == 'ok' for p in probes.values()):
                    raise RuntimeError('修复后仍无可验证的网络路径')
                old_types = {i[1] for i in fresh['issues']}
                if any(i[0] in ('high', 'medium') and i[1] not in old_types for i in after['issues']):
                    raise RuntimeError('修复后出现新的网络问题')
                for action, change in zip(actions, changes):
                    self._assert_action_context(action)
                    actual = self._read_field(action['field'], action['service'])
                    change.update(after=actual, after_known=True, readback_phase='verification',
                                  observed_at=datetime.now().isoformat())
                    if actual != action['desired']:
                        raise RuntimeError('验证期间网络配置再次变化，不能确认修复成功')
                record['phase'] = 'verified'
                self._persist(record, path)
                return finish([f"✅ 已验证：{action['description']}" for action in actions], 'verified')
            except Exception as exc:
                if not attempted:
                    record['phase'] = 'cancelled'
                    record['failure'] = str(exc)
                    try:
                        self._persist(record, path)
                    except Exception:
                        pass
                    engine.run_all()
                    return finish([f'未执行修复：{exc}'])
                _notify(progress, phase='rollback', message=f'验证未通过：{exc}。正在恢复本次修改')
                failures = []
                for action in reversed(attempted):
                    change = next(c for c in changes if c['field'] == action['field'] and c['service'] == action['service'])
                    try:
                        actual = self._rollback_field(action, saved[action['field']], record, path)
                        change.update(after=actual, after_known=actual is not None, readback_phase='rollback',
                                      observed_at=datetime.now().isoformat())
                    except Exception as rollback_exc:
                        change.update(after=None, after_known=False, readback_phase='rollback_failed', observed_at=None)
                        failures.append(f"{action['field']}: {rollback_exc}")
                restored = engine.run_all()
                record['phase'] = 'rollback_failed' if failures else 'rolled_back'
                record['failure'] = str(exc)
                record['rollback_errors'] = failures
                try:
                    self._persist(record, path)
                except Exception:
                    pass  # The original prepared snapshot remains recoverable.
                lines = [f'❌ 修复失败：{exc}']
                if failures:
                    lines.append('❌ 回滚失败：' + '；'.join(failures))
                    lines.append(f'保留的恢复快照：{path}')
                else:
                    lines.append('↩ 已验证回滚：本次涉及的配置已恢复')
                    if restored['check_errors'] or self._regressed(baseline, self._probe_baseline(restored)):
                        lines.append('⚠️ 配置已恢复，但网络连通性仍需重新确认')
                return finish(lines, 'rollback_failed' if failures else 'rolled_back')
