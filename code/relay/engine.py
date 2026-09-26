"""Local diagnostics and verified, reversible built-in repair operations.

This module intentionally has no account, license or network-login dependency.
"""
import copy
import json
import os
import stat
import tempfile
from pathlib import Path
import threading
from datetime import datetime
import uuid

from .checks import create_instances, get_load_errors
from .commands import CommandResult, checked, run_command
from .network import dns_list, ip_addresses, key_values


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

    def reload_checks(self):
        with self.lock:
            self._load_checks()

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

    def run_all(self):
        with self.lock:
            self.checking = True
            try:
                status, issues, errors = {}, [], dict(self._load_errors)
                for check in self.checks:
                    candidate = copy.deepcopy(status)
                    try:
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
                for name, error in errors.items():
                    issues.append(("medium", f"check_failed_{name}", f"{name} 检查未完成：{error}"))
                if not self.checks and not errors:
                    errors["plugins"] = "没有启用的检测插件"
                    issues.append(("medium", "check_failed_plugins", "没有启用的检测插件"))
                self._snapshot = {"status": status, "issues": issues,
                                  "last_check": datetime.now(), "check_errors": errors}
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
        lines = ["Relay 网络诊断报告", f"检测时间: {date}", ""]
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
    """Only typed, built-in operations can mutate network configuration.

    Config's historical shell templates are never read or executed. All actions
    run under the diagnostic engine's lock, with a fresh preflight and snapshot.
    """
    DNS_TO_COMPANY_ISSUES = {'dns_mixed_on_vpn', 'dns_no_company_on_vpn'}
    DNS_TO_PUBLIC_ISSUES = {'dns_company_leftover'}
    PROXY_GETTERS = {'http': '-getwebproxy', 'https': '-getsecurewebproxy', 'socks': '-getsocksfirewallproxy'}
    PROXY_SETTERS = {'http': '-setwebproxystate', 'https': '-setsecurewebproxystate', 'socks': '-setsocksfirewallproxystate'}

    def __init__(self, config=None, detection_engine=None, runner=None, snapshot_dir=None):
        self.config = config
        self.detection_engine = detection_engine
        self.runner = runner or (detection_engine.runner if detection_engine else run_command)
        self.snapshot_dir = Path(snapshot_dir) if snapshot_dir else Path.home() / 'Library/Application Support/Relay/repair-snapshots'

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
                    fields = [('dns', servers, '切换到已确认 VPN 档案的 DNS')]
            elif kind in self.DNS_TO_PUBLIC_ISSUES:
                if status.get('vpn') != 'off' or status.get('vpn_path') != 'off':
                    continue
                servers = ip_addresses(self._get('dns.public_dns', []))
                fields = [('dns', servers, '恢复用户配置的公共 DNS' if servers else '恢复自动获取 DNS（DHCP）')]
            elif kind == 'ipv6_enabled' and self._get('ipv6.should_be', 'observe') == 'off':
                fields = [('ipv6', 'Off', '按用户明确策略关闭 IPv6')]
            elif kind == 'proxy_leftover':
                for proxy_kind in status.get('proxy_failed_types', []):
                    endpoint = status.get('proxy_details', {}).get(proxy_kind, {})
                    if proxy_kind in self.PROXY_SETTERS and endpoint.get('local') and endpoint.get('state') == 'unreachable':
                        fields.append(('proxy:' + proxy_kind, False, f'关闭未监听的本机 {proxy_kind.upper()} 代理'))
            for field, desired, description in fields:
                if not any(action['field'] == field for action in actions):
                    action = {'issue_type': kind, 'field': field, 'desired': desired,
                              'description': description, 'service': self._service()}
                    if field == 'dns':
                        action['vpn_context'] = self._vpn_context(status)
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
        return [a['description'] for a in actions] or ['没有可安全自动修复的问题']

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
        if action['field'] == 'dns':
            # Read fresh routing/ownership evidence immediately around each write,
            # rather than relying on the older whole-run snapshot or issue absence.
            check = next((c for c in self.detection_engine.checks if c.name == 'vpn'), None)
            if check is None:
                raise RuntimeError('缺少 VPN 检测，无法验证 DNS 修复前提')
            current = {}
            check.check(current)
            if self._vpn_context(current) != action['vpn_context']:
                raise RuntimeError('VPN 归属、路径或接口证据已变化，DNS 修复前提不再成立')
        elif action['field'].startswith('proxy:'):
            current = self._read_proxy(action['field'], action['service'])
            expected = action['proxy_endpoint']
            if any(current[key] != expected[key] for key in ('host', 'port')):
                raise RuntimeError('此网络服务的代理端点与获准修复的端点不一致')
            if before_write:
                probe = self.runner(['/usr/bin/nc', '-G', '2', '-z', expected['host'], str(expected['port'])], timeout=3)
                if not isinstance(probe, CommandResult) or probe.returncode != 1 or probe.timed_out:
                    raise RuntimeError('代理端点已恢复或检查未完成，停止修改代理开关')

    def _rollback_field(self, action, saved):
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
            return
        if actual != action['desired']:
            raise RuntimeError('配置出现外部变更或无法确认的部分写入，未覆盖现值；请按快照人工核对')
        self._write_field(field, saved, service)
        if self._read_field(field, service) != saved:
            raise RuntimeError('回滚后的配置与快照不一致')

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

    @staticmethod
    def _regressed(before, after):
        for key, old in before.items():
            current = after.get(key, {})
            if old.get('transport') == 'ok' and current.get('transport') != 'ok':
                return True
            if old.get('service') in ('responding', 'auth_required') and current.get('service') not in ('responding', 'auth_required'):
                return True
        return False

    def fix_all(self, issues=None):
        engine = self.detection_engine
        if not engine:
            return ['未执行修复：缺少检测引擎，无法验证结果']
        with engine.lock:
            try:
                approved = self._planned_actions(issues)
                if not approved:
                    return ['没有可安全自动修复的问题']
                fresh = engine.run_all()
                if fresh['check_errors']:
                    return ['未执行修复：检查未完成，请先解决诊断错误']
                requested = {a['issue_type'] for a in approved}
                remaining = [i for i in fresh['issues'] if i[1] in requested]
                actions = self._planned_actions(remaining, fresh['status'])
                if not actions:
                    return ['未执行修复：重新检测后已无获准的可安全修复问题']
                if actions != approved:
                    return ['未执行修复：网络状态或修复目标已变化，请重新检查并确认']
                baseline = self._probe_baseline(fresh)
                if not baseline or any(p.get('transport') in ('unknown', 'check_failed') for p in baseline.values()):
                    return ['未执行修复：缺少完整的连通性基线，无法验证修复是否导致退化']
                if any(p.get('path') == 'direct_pac_unresolved' for p in baseline.values()):
                    return ['未执行修复：尚不能验证 PAC/WPAD 的实际网络路径，请先使用手工诊断']
                saved = {a['field']: self._read_field(a['field'], a['service']) for a in actions}
                record = {'created_at': datetime.now().isoformat(), 'phase': 'prepared',
                          'actions': actions, 'before': saved, 'baseline': baseline}
                path = self._persist(record)
            except Exception as exc:
                return [f'未执行修复：准备失败（{exc}）']

            attempted = []
            try:
                for action in actions:
                    self._assert_action_context(action, before_write=True)
                    if self._read_field(action['field'], action['service']) != saved[action['field']]:
                        raise RuntimeError('网络配置在准备后被其他程序更改，已停止修复')
                    attempted.append(action)
                    self._write_field(action['field'], action['desired'], action['service'])
                    actual = self._read_field(action['field'], action['service'])
                    if actual != action['desired']:
                        raise RuntimeError('修改后读取的配置与目标不一致')
                    self._assert_action_context(action)
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
                for action in actions:
                    self._assert_action_context(action)
                    if self._read_field(action['field'], action['service']) != action['desired']:
                        raise RuntimeError('验证期间网络配置再次变化，不能确认修复成功')
                record['phase'] = 'verified'
                self._persist(record, path)
                return [f"✅ 已验证：{action['description']}" for action in actions]
            except Exception as exc:
                if not attempted:
                    record['phase'] = 'cancelled'
                    record['failure'] = str(exc)
                    try:
                        self._persist(record, path)
                    except Exception:
                        pass
                    engine.run_all()
                    return [f'未执行修复：{exc}']
                failures = []
                for action in reversed(attempted):
                    try:
                        self._rollback_field(action, saved[action['field']])
                    except Exception as rollback_exc:
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
                return lines
