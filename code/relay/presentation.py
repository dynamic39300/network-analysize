"""Evidence-based panel content, independent of Cocoa and system commands."""

SECTIONS = (
    ('wifi', '网络连接', 'wifi'),
    ('vpn', 'VPN 连接', 'network'),
    ('proxy', '代理软件', 'app.connected.to.app.below.fill'),
    ('dns', '网站地址查找', 'server.rack'),
    ('ipv6', '联网方式', 'point.3.connected.trianglepath.dotted'),
    ('system_proxy', '上网转发设置', 'arrow.triangle.branch'),
    ('reachability', '网页访问', 'globe'),
)

GUIDANCE = {
    'wifi_disconnected': '电脑还没有取得可用的网络地址。请检查 Wi-Fi 是否连上，或网线是否插好；也可能需要重连路由器。',
    'vpn_owner_unconfirmed': '检测到了 VPN 通道，但还不能确认由哪款软件提供。请先在 VPN 软件里确认连接情况，以免调整设置后影响上网。',
    'vpn_unconfirmed': '还不能确认 VPN 是否连接成功。请在 VPN 软件中查看连接状态；仅仅打开软件不代表已经连通。',
    'dns_mixed_on_vpn': '公司 VPN 已连接，但电脑查找网站地址时仍使用其他设置，可能打不开公司内部网站。可切换到已保存的公司设置。',
    'dns_no_company_on_vpn': '公司 VPN 已连接，但缺少查找公司网站地址所需的设置，可能打不开内部网站。可恢复已保存的公司设置。',
    'dns_company_leftover': 'VPN 已断开，电脑却仍向公司网络查询网站地址，可能导致网页打不开。可恢复已保存的日常上网设置，或让网络自动分配。',
    'dns_unavailable': '电脑目前找不到负责查询网站地址的服务。请先重新连接网络；如果仍然异常，需要检查路由器或 VPN 的设置。',
    'proxy_leftover': '电脑仍把上网请求交给一个没有响应的本机代理。可关闭失效的转发设置，也可以先重新打开代理软件。',
    'proxy_endpoint_unreachable': '连接不上远程代理服务器，可能是服务器或网络出了问题。请核对代理服务是否可用，当前不宜直接关闭它。',
    'proxy_check_incomplete': '还没有取得代理的检查结果，请稍后重新检测。',
    'ipv6_enabled': '另一种联网方式（IPv6）已开启，但你保存的设置要求关闭。开启本身不是故障，优化只会按你保存的设置调整。',
    'ipv6_disabled': '另一种联网方式（IPv6）已关闭，但你保存的设置要求开启。请核对网络设置。',
}

SHORT_ISSUES = {
    'wifi_disconnected': '电脑还没有连接到可用网络',
    'vpn_owner_unconfirmed': '检测到 VPN，但还不能确认连接来源',
    'vpn_unconfirmed': '还不能确认 VPN 是否连通',
    'dns_mixed_on_vpn': '公司 VPN 与网站地址设置不匹配',
    'dns_no_company_on_vpn': '缺少访问公司网站所需的设置',
    'dns_company_leftover': 'VPN 已断开，还在使用公司上网设置',
    'dns_unavailable': '找不到查询网站地址的服务',
    'proxy_leftover': '上网请求被交给了没有响应的代理',
    'proxy_endpoint_unreachable': '连接不上远程代理服务器',
    'proxy_check_incomplete': '代理连接还没有检查完',
    'ipv6_enabled': '联网方式与你保存的设置不一致',
    'ipv6_disabled': '联网方式与你保存的设置不一致',
}


def optimization_issues(state):
    """Use only currently visible, eligible issues; never invent a repair."""
    if not state.get('ready') or state.get('busy') or state.get('snapshot', {}).get('check_errors'):
        return []
    eligible = state.get('repair_options', {})
    return list(dict.fromkeys(issue[1] for issue in state.get('snapshot', {}).get('issues', [])
                              if issue[1] in eligible))


def scan_caption(state):
    scan = state.get('scan_progress') or {}
    if scan.get('phase') == 'running':
        for index, (key, title, _) in enumerate(SECTIONS, 1):
            if key == scan.get('check'):
                return f'正在检测 {index:02d} · {title}'
    return '01 → 07 · 按顺序检测'


def _short_result(section, row, relevant, status):
    if row['state'] in ('待检测', '检测中', '未检测', '未确认'):
        return {'待检测': '尚未检测', '检测中': '正在检查，请稍候',
                '未检测': '当前未启用这项检查', '未确认': '检查还没有明确结果，需要再确认'}[row['state']]
    if relevant:
        if section == 'reachability':
            observations = status.get('reachability_results', {}).values()
            if any(item.get('transport') != 'ok' for item in observations):
                return '部分网站连接失败或响应太慢'
            return '已连上网站，但网站没有正常提供服务'
        first = SHORT_ISSUES.get(relevant[0][1], '这项检查发现问题，请展开查看')
        return first + (f'（共 {len(relevant)} 项）' if len(relevant) > 1 else '')
    return {
        'wifi': '电脑已连接网络' if status.get('wifi') != 'off' else '正在使用其他网络连接',
        'vpn': 'VPN 连接已确认' if status.get('vpn') == 'ok' else '未使用 VPN，普通上网无需开启',
        'proxy': '已找到正在运行的代理软件' if status.get('proxy_app') == 'running' else '未发现常见代理软件，这本身不是问题',
        'system_proxy': ('使用自动转发，效果还需确认' if status.get('proxy_pac') else
                         '转发服务可以连接' if status.get('proxy') == 'on' else '没有设置上网转发，直接连接网络'),
        'dns': '网站地址查找设置未发现冲突',
        'ipv6': '当前联网方式与你保存的设置一致',
        'reachability': ('直接访问正常，自动转发尚未验证'
                         if any(item.get('path') == 'direct_pac_unresolved' for item in status.get('reachability_results', {}).values())
                         else '网站可以连通，部分服务需要登录'
                         if any(item.get('service') == 'auth_required' for item in status.get('reachability_results', {}).values())
                         else '本次访问测试正常'),
    }[section]

TRANSPORT = {
    'dns_error': '域名解析失败，尚未连接目标服务',
    'connect_error': '连接目标端口失败，可能与路径或目标服务有关',
    'timeout': '请求超时，尚不能确定故障发生在路径还是目标服务',
    'tls_error': 'TLS 握手或证书验证失败，请核对系统时间及证书',
    'check_failed': '探测未完成，不能据此判断网络是否正常',
    'unknown': '没有获得明确的连接结果',
    'not_applicable': '当前环境不适用，未发起访问',
    'not_tested': '预期路径尚未确认，未发起访问',
}
SERVICE = {
    'responding': '服务响应正常', 'auth_required': '网络可达，服务需要登录认证',
    'redirected': '网络可达，服务返回重定向',
    'access_denied': '网络可达，服务拒绝访问', 'rate_limited': '网络可达，服务限制请求频率',
    'server_error': '网络可达，目标服务器报错', 'request_rejected': '网络可达，服务拒绝该请求',
}


def issue_section(kind):
    if kind.startswith('check_failed_'):
        return kind.removeprefix('check_failed_')
    if kind.startswith(('reachability_', 'service_')):
        return 'reachability'
    if kind.startswith('proxy_'):
        return 'system_proxy'
    return kind.split('_', 1)[0]


def handling_plan(section, state):
    """Offer useful next actions without pretending an unverified repair is safe."""
    snapshot = state.get('snapshot', {})
    if snapshot.get('check_errors'):
        return {'steps': '部分检查没有完成，暂时不能安全修改设置。请先重新检测；若仍失败，可复制诊断报告联系支持。', 'settings': False}
    if section == 'reachability':
        results = snapshot.get('status', {}).get('reachability_results', {}).values()
        if results and all(result.get('transport') == 'ok' for result in results):
            return {'steps': '电脑已经连上网站，问题出在网站响应。请在浏览器确认账号权限；访问过于频繁或网站报错时，稍后重试或联系网站管理员。NetCare 无法替网站解除限制，可以帮你重新检测。', 'settings': False}
    steps = {
        'wifi': '打开网络设置，确认 Wi-Fi 已连接或网线已插好。若已连接但仍无法上网，请重新连接当前网络，再回到这里检测。',
        'vpn': '请先在你使用的 VPN 软件中确认账号和连接状态，必要时重新连接。网络设置也可查看系统 VPN；第三方 VPN 需在对应软件中操作。完成后重新检测。',
        'proxy': '请打开你使用的代理软件，确认服务正在运行，再重新检测。未识别到软件不一定代表网络故障。',
        'dns': '目前还没有可以安全应用的地址设置。请先重新连接网络或确认 VPN 状态，再重新检测；公司网络的地址设置请向管理员确认，不要随意替换。',
        'ipv6': '打开网络设置，在当前连接的详细信息中核对 TCP/IP 设置。请先确认公司或运营商的要求，再调整联网方式并重新检测。',
        'system_proxy': '请先确认代理软件或代理服务器可用。也可在网络设置中进入当前连接的详细信息，核对“代理”设置。公司提供的代理或自动配置地址请先向管理员确认，完成后重新检测。',
        'reachability': '请先确认网络已连接，并检查正在使用的 VPN 或代理。可以打开网络设置查看当前连接，再重新检测网站是否恢复。',
    }
    return {'steps': steps[section], 'settings': section != 'proxy'}


def _observations(section, status):
    if section == 'wifi':
        if status.get('wifi') == 'off':
            return '当前使用其他网络接口：' + str(status.get('default_interface', '未确认'))
        address = status.get('wifi_ip')
        return ('本机地址：' + address) if address else '尚未取得有效的本机地址。'
    if section == 'vpn':
        if status.get('vpn') == 'off':
            return '未发现活动 VPN；直连场景下无需开启。'
        evidence = status.get('vpn_evidence', {})
        routes = evidence.get('routes', {})
        if routes:
            return '路由证据：' + '；'.join(f'{ip} → {iface}' for ip, iface in routes.items())
        return '尚未取得能确认 VPN 路径及归属的路由证据。'
    if section == 'proxy':
        client = status.get('proxy_client')
        return ('已识别：' + client + '。端点可用性见系统代理。') if client else '未识别到已知代理客户端；这本身不代表网络异常。'
    if section == 'system_proxy':
        details = status.get('proxy_details', {})
        lines = [f"{kind.upper()}  {item.get('host', '?')}:{item.get('port', '?')} · " +
                 {'ok': '端口可达', 'unreachable': '端口不可达'}.get(item.get('state'), '待确认')
                 for kind, item in details.items()]
        if status.get('proxy_pac'):
            lines.append('已启用自动代理；当前访问测试不能完整验证 PAC/WPAD 路径。')
        return '\n'.join(lines) if lines else '系统代理已关闭，访问测试将使用直连。'
    if section == 'dns':
        servers = status.get('dns_effective_servers', [])
        mode = '自动获取' if status.get('dns_mode') == 'automatic' else '手工配置'
        return f"{mode} · 系统解析器：{', '.join(servers) or '无'}。解析配置存在不等于每个域名均可解析。"
    if section == 'ipv6':
        mode = status.get('ipv6_mode')
        return '当前模式：' + {'Off': '关闭', 'Automatic': '自动', 'Manual': '手工',
                             'Link-local only': '仅本地链路', 'Link-local': '仅本地链路'}.get(mode, str(mode or '未检测'))
    lines = []
    for name, observation in status.get('reachability_results', {}).items():
        name = observation.get('name', name)
        transport = observation.get('transport', 'unknown')
        result = SERVICE.get(observation.get('service'), '服务状态未确认') if transport == 'ok' else TRANSPORT.get(transport, '检测未完成')
        code = observation.get('http_status')
        path = {'direct': '直连', 'system_proxy': '系统代理', 'vpn': '已核对 VPN 路由',
                'direct_pac_unresolved': '直连，未验证自动代理'}.get(observation.get('path'), '路径未确认')
        if observation.get('path') == 'vpn' and observation.get('path_verified') is not True:
            path = 'VPN 路径未通过核对'
        lines.append(f"{name} · {result}" + (f'（HTTP {code}）' if code else '') + f' · {path}')
    return '\n'.join(lines) or '未配置访问测试目标。'


def diagnostic_rows(state):
    live = state.get('live_snapshot')
    snapshot = live if live is not None else state.get('snapshot', {})
    status, errors = snapshot.get('status', {}), snapshot.get('check_errors', {})
    issues = snapshot.get('issues', [])
    scan = state.get('scan_progress') or {}
    enabled = state.get('enabled_checks')
    options = {} if errors else state.get('repair_options', {})
    rows = []
    for number, (section, title, symbol) in enumerate(SECTIONS, 1):
        relevant = [i for i in issues if issue_section(i[1]) == section]
        value = status.get('proxy_app' if section == 'proxy' else section)
        row = dict(id=section, number=number, title=title, symbol=symbol, tone='neutral', state='待检测',
                   explanation='等待检测结果。', evidence='', issue_types=[], action='')
        if scan.get('check') == section and scan.get('phase') == 'running':
            row.update(state='检测中', tone='working', explanation='正在读取状态并核对网络证据。')
        elif section in errors:
            row.update(state='未确认', tone='warning', explanation='检查未完成，暂时无法判断该环节是否正常。',
                       evidence=str(errors[section]), action='重新检测')
        elif value is not None or relevant:
            row['evidence'] = _observations(section, status)
            if relevant:
                row.update(state='需要处理', tone='error' if any(i[0] == 'high' for i in relevant) else 'warning')
                row['explanation'] = '\n'.join(dict.fromkeys(GUIDANCE.get(i[1], i[2]) for i in relevant))
                if section == 'reachability':
                    row['explanation'] = '访问测试发现异常；以下结果区分连接故障与服务端响应。'
                row['issue_types'] = [i[1] for i in relevant if i[1] in options]
                row['action'] = '立即帮我处理'
            elif value == 'unknown':
                row.update(state='未确认', tone='warning', explanation='已有证据不足以确认此环节的状态。', action='重新检测')
            elif value in ('ignored', 'skipped'):
                row.update(state='未检测', explanation='当前设置未要求检测此项。')
            elif section == 'system_proxy' and status.get('proxy_pac'):
                row.update(state='待确认', tone='warning', action='查看建议',
                           explanation='网络使用了自动转发规则，目前还不能完整检查它的实际效果。网页测试的直接连接结果不能代表这条转发路径。')
            else:
                row.update(state='正常', tone='ok', explanation='当前检测未发现异常。')
                if section == 'vpn' and value == 'off':
                    row.update(state='未启用', tone='neutral', explanation='未发现需要处理的 VPN 问题。')
                elif section == 'proxy' and value == 'off':
                    row.update(state='未发现', tone='neutral', explanation='没有识别到常见代理软件，这本身不是网络问题。')
                elif section == 'ipv6':
                    row['explanation'] = '当前模式与已保存策略没有冲突。'
                elif section == 'dns':
                    row['explanation'] = '已取得系统解析器，未发现档案配置冲突。'
        elif enabled is not None and section not in enabled:
            row.update(state='未检测', explanation='此项检测未启用。')
        elif state.get('ready') and live is None and snapshot.get('last_check'):
            row.update(state='未确认', tone='warning', explanation='本次检测没有返回该环节的状态。', action='重新检测')
        row['summary'] = _short_result(section, row, relevant, status)
        if row['action']:
            row['action'] = '立即帮我处理'
        rows.append(row)
    return rows


def panel_summary(state):
    if state.get('connection') == 'disconnected':
        return '与核心的连接已中断', '执行结果待核对，未自动重发请求。'
    if state.get('busy'):
        return ('正在检查当前网络' if state.get('scan_progress') else state['busy']), '正在检查，请稍候。'
    if not state.get('ready'):
        return '正在准备', '正在加载检测设置。'
    if not state.get('snapshot', {}).get('last_check'):
        return '尚未检测', '手动检测 · 网络设置未作修改'
    profile = state.get('agent', {}).get('profile', {})
    if profile.get('health') in ('degraded', 'unknown', 'attention'):
        return '保护目标需要关注', f"{profile.get('covered', 0)}/{profile.get('total', 0)} 个目标符合预期 · 查看网络档案"
    rows = diagnostic_rows(state)
    attention = sum(row['tone'] in ('warning', 'error') for row in rows)
    if attention:
        count = len(optimization_issues(state))
        manual = sum(row['tone'] in ('warning', 'error') and not row['issue_types'] for row in rows)
        detail = f'{count} 个问题可自动优化' + (f'，另有 {manual} 项需要确认' if manual else '')
        return f'{attention} 个环节需要关注', (detail if count else '需要进一步确认，可逐项开始处理')
    if state.get('overall') == 'unknown':
        return '检测尚未完成', '部分检查还没有取得明确结果。'
    return '当前检测未发现异常', '仅代表已启用检测项的本次结果。'
