"""Describe frozen selected/baseline replay PnL; never select or execute a policy."""
import argparse
from collections import defaultdict
import csv
import datetime as dt
from decimal import Decimal, getcontext
import hashlib
import json
from pathlib import Path

getcontext().prec = 40
D = Decimal
ZERO = D(0)
FEE = D('0.0003')
BPS = D(10000)
VENUES = ('bybit', 'bitget')
BINS = {
    'holding_seconds': [D(x) for x in (1, 5, 15, 30, 60, 120)],
    'entry_cross_spread_bps': [D(x) for x in (0, 15, 21, 30, 50, 100)],
    'entry_within_spread_sum_bps': [D(x) for x in (5, 10, 20, 50, 100)],
    'entry_liquidation_cost_bps': [D(x) for x in (12, 20, 30, 50, 100)],
}


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load(path):
    return json.loads(Path(path).read_text())


def decimal(value):
    result = D(value)
    require(result.is_finite(), 'non-finite decimal')
    return result


def csv_rows(path):
    with Path(path).open(newline='') as stream:
        return list(csv.DictReader(stream))


def bin_name(value, edges):
    for i, upper in enumerate(edges):
        if value < upper:
            return ('<' if not i else '[' + str(edges[i-1]) + ',') + str(upper) + ('' if not i else ')')
    return '>=' + str(edges[-1])


def cycle_record(day, policy_id, cycle, orders):
    """Independent exact arithmetic; bps denominators are explicitly named."""
    require(len(orders) == 4, 'closed pair must have four orders')
    qty = int(cycle['qty'])
    entry_ns, exit_ns = int(cycle['entry_ns']), int(cycle['exit_ns'])
    require(qty > 0 and qty % 10 == 0 and exit_ns >= entry_ns, 'cycle quantity/time')
    require(int(cycle['holding_ns']) == exit_ns-entry_ns, 'holding ns mismatch')
    lng, sht = cycle['long_venue'], cycle['short_venue']
    require({lng, sht} == set(VENUES), 'unknown cycle direction')
    indexed = {(o['venue'], o['intent']): o for o in orders}
    require(len(indexed) == 4 and set(indexed) == {(v, intent) for v in VENUES for intent in ('entry', 'exit')}, 'four unique venue/intent orders')
    entry, close, per_venue_net = {}, {}, {}
    gross, fees, depth_excess = ZERO, ZERO, 0
    depth, below = False, False
    for venue in VENUES:
        opened, closed = indexed[venue, 'entry'], indexed[venue, 'exit']
        require(int(opened['ns']) == entry_ns and int(closed['ns']) == exit_ns, 'synchronous order time')
        require(int(opened['qty']) == qty == int(closed['qty']), 'same paired quantity')
        require(opened['side'] == ('buy' if venue == lng else 'sell') and closed['side'] == ('sell' if venue == lng else 'buy'), 'order side')
        entry[venue], close[venue] = decimal(opened['price']), decimal(closed['price'])
        venue_fees = ZERO
        for order in (opened, closed):
            price = decimal(order['price'])
            paid = decimal(order['fee'])
            require(paid == qty*price*FEE, 'fee != actual notional * 3bp')
            display = int(order['l1_display_qty'])
            expected_excess = max(0, qty-display) if order['intent'] == 'exit' else 0
            expected_below = order['intent'] == 'exit' and qty*price < 5
            require(int(order['excess_depth_qty']) == expected_excess and bool(int(order['exit_below_min'])) == expected_below, 'execution assumption flags')
            if order['intent'] == 'entry':
                require(qty <= display and 5 <= qty*price <= 250, 'entry sizing/minimum')
            depth_excess += expected_excess
            depth |= expected_excess > 0
            below |= expected_below
            venue_fees += paid
        venue_gross = qty*(close[venue]-entry[venue])*(1 if venue == lng else -1)
        per_venue_net[venue] = venue_gross-venue_fees
        require(per_venue_net[venue] == decimal(cycle[venue+'_net_before_funding']), 'per-venue net mismatch')
        gross += venue_gross
        fees += venue_fees
        require(closed['exit_reason'] == cycle['exit_reason'], 'exit reason mismatch')
    net = gross-fees
    require(gross == decimal(cycle['gross_pnl']) and fees == decimal(cycle['total_fees']) and net == decimal(cycle['net_before_funding']), 'cycle gross/fee/net mismatch')
    require(depth == bool(int(cycle['exit_depth_assumed'])) and below == bool(int(cycle['exit_min_notional_assumed'])), 'cycle flags mismatch')
    require(entry[lng] == decimal(cycle['long_entry_price']) and entry[sht] == decimal(cycle['short_entry_price']) and close[lng] == decimal(cycle['long_exit_price']) and close[sht] == decimal(cycle['short_exit_price']), 'cycle four-price mismatch')
    quotes = {v: {side: decimal(cycle['entry_'+v+'_'+side]) for side in ('ask', 'bid')} for v in VENUES}
    require(entry[lng] == quotes[lng]['ask'] and entry[sht] == quotes[sht]['bid'], 'entry L1 mismatch')
    reverse_fees = qty*(quotes[lng]['bid']+quotes[sht]['ask'])*FEE
    entry_fees = qty*(entry[lng]+entry[sht])*FEE
    baseline_gross = qty*(quotes[lng]['bid']-entry[lng]+entry[sht]-quotes[sht]['ask'])
    baseline_net = baseline_gross-entry_fees-reverse_fees
    require(baseline_net == decimal(cycle['initial_quote_net_proxy']), 'entry immediate liquidation baseline')
    notional = qty*entry[lng]
    cross = (entry[sht]-entry[lng])*2*BPS/(entry[sht]+entry[lng])
    within = sum(((q['ask']-q['bid'])*2*BPS/(q['ask']+q['bid']) for q in quotes.values()), ZERO)
    hour = dt.datetime.fromtimestamp(entry_ns//10**9, dt.timezone.utc).hour
    record = {
        'date': day, 'policy_id': policy_id, 'cycle': int(cycle['cycle']), 'qty': qty,
        'entry_ns': entry_ns, 'exit_ns': exit_ns, 'holding_seconds': D(exit_ns-entry_ns)/10**9,
        'direction': 'long_'+lng+'_short_'+sht, 'exit_reason': cycle['exit_reason'],
        'entry_utc_2h': f'{hour//2*2:02}:00–{hour//2*2+2:02}:00',
        'entry_long_notional_usdt': notional, 'gross_usdt': gross, 'fees_usdt': fees, 'net_before_funding_usdt': net,
        'gross_bps_on_long_notional': gross/notional*BPS, 'fees_bps_on_long_notional': fees/notional*BPS,
        'net_bps_on_long_notional': net/notional*BPS, 'bybit_net_usdt': per_venue_net['bybit'], 'bitget_net_usdt': per_venue_net['bitget'],
        'entry_cross_spread_bps': cross, 'entry_within_spread_sum_bps': within,
        'entry_reverse_spread_cost_bps_on_long_notional': -baseline_gross/notional*BPS,
        'entry_liquidation_cost_bps': -baseline_net/notional*BPS,
        'entry_four_fee_cost_bps_on_long_notional': (entry_fees+reverse_fees)/notional*BPS,
        'entry_immediate_net_usdt': baseline_net,
        'gross_positive_net_negative': gross > 0 and net < 0,
        'gross_positive_net_zero': gross > 0 and net == 0,
        'exit_depth_assumed': depth, 'exit_min_notional_assumed': below, 'exit_excess_depth_qty_sum_legs': depth_excess,
        'take_profit_trigger': cycle['exit_reason'] == 'take_profit',
        'initial_hard_budget_breach': bool(int(cycle['initial_hard_budget_breach'])),
    }
    record.update({key+'_bin': bin_name(record[key], bounds) for key, bounds in BINS.items()})
    return record


def totals(records):
    result = {'cycles': len(records), 'net_wins': 0, 'net_losses': 0, 'net_zero': 0,
              'gross_wins': 0, 'gross_positive_net_negative': 0, 'gross_positive_net_zero': 0,
              'take_profit_triggers': 0, 'take_profit_net_wins': 0, 'net_wins_without_take_profit': 0,
              'initial_hard_budget_breaches': 0}
    for key in ('gross_usdt', 'fees_usdt', 'net_before_funding_usdt', 'entry_long_notional_usdt'):
        result[key] = sum((r[key] for r in records), ZERO)
    for row in records:
        net = row['net_before_funding_usdt']
        result['net_wins'] += net > 0
        result['net_losses'] += net < 0
        result['net_zero'] += net == 0
        result['gross_wins'] += row['gross_usdt'] > 0
        for key in ('gross_positive_net_negative', 'gross_positive_net_zero'):
            result[key] += row[key]
        result['take_profit_triggers'] += row['take_profit_trigger']
        result['take_profit_net_wins'] += row['take_profit_trigger'] and net > 0
        result['net_wins_without_take_profit'] += not row['take_profit_trigger'] and net > 0
        result['initial_hard_budget_breaches'] += row['initial_hard_budget_breach']
    result['net_win_rate'] = D(result['net_wins'])/len(records) if records else None
    result['net_bps_on_sum_entry_long_notional'] = result['net_before_funding_usdt']/result['entry_long_notional_usdt']*BPS if records else None
    result['mean_entry_liquidation_cost_bps'] = sum((r['entry_liquidation_cost_bps'] for r in records), ZERO)/len(records) if records else None
    return result


def grouped(records, field):
    groups = defaultdict(list)
    for row in records:
        groups[str(row[field])].append(row)
    return {key: totals(rows) for key, rows in sorted(groups.items())}


def temporal_losses(records):
    """A zero-net cycle ends a losing streak. Dates reset accounts and streaks."""
    result = {}
    for day in sorted({r['date'] for r in records}):
        rows = sorted((r for r in records if r['date'] == day), key=lambda r: (r['exit_ns'], r['cycle']))
        streak, maximum = [], []
        for row in rows:
            if row['net_before_funding_usdt'] < 0:
                streak.append(row)
                if len(streak) > len(maximum):
                    maximum = list(streak)
            else:
                streak = []
        losses = [r for r in rows if r['net_before_funding_usdt'] < 0]
        amount = sum((-r['net_before_funding_usdt'] for r in losses), ZERO)
        windows = {}
        for window in sorted({r['entry_utc_2h'] for r in losses}):
            sample = [r for r in losses if r['entry_utc_2h'] == window]
            lost = sum((-r['net_before_funding_usdt'] for r in sample), ZERO)
            windows[window] = {'loss_cycles': len(sample), 'absolute_loss_usdt': lost,
                               'fraction_of_loss_count': D(len(sample))/len(losses),
                               'fraction_of_absolute_loss': lost/amount}
        dominant = sorted(windows, key=lambda key: (-windows[key]['absolute_loss_usdt'], key))
        result[day] = {
            'max_consecutive_net_losses': len(maximum),
            'max_streak_start_cycle': maximum[0]['cycle'] if maximum else None,
            'max_streak_end_cycle': maximum[-1]['cycle'] if maximum else None,
            'max_streak_entry_ns': maximum[0]['entry_ns'] if maximum else None,
            'max_streak_exit_ns': maximum[-1]['exit_ns'] if maximum else None,
            'max_streak_net_usdt': sum((r['net_before_funding_usdt'] for r in maximum), ZERO),
            'max_streak_mean_entry_liquidation_cost_bps': sum((r['entry_liquidation_cost_bps'] for r in maximum), ZERO)/len(maximum) if maximum else None,
            'absolute_loss_usdt': amount, 'loss_cycles': len(losses),
            'by_entry_UTC_2h': windows,
            'largest_loss_window': {'window': dominant[0], **windows[dominant[0]]} if dominant else None,
            'definition': 'Consecutive strictly negative net closed cycles; zero or positive ends streak; reset each date. Time concentration uses entry UTC, absolute negative-cycle loss without offsetting winners.'}
    return result


def diagnostic_policy(records):
    depth = [r for r in records if r['exit_depth_assumed']]
    covered = [r for r in records if not r['exit_depth_assumed']]
    below = [r for r in records if r['exit_min_notional_assumed']]
    dimensions = ('date', 'exit_reason', 'direction', 'entry_utc_2h', *(key+'_bin' for key in BINS))
    return {
        'totals': totals(records),
        'by_date': grouped(records, 'date'),
        'groups': {field: grouped(records, field) for field in dimensions if field != 'date'},
        'groups_by_date': {day: {field: grouped([r for r in records if r['date'] == day], field) for field in dimensions if field != 'date'} for day in sorted({r['date'] for r in records})},
        'temporal_losses_by_date': temporal_losses(records),
        'worst_five_losses': sorted((r for r in records if r['net_before_funding_usdt'] < 0), key=lambda r: (r['net_before_funding_usdt'], r['date'], r['cycle']))[:5],
        'execution_assumption_effect': {
            'depth_exceeded_cycles': totals(depth), 'fully_visible_exit_depth_cycles': totals(covered),
            'depth_exceeded_cycle_fraction': D(len(depth))/len(records) if records else None,
            'below_minimum_exit_cycles': totals(below),
            'excess_depth_qty_sum_legs': sum(r['exit_excess_depth_qty_sum_legs'] for r in records),
            'interpretation': 'Flagged and unflagged groups have different trades; their PnL difference is descriptive, not the causal price of enforcing depth.'},
    }


def directions(records, checks):
    all_stats = totals(records)
    timeout = totals([r for r in records if r['exit_reason'] == 'holding_timeout'])
    stop = totals([r for r in records if r['exit_reason'] in ('stop_loss', 'hard_stop_loss')])
    high_cost = totals([r for r in records if r['entry_liquidation_cost_bps'] >= 30])
    depth = totals([r for r in records if r['exit_depth_assumed']])
    result = [
        {'direction': '增加入场立即反向清算成本上限', 'evidence': {'cost_ge_30bp_fixed_descriptive_bin': high_cost},
         'evidence_summary': f"入选策略清算成本≥30bp的固定分箱有 {high_cost['cycles']} 笔，合计净额 {high_cost['net_before_funding_usdt']:.4f}U。",
         'hypothesis': '原始跨所价差大仍可能伴随宽所内买卖差，刚开仓的反向清算值已很差。下一轮在训练日预设少量成本上限，并保留原始跨所价差门槛；30bp只是固定描述分箱，不是推荐阈值。',
         'test': '检查成本过滤是否改善不同日的净值及回撤，同时报告减少的机会和名义金额。'},
        {'direction': '恢复可执行退出约束，并检查入场反向盘口覆盖', 'evidence': {'exits_exceeding_visible_L1': depth},
         'evidence_summary': f"入选策略有 {depth['cycles']} 笔退出超出可见一档深度，该组净额 {depth['net_before_funding_usdt']:.4f}U。",
         'hypothesis': '理想平仓超出一档深度的收益可能无法实现。下一轮先降低目标数量至入场正反向可见深度共同支持的数量，再用真实退出深度/延迟/部分成交重跑。',
         'test': '单独报告滞后退出、残量、滑点和减仓后的现金收益；不能把事后退出深度当作开仓先验。'},
        {'direction': '用回归进展检验超时，而非只延长持仓', 'evidence': {'holding_timeout': timeout, 'stop_exits': stop},
         'evidence_summary': f"入选策略到时退出 {timeout['cycles']} 笔、净额 {timeout['net_before_funding_usdt']:.4f}U；止损类退出 {stop['cycles']} 笔、净额 {stop['net_before_funding_usdt']:.4f}U。",
         'hypothesis': '固定到时退出可能包含未回归、已改善但尚未达止盈等不同情况。下一轮记录持有期间的最大有利/不利净值及回归进展，再研究分阶段到期规则；仅凭开平记录不能判定错过多少止盈。',
         'test': '训练日预设检查时点与进展阈值，保持硬止损，并在后续日期冻结验证。'},
        {'direction': '研究连续亏损暂停，并区分费损与价格亏损',
         'evidence': {'gross_positive_net_negative': all_stats['gross_positive_net_negative'], 'gross_positive_net_zero': all_stats['gross_positive_net_zero'], 'gross_wins': all_stats['gross_wins'], 'fees_usdt': all_stats['fees_usdt'], 'temporal_losses': temporal_losses(records)},
         'evidence_summary': f"入选策略毛利正但费后亏 {all_stats['gross_positive_net_negative']} 笔；各日最长连续净亏分别为 " + '、'.join(f"{day}: {value['max_consecutive_net_losses']}笔" for day, value in temporal_losses(records).items()) + '。',
         'hypothesis': '若净亏损成串，可研究预设连续亏损阈值及冷静期；其中毛利为正但费后亏的情况，更可能需要限制低净捕获空间和重复周转。连续亏损并不证明下一笔仍会亏。',
         'test': '训练期预设少量暂停阈值与恢复条件，同时报告被暂停的后续盈利机会；费用不能重复扣在已经净值化的止盈条件上。'},
    ]
    capped = [r for r in checks if r['loss_cap_hit']]
    if capped:
        result[-1]['evidence']['daily_loss_cap_hits'] = capped
        result[-1]['test'] += ' 保留10U保护上限，并区分暂停影响与最后一笔越过上限的金额差异。'
    return result


def fmt(value, places=4):
    return '—' if value is None else f'{D(value):.{places}f}'


def markdown(result):
    lines = ['# 冻结参数回放：盈亏归因', '',
             '本报告仅分析已冻结的入选参数和基准，不重选参数。金额均为 USDT；净额为扣交易手续费、未扣资金费的已闭合组合损益。资金费核验状态逐日列出。每日账户独立，跨日合计是样本加总，不是连续账户收益率。', '',
             '| 日期 | 角色 / 参数 | 组合 | 毛利 | 手续费 | 净额 | 净赢 | 止盈触发 | 毛利正、费后亏 |',
             '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for pid, policy in result['by_policy'].items():
        roles = '/'.join(policy['roles'])
        for day, row in policy['by_date'].items():
            lines.append(f"| {day} | {roles} / {pid} | {row['cycles']} | {fmt(row['gross_usdt'])} | {fmt(row['fees_usdt'])} | {fmt(row['net_before_funding_usdt'])} | {row['net_wins']} | {row['take_profit_triggers']} | {row['gross_positive_net_negative']} |")
    lines += ['', '入场跨所价差使用 `(做空所买一−做多所卖一)/两者均价`；所内 spread 合计分别除以各所中间价后相加。立即反向清算成本使用原始四个一档报价、开平四笔手续费，除以初始做多单腿名义金额。它与原始跨所价差不是同一个指标。所有 bp 分箱事先固定，只作描述。', '', '入选策略同时改变入场、止盈、止损和持仓期限，不能把交易减少或净额变化单独归因于更高的入场门槛；较短持仓也可能增加同日再次交易的次数。', '', '## 退出原因与理想成交影响', '']
    if result['loss_cap_interpretation']['cap_hit_day_policy_count']:
        lines += ['**亏损上限影响**：部分参数在累计亏损10U附近停止新增交易。排名中的微小差异可能只是最后一笔超出上限的金额不同，不能据此称策略显著提升或已找到稳定盈利参数。日内比较覆盖的交易时长也可能不同。', '']
    for pid, policy in result['by_policy'].items():
        row = policy['totals']
        lines += [f"**{pid}**：{row['net_wins']} 笔净赢，其中 {row['take_profit_net_wins']} 笔由止盈触发，{row['net_wins_without_take_profit']} 笔由其他原因退出。止盈触发与最终净赢分别计数。", '', '| 退出原因 | 组合 | 毛利 | 手续费 | 净额 | 净赢 |', '|---|---:|---:|---:|---:|---:|']
        for reason, r in policy['groups']['exit_reason'].items():
            lines.append(f"| {reason} | {r['cycles']} | {fmt(r['gross_usdt'])} | {fmt(r['fees_usdt'])} | {fmt(r['net_before_funding_usdt'])} | {r['net_wins']} |")
        effect = policy['execution_assumption_effect']
        assumed = effect['depth_exceeded_cycles']
        lines += ['', f"超出当时可见一档深度的退出涉及 {assumed['cycles']} 笔组合（{fmt((effect['depth_exceeded_cycle_fraction'] or ZERO)*100, 2)}%），该组净额 {fmt(assumed['net_before_funding_usdt'])}；低于最小名义金额退出 {effect['below_minimum_exit_cycles']['cycles']} 笔。分组净额不是取消理想成交假设的因果收益估计。", '', '| 最差组合：日期 / 编号 | 原因 | 净额 | 持仓秒 | 入场跨差 bp | 清算成本 bp | 超深度 |', '|---|---|---:|---:|---:|---:|---|']
        for r in policy['worst_five_losses']:
            lines.append(f"| {r['date']} / {r['cycle']} | {r['exit_reason']} | {fmt(r['net_before_funding_usdt'])} | {fmt(r['holding_seconds'], 3)} | {fmt(r['entry_cross_spread_bps'], 2)} | {fmt(r['entry_liquidation_cost_bps'], 2)} | {'是' if r['exit_depth_assumed'] else '否'} |")
        lines.append('')
        for day, temporal in policy['temporal_losses_by_date'].items():
            worst = temporal['largest_loss_window']
            text = f"- {day}：最长连续净亏 {temporal['max_consecutive_net_losses']} 笔，该段净额 {fmt(temporal['max_streak_net_usdt'])}。"
            if worst:
                text += f" 按入场UTC时段统计，{worst['window']} 集中了 {fmt(worst['fraction_of_absolute_loss']*100, 2)}% 的亏损单绝对亏损金额（不以盈利单抵消）。"
            lines.append(text)
        lines.append('')
    lines += ['## 下一轮研究方向', '', '以下为待验证假设，不是已证明更好的参数，也不更改交易默认设置。证据来自入选策略全部样本的描述；不得利用10月结果回调本轮参数。', '']
    for i, item in enumerate(result['next_round_directions'], 1):
        # Detailed evidence remains machine-readable; prose includes the relevant aggregates above.
        lines += [f"{i}. **{item['direction']}**：{item['evidence_summary']} {item['hypothesis']} 验证方法：{item['test']}", '']
    lines += ['## 核验与限制', '']
    for row in result['day_policy_checks']:
        lines.append(f"- {row['date']} / {row['policy_id']}：账目复算通过；资金费 {row['funding_status']}；未闭合币数 {row['unresolved_qty']}。")
        if row['loss_cap_hit']:
            lines.append(f"  累计亏损上限触发；最后退出时间戳 {row['last_exit_ns']}，已实现净额比 −10U 再低 {fmt(row['loss_cap_overshoot_usdt'], 6)}U。")
    lines += ['', '方向、时段、持仓与三类入场价差指标的固定分箱总表，以及每个组合的派生指标，见 attribution.json。开平记录不包含盘中完整路径，不能据此声称错过的止盈机会数量或推导因果最优退出。理想成交没有真实延迟、冲击与部分成交，结果不能作为实盘收益证明。', '']
    return '\n'.join(lines)


def build(experiment):
    root = Path(experiment).resolve()
    destinations = [root/'attribution.json', root/'attribution.md']
    require(not any(p.exists() for p in destinations), 'attribution already exists; refusing overwrite')
    # Freeze check precedes any evaluation economic read.
    frozen_path = root/'selection-frozen.json'
    frozen = load(frozen_path)
    protocol = load(root/'protocol.json')
    require(frozen['evaluation_date_results_consumed'] is False, 'selection consumed evaluation results')
    require(sha(root/'protocol.json') == frozen['protocol_sha256'] and sha(root/'selected-policies.csv') == frozen['selected_policies_sha256'], 'frozen protocol/policies changed')
    require(frozen['fit_dates'] == protocol['fit_dates'] and frozen['evaluation_date'] == protocol['evaluation_date'], 'date protocol mismatch')
    pids = sorted({frozen['selected_id'], frozen['baseline_id']})
    require(set(r['id'] for r in csv_rows(root/'selected-policies.csv')) == set(pids), 'selected policies mismatch')
    provenance = {str(p): sha(p) for p in (frozen_path, root/'protocol.json', root/'selected-policies.csv', Path(__file__))}
    phases = [('search', day) for day in protocol['fit_dates']] + [('evaluation', protocol['evaluation_date'])]
    # All runs must be complete before the first evaluation aggregate is opened.
    for phase, day in phases:
        folder = root/phase/day
        status = load(folder.with_suffix('.status.json'))
        require(status['status'].startswith('COMPLETED'), 'incomplete replay: '+day)
        if phase == 'evaluation':
            launch = load(folder.with_suffix('.launch.json'))
            require(launch['selection_frozen_sha256'] == sha(frozen_path), 'evaluation used another selection')
    records, checks = [], []
    for phase, day in phases:
        folder = root/phase/day
        aggregate_path, audit_path = folder/'aggregate.json', folder/'perfect-audit.json'
        audit = load(audit_path)
        require(audit['status'] == 'PASS' and audit['aggregate_sha256'] == sha(aggregate_path), 'independent audit missing, failed or stale')
        if phase == 'search':
            for path in (aggregate_path, audit_path):
                require(sha(path) == frozen['source_sha256'][str(path)], 'fit result changed after selection')
        aggregate = load(aggregate_path)
        require(aggregate['execution_enabled'] is False, 'not offline replay')
        scenarios = {r['id']: r for r in aggregate['scenarios']}
        for path in (aggregate_path, audit_path, folder.with_suffix('.status.json'), folder.with_suffix('.launch.json')):
            provenance[str(path)] = sha(path)
        for pid in pids:
            scenario = scenarios[pid]
            require(audit['scenarios'][pid]['status'] == 'PASS', 'policy audit failed')
            paths = {kind: folder/pid/(kind+'.csv') for kind in ('orders', 'cycles')}
            for kind, path in paths.items():
                expected = audit['scenarios'][pid]['source_sha256'][kind+'.csv']
                require(sha(path) == expected, 'order/cycle changed after audit')
                provenance[str(path)] = sha(path)
            orders, cycles = csv_rows(paths['orders']), csv_rows(paths['cycles'])
            order_groups = defaultdict(list)
            for order in orders:
                order_groups[int(order['cycle'])].append(order)
            require(len({c['cycle'] for c in cycles}) == len(cycles), 'duplicate cycles')
            rows = [cycle_record(day, pid, c, order_groups[int(c['cycle'])]) for c in cycles]
            stats = totals(rows)
            all_fees = sum((decimal(o['fee']) for o in orders), ZERO)
            require(len(orders) == scenario['orders'] and len(cycles) == scenario['cycles'], 'summary row counts')
            require(stats['gross_usdt'] == decimal(scenario['gross_pnl']) and all_fees == decimal(scenario['fees']), 'summary gross/fees')
            require(stats['net_before_funding_usdt'] == decimal(scenario['closed_net_before_funding']), 'summary closed net')
            require(stats['gross_usdt']-all_fees == decimal(scenario['net_before_funding']), 'summary net including open entry fee')
            require(stats['net_wins'] == scenario['wins'] and stats['net_losses'] == scenario['losses'], 'summary win/loss')
            records.extend(rows)
            account_net = decimal(scenario['net_before_funding'])
            checks.append({'date': day, 'phase': phase, 'policy_id': pid, 'closed_cycles': len(cycles), 'orders': len(orders),
                           'funding_status': audit['scenarios'][pid]['funding_status'], 'unresolved_qty': scenario['unresolved_qty'],
                           'open_entry_fees_usdt': all_fees-stats['fees_usdt'], 'cash': scenario['cash'],
                           'account_net_before_funding_usdt': scenario['net_before_funding'], 'halt_reason': scenario['halt_reason'],
                           'loss_cap_hit': scenario['halt_reason'] == 'total_loss_cap',
                           'loss_cap_overshoot_usdt': max(ZERO, -account_net-D(10)) if scenario['halt_reason'] == 'total_loss_cap' else ZERO,
                           'first_entry_ns': min((r['entry_ns'] for r in rows), default=None),
                           'last_exit_ns': max((r['exit_ns'] for r in rows), default=None)})
    by_policy = {}
    for pid in pids:
        rows = [r for r in records if r['policy_id'] == pid]
        by_policy[pid] = diagnostic_policy(rows)
        by_policy[pid]['roles'] = [role for role, value in [('selected', frozen['selected_id']), ('baseline', frozen['baseline_id'])] if value == pid]
        for _, day in phases:
            by_policy[pid]['by_date'].setdefault(day, totals([]))
    result = {'status': 'COMPLETED_DESCRIPTIVE_ATTRIBUTION', 'execution_enabled': False,
              'created_utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'selection_frozen_sha256': sha(frozen_path),
              'selected_id': frozen['selected_id'], 'baseline_id': frozen['baseline_id'], 'date_phases': phases,
              'not_parameter_selection': True, 'not_live_default_change': True,
              'scope': 'closed-cycle gross, four actual fees and net before funding; independent accounts reset daily; unresolved inventory separately reported',
              'fixed_bin_edges': BINS, 'bin_boundary': 'lower inclusive, upper exclusive',
              'bps_definition': {'entry_cross_spread_bps': '20000*(short_bid-long_ask)/(short_bid+long_ask)',
                                 'entry_within_spread_sum_bps': 'sum per venue: 20000*(ask-bid)/(ask+bid)',
                                 'entry_liquidation_cost_bps': '-immediate_reverse_L1_net_including_four_fees / entry_long_notional *10000',
                                 'net_bps_on_sum_entry_long_notional': 'sum closed net / sum closed entry long notional *10000; not portfolio return'},
              'by_policy': by_policy, 'records': records, 'day_policy_checks': checks,
              'next_round_directions': directions([r for r in records if r['policy_id'] == frozen['selected_id']], checks),
              'loss_cap_interpretation': {'cap_hit_day_policy_count': sum(r['loss_cap_hit'] for r in checks),
                                          'daily_limit_usdt': 10,
                                          'warning': 'Worst-day rankings near the same 10U cap may reflect overshoot differences and truncated opportunity coverage, not significant performance improvement.'},
              'source_sha256': provenance,
              'limitations': ['No intraholding path: cannot quantify missed take profits or maximum favorable/adverse excursion.',
                              'Group differences are descriptive, not causal effects or independently optimized thresholds.',
                              'October is an already-seen internal temporal evaluation, not untouched future evidence.',
                              'Perfect exit depth and minimum assumptions must not be presented as executable real fills.']}
    with destinations[0].open('x') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, default=str)
        stream.write('\n')
    with destinations[1].open('x') as stream:
        stream.write(markdown(result))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True)
    args = parser.parse_args()
    outcome = build(args.experiment)
    print(json.dumps({'status': outcome['status'], 'selected_id': outcome['selected_id'], 'policies': len(outcome['by_policy']), 'closed_cycle_records': len(outcome['records'])}))
