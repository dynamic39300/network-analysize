"""Redacted task reports, independent of accounts and native UI."""
STAGES = {'observed': '已检测', 'awaiting_authorization': '等待授权', 'authorized': '已授权',
          'executing': '正在处理', 'needs_reconciliation': '需要核对', 'finished': '已结束',
          'cancelled': '已取消', 'blocked': '未执行', 'investigating': '正在调查',
          'needs_participation': '需要参与', 'model_limited': '模型能力受限'}
OUTCOMES = {'verified': '已验证恢复', 'restored': '配置已恢复', 'rolled_back': '已回退',
            'rollback_failed': '回退失败', 'interrupted': '执行中断', 'cancelled': '已取消',
            'blocked': '未执行', 'not_started': '未开始', 'needs_review': '需要人工核对',
            'reviewed': '已人工核对（非自动验证）'}


def record_identity(run):
    receipt = run.get('receipt') or {}
    return {'id': run['id'], 'kind': 'dynamic_command' if receipt.get('kind') == 'dynamic_command' else 'network',
            'has_receipt': bool(receipt), 'recovery_required': run['stage'] == 'needs_reconciliation'}


def records_report(records, redact, *, controls=False):
    # Without the optional redactor only enumerated states and counts can leave storage.
    if redact is None:
        return {'records': [{**(record_identity(r) if controls else {}), 'stage': r['stage'] if r['stage'] in STAGES else 'blocked',
                             'outcome': r['outcome'] if r.get('outcome') in OUTCOMES else None,
                             'events': []} for r in records],
                'message': '脱敏组件不可用，仅显示处理状态。'}
    visible = []
    for run in records:
        item = {key: run.get(key) for key in
                ('id', 'incident_id', 'stage', 'outcome', 'stop_reason', 'events', 'reconciliation', 'trigger', 'budget')}
        item.update(record_identity(run))
        item['proposals'] = [{'plans': p.get('plans', []), 'expires_at': p.get('expires_at')}
                             for p in run.get('proposals', [])]
        receipt = run.get('receipt')
        if receipt:
            item['receipt'] = {key: receipt.get(key) for key in
                               ('proposal_hash', 'started_at', 'finished_at', 'outcome')}
            item['receipt']['recovery_saved'] = bool(receipt.get('recovery_path'))
        visible.append(item)
    return {'records': redact(visible)}


def records_summary(payload):
    sections = []
    for index, run in enumerate(payload.get('records', []), 1):
        state = OUTCOMES.get(run.get('outcome'), STAGES.get(run.get('stage'), '状态未确认'))
        events = run.get('events', [])
        stamp = events[0]['time'] if events else ''
        lines = [f'{index:02d}  {state}  {stamp}']
        for proposal in run.get('proposals', []):
            lines.extend(proposal.get('plans', []))
        lines.extend(f"{e['time']}  {e['message']}" for e in events)
        if run.get('reconciliation'):
            lines.append(run['reconciliation']['message'])
        sections.append('\n'.join(lines))
    return '\n\n'.join(sections) or payload.get('message') or '尚无处理记录'
