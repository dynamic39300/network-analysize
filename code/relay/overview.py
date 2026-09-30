"""Goal-first overview content, independent of Cocoa and operating-system tools."""
import copy
from datetime import datetime

from .agent_records import OUTCOMES, STAGES
from .presentation import diagnostic_rows, panel_summary
from .profiles import PATHS, expire_assessment
from .task_details import REASONS
from .trust import REASONS as TRUST_REASONS


def overview(state):
    profile = expire_assessment(copy.deepcopy(state.get('agent', {}).get('profile', {})))
    connected = state.get('ready') and state.get('connection') != 'disconnected'
    snapshot = state.get('snapshot', {})
    status = snapshot.get('status', {})
    targets = []
    for row in profile.get('targets', []):
        current = copy.deepcopy(row)
        if not connected:
            current.update(state='unknown', label='当前未确认', summary='核心连接中断，保留的是历史观测')
        elif not snapshot.get('last_check'):
            current.update(state='unknown', label='尚未检测', summary='尚无当前档案的观测')
        current['path_label'] = PATHS.get(row['expected_path'], '路径未确认')
        diagnosis = current.get('observation', {}).get('diagnosis', {})
        issue = 'dns_reference_' + current['id']
        current['repair_issue'] = (issue if connected and current['state'] == 'degraded'
                                   and diagnosis.get('kind') == 'dns_reference_candidate'
                                   and issue in state.get('repair_options', {}) else '')
        current['diagnosis_lines'] = ([
            '系统解析 → ' + diagnosis['current_address'] + ' → 连接失败',
            '参考解析 ' + diagnosis['resolver'] + ' → ' + diagnosis['verified_address']
            + ' → 网站已响应（HTTP ' + str(diagnosis['http_status']) + '）',
            '这支持“当前解析结果或到该地址的路径异常”；更换 DNS 后仍须复测全部保护目标。',
        ] if current['repair_issue'] else [])
        targets.append(current)
    report = state.get('report') or {}
    stage = state.get('run_stage') or report.get('stage')
    reason = report.get('model', {}).get('reason')
    execution = state.get('active_execution')
    title, _ = panel_summary(state)
    if not connected:
        title = '核心未连接，当前健康未确认'
    elif execution:
        title = {'preflight': '正在复核获准范围', 'snapshot': '正在保存修改前配置', 'applying': '正在处理范围内的问题',
                 'verifying': '正在验证修改结果', 'rollback': '正在恢复本次修改', 'finished': '正在记录执行结果'}.get(
                     execution['phase'], '正在按范围信任处理')
    elif state.get('agent', {}).get('recovery_pending'):
        title = '执行结果需要核对'
    elif stage == 'awaiting_authorization':
        title = '有一份处理方案等待确认'
    elif not snapshot.get('last_check'):
        title = '尚未检测保护目标'
    elif not state.get('busy') and profile.get('health') == 'healthy':
        title = '本次保护目标符合预期'
    elif not state.get('busy') and profile.get('health') == 'unknown':
        title = '保护目标仍有未确认结果'
    elif not state.get('busy') and profile.get('health') in ('degraded', 'attention'):
        title = '部分保护目标需要关注'
    environment = ('VPN 已确认' if status.get('vpn') == 'ok' else 'VPN 未启用' if status.get('vpn') == 'off' else 'VPN 状态未确认')
    if status.get('default_interface'):
        environment = str(status['default_interface']) + ' · ' + environment
    if status.get('wifi_network'):
        environment = str(status['wifi_network']) + ' · ' + environment
    if not connected:
        environment = '历史环境 · ' + environment
    stamp = profile.get('observed_at')
    captured = datetime.fromtimestamp(stamp).strftime('%m-%d %H:%M:%S') if stamp else '尚无当前档案的检测'
    checks = diagnostic_rows(state)
    if not connected:
        for row in checks:
            row.update(tone='neutral', state='历史观测', summary='当前未确认；上次结果：' + row['summary'])
    return {'title': title, 'environment': environment, 'profile': profile.get('name', '未配置网络档案'),
            'coverage': f"{profile.get('covered', 0) if connected else 0}/{profile.get('total', 0)} 个目标符合预期",
            'captured': captured, 'targets': targets,
            'task': ('本次任务信任' if execution['mode'] == 'task' else '限定范围持续信任') if execution else
                    OUTCOMES.get(report.get('outcome'), STAGES.get(stage, '尚无处理任务')),
            'reason': REASONS.get(reason, '调查能力受限，已有本地检查仍保留') if reason and report.get('model', {}).get('state') == 'limited' else '',
            'permission_reason': TRUST_REASONS.get(report.get('authorization', {}).get('reason'), ''),
            'checks': checks}
