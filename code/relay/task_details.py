"""Explicit local task-detail projection. Never includes raw command output or tool arguments."""
import copy

from .agent_records import OUTCOMES, STAGES, records_report, records_summary
from .reasoning import MODEL_REASONS
from .trust import REASONS as TRUST_REASONS

TOOLS = {'record_hypothesis': '记录或修订假设', 'refresh_environment': '重新核对全部目标',
         'probe_targets': '补查指定目标', 'propose_repair': '准备成熟修复方案',
         'propose_command': '准备动态命令方案', 'finish': '结束调查'}
REASONS = {'consent_required': '尚未获得上传同意', 'credential_missing': '缺少本次模型凭据',
           'connection_unavailable': '模型连接不可用', 'authentication': '模型服务拒绝凭据',
           'timeout': '调查等待超时', 'budget_exhausted': '本次调查预算已用尽',
           'hourly_budget': '本小时调查预算已用尽', 'task_deadline': '任务已超过总时限',
           'model_path_unverified': '模型访问路径尚未验证', 'journal_unavailable': '本地记录不可用',
           'profile_changed': '网络档案已变化', 'user_cancelled': '用户已停止任务',
           'process_restarted': '核心重启，调查未自动恢复', 'manual_review': '已转人工核对',
           'needs_user': '需要用户参与', 'needs_admin': '需要管理员参与', 'needs_review': '执行结果仍需核对',
           'awaiting_authorization': '等待具体方案授权', 'verified_healthy': '已实测符合预期',
           'verification_failed': '实测仍未符合预期'}


def task_detail(run, redact):
    record = records_report([run], redact, controls=True)['records'][0]
    result = {'record': record, 'model': {}, 'hypotheses': [], 'evidence': [], 'steps': [],
              'conclusion': {}, 'executions': [], 'redacted': True,
              'permission': {}}
    if redact is None:
        result['message'] = '脱敏组件不可用，仅显示枚举状态；未包含原始调查内容。'
        return result
    permission = run.get('authorization_status', {})
    result['permission'] = {'state': permission.get('state') if permission.get('state') in ('trusted', 'requires_review') else None,
                            'reason': permission.get('reason') if permission.get('reason') in TRUST_REASONS else None}
    model = run.get('model_state', {})
    reason = model.get('reason', '')
    result['model'] = {'state': model.get('state', 'not_configured'),
                       'reason': reason if reason in MODEL_REASONS | set(REASONS) else 'unknown',
                       'calls': model.get('calls', 0), 'tools': model.get('tools', 0)}
    result['hypotheses'] = [{key: copy.deepcopy(row.get(key)) for key in
        ('id', 'summary', 'state', 'evidence_ids')} | {'verified_root_cause': False} for row in run.get('hypotheses', [])]
    for row in run.get('evidence', []):
        summary = row.get('summary', {})
        result['evidence'].append({'id': row['id'], 'observed_at': row.get('observed_at'),
            'health': summary.get('health'), 'partial': summary.get('partial_target_check') is True,
            'targets': [{key: target.get(key) for key in ('target', 'state', 'expected_path', 'transport', 'http_status', 'path_verified')}
                        for target in summary.get('targets', [])]})
    result['steps'] = [{key: row.get(key) for key in ('id', 'tool', 'state', 'started_at', 'finished_at')}
                       for row in run.get('tool_steps', [])]
    result['conclusion'] = {key: run.get('conclusion', {}).get(key) for key in ('summary', 'disposition', 'evidence_ids')}
    receipts = list(run.get('execution_history', [])) + ([run['receipt']] if run.get('receipt') else [])
    for index, receipt in enumerate(receipts, 1):
        changes = [{key: row.get(key) for key in ('field', 'service', 'before', 'requested', 'after',
                   'after_known', 'readback_phase', 'observed_at')} | {
                   'comparison': ('unknown' if not row.get('after_known') else 'original' if row['after'] == row['before']
                                  else 'requested' if row['after'] == row['requested'] else 'other')}
                   for row in receipt.get('changes', [])]
        result['executions'].append({'index': index, 'outcome': receipt.get('outcome'),
            'dynamic': receipt.get('kind') == 'dynamic_command',
            'basis': {'mode': receipt.get('authorization', {}).get('mode')
                      if receipt.get('authorization', {}).get('mode') in ('single_run', 'task', 'continuous') else None},
            'changes': changes})
    return redact(result)


def detail_text(detail, section='all'):
    record = detail['record']
    lines = [record.get('id', '')]
    if section in ('all', 'timeline'):
        lines += [records_summary({'records': [record]})]
        if detail.get('permission', {}).get('reason'):
            lines += ['授权状态：' + TRUST_REASONS.get(detail['permission']['reason'], '需要重新审阅')]
    else:
        lines += [OUTCOMES.get(record.get('outcome'), STAGES.get(record.get('stage'), '状态未确认'))]
    if section in ('all', 'investigation'):
        lines += ['', '调查依据', *_investigation_lines(detail)]
    if section in ('all', 'changes'):
        lines += ['', '修改与回读（脱敏值）', *_execution_lines(detail)]
    return '\n'.join(lines)


def _investigation_lines(detail):
    lines = []
    model = detail.get('model', {})
    reason = model.get('reason')
    if reason:
        lines.append(REASONS.get(reason, '调查停止原因：' + reason))
    if detail.get('message'):
        lines.append(detail['message'])
    if not detail.get('hypotheses'):
        lines.append('没有已记录的调查假设；不据此推断根因。')
    states = {'possible': '待验证', 'supported': '有支持证据，非已确认根因', 'rejected': '已否定'}
    for row in detail.get('hypotheses', []):
        lines += [f"{row['id']} · 模型假设 · {states.get(row['state'], '未确认')}", row['summary'] or '',
                  '证据：' + '、'.join(row.get('evidence_ids') or [])]
    for row in detail.get('evidence', []):
        lines += ['', f"{row['id']} · {row['observed_at']} · " + ('部分目标复查' if row['partial'] else '目标检查记录')]
        lines += [f"{t['target']} · {t['state']} · {t['expected_path']} · {t['transport']} · HTTP {t['http_status'] or '-'}"
                  for t in row['targets']]
    if detail.get('steps'):
        lines += ['', '实际调查步骤']
        lines += [f"{row['id']} · {TOOLS.get(row['tool'], '未知工具')} · " +
                  {'started': '开始', 'completed': '完成', 'interrupted': '中断'}.get(row['state'], '未确认') for row in detail['steps']]
    if detail.get('conclusion', {}).get('summary'):
        lines += ['', '模型结论（不替代实测或执行收据）', detail['conclusion']['summary']]
    return lines


def _execution_lines(detail):
    lines = []
    if not detail.get('executions'):
        lines.append('本任务没有执行收据。')
    for execution in detail.get('executions', []):
        lines += ['', f"第 {execution['index']} 次执行 · " + OUTCOMES.get(execution['outcome'], STAGES.get(execution['outcome'], '结果待确认'))]
        if execution.get('basis', {}).get('mode'):
            lines.append('授权依据：' + {'single_run': '逐次确认', 'task': '本次任务信任', 'continuous': '限定范围持续信任'}.get(
                execution['basis'].get('mode'), '未记录'))
        if execution['dynamic']:
            lines.append('动态命令的任意副作用未自动验证，具体内容见私有收据。')
        elif not execution['changes']:
            lines.append('此收据没有结构化前后回读，不能将计划值当成实际修改结果。')
        for row in execution['changes']:
            lines += [f"{row['service']} · {row['field']}", f"修改前：{row['before']}", f"计划值：{row['requested']}",
                      f"实际回读：{row['after']}" if row['after_known'] else '实际回读：未确认',
                      {'original': '回读与修改前一致', 'requested': '回读与计划值一致，和修改前不同',
                       'other': '回读既不是原值，也不是计划值', 'unknown': '没有可确认的最终回读'}.get(row['comparison'], '差异未确认'),
                      '回读阶段：' + str(row['readback_phase']) + ' · ' + str(row['observed_at'] or '无时间记录')]
    return lines
