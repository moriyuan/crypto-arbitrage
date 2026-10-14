"""Audited, descriptive comparison of three predeclared activity gates.

No gate selection, execution, parameter changes, or exchange access. October
economics are read only after the experiment's selection has been frozen.
"""
import argparse
from collections import defaultdict
import csv
import datetime as dt
from decimal import Decimal as D
import hashlib
import json
import mmap
from pathlib import Path
import struct
import sys
import numpy as np

OLD_CODE = Path(__file__).resolve().parent.parent/'20261013-btw-perfect-taker-code'
sys.path.insert(0, str(OLD_CODE))
from attribution import cycle_record, diagnostic_policy, temporal_losses, totals
from microstructure_attribution import decompose

ACTIVITY = struct.Struct('<qIIqqqqQ')
GATES = ('q25', 'q50', 'q75')
SIDE_ORDER = ['bybit_ask', 'bybit_bid', 'bitget_ask', 'bitget_bid']
ZERO = D(0)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def load(path):
    return json.loads(Path(path).read_text())


def read_csv(path):
    with Path(path).open(newline='') as stream:
        return list(csv.DictReader(stream))


def threshold(path):
    rows = read_csv(path)
    require(len(rows) == 2 and {r['name'] for r in rows} == {'q10', 'q25'}, 'threshold aliases must match frozen engine schema')
    row = next(r for r in rows if r['name'] == 'q25')
    keys = ('countA', 'countB', 'turnoverAunits', 'turnoverBunits', 'maxAgeNs')
    values = tuple(int(row[k]) for k in keys)
    require(all(n >= 0 for n in values), 'negative activity threshold')
    return values


def admitted(activity, limits):
    return (activity[7] == 3 and activity[1] >= limits[0] and activity[2] >= limits[1]
            and activity[3] >= limits[2] and activity[4] >= limits[3]
            and 0 <= activity[5] <= limits[4] and 0 <= activity[6] <= limits[4])


def rejection_reasons(activity, limits):
    reasons = []
    for v, venue in enumerate(('bybit', 'bitget')):
        if not activity[7] & (1 << v):
            reasons.append(venue+'_warm')
        if activity[1+v] < limits[v]:
            reasons.append(venue+'_count')
        if activity[3+v] < limits[2+v]:
            reasons.append(venue+'_turnover')
        if not 0 <= activity[5+v] <= limits[4]:
            reasons.append(venue+'_trade_age')
    return reasons


def activity_at_entries(path, thresholds, target_ns, start_ns):
    """One native-order pass per date; no nearest-second or future lookup."""
    target_ns = set(target_ns)
    found, previous, count = {}, -1, 0
    coverage = {gate: {'post_noon_native_rows': 0, 'activity_pass_rows': 0} for gate in thresholds}
    with Path(path).open('rb') as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data:
        require(data[:8] == b'BTWACT01' and (len(data)-8) % ACTIVITY.size == 0, 'activity magic/length')
        for offset in range(8, len(data), ACTIVITY.size):
            row = ACTIVITY.unpack_from(data, offset)
            ns = row[0]
            require(ns > previous and ns > 0 and 0 <= row[7] <= 3 and min(row[3:5]) >= 0 and min(row[5:7]) >= -1, 'invalid native activity row')
            previous = ns
            count += 1
            if ns >= start_ns:
                for gate, limits in thresholds.items():
                    coverage[gate]['post_noon_native_rows'] += 1
                    coverage[gate]['activity_pass_rows'] += admitted(row, limits)
            if ns in target_ns:
                found[ns] = row
    require(set(found) == target_ns, 'entry timestamp missing from exact native activity stream')
    for value in coverage.values():
        value['activity_pass_fraction'] = D(value['activity_pass_rows'])/value['post_noon_native_rows'] if value['post_noon_native_rows'] else None
        value['scope'] = 'Native decision-count fraction at/after entry start; not clock-time coverage and not final entry eligibility.'
    return found, coverage, count


def uniform_coverage(features, thresholds, start_ns, end_ns):
    """A true equally spaced time denominator, including inactive seconds."""
    require(features.dtype == np.dtype('int64') and features.ndim == 2 and features.shape[1] == 8, 'uniform feature schema')
    window = features[(features[:, 0] >= start_ns) & (features[:, 0] < end_ns)]
    expected = np.arange(start_ns, end_ns, 10**9, dtype=np.int64)
    require(np.array_equal(window[:, 0], expected), 'uniform clock coverage must include every second')
    hours = (window[:, 0]//(3600*10**9)) % 24
    result = {}
    for gate, limits in thresholds.items():
        mask = ((window[:, 7] == 3) & (window[:, 1] >= limits[0]) & (window[:, 2] >= limits[1])
                & (window[:, 3] >= limits[2]) & (window[:, 4] >= limits[3])
                & (window[:, 5] >= 0) & (window[:, 5] <= limits[4])
                & (window[:, 6] >= 0) & (window[:, 6] <= limits[4]))
        allowed, count = int(mask.sum()), len(window)
        hourly = {}
        for hour in sorted(set(map(int, hours))):
            subset = hours == hour
            n, passing = int(subset.sum()), int((mask & subset).sum())
            hourly[f'{hour:02}:00'] = {'allowed_seconds': passing, 'total_seconds': n, 'fraction': D(passing)/n if n else None}
        result[gate] = {'activity_allowed_seconds': allowed, 'activity_total_seconds': count,
                        'activity_time_fraction': D(allowed)/count if count else None, 'by_UTC_hour': hourly,
                        'start_ns_inclusive': start_ns, 'end_ns_exclusive': end_ns,
                        'scope': 'Only both-venue activity count, turnover, age and warm conditions; excludes book, funding, balance, position and cooldown eligibility.'}
    return result


def decomposition_summary(records):
    columns = ('mid_basis_convergence_usdt', 'bid_ask_crossing_cost_usdt', 'gross_usdt', 'fees_usdt', 'net_before_funding_usdt')
    value = {column: sum((r[column] for r in records), ZERO) for column in columns}
    value.update(cycles=len(records), converging_cycles=sum(r['mid_basis_convergence_usdt'] > 0 for r in records),
                 convergence_but_net_loss=sum(r['mid_basis_convergence_usdt'] > 0 and r['net_before_funding_usdt'] < 0 for r in records),
                 gross_winners=sum(r['gross_usdt'] > 0 for r in records), net_winners=sum(r['net_before_funding_usdt'] > 0 for r in records))
    require(value['mid_basis_convergence_usdt']-value['bid_ask_crossing_cost_usdt']-value['fees_usdt'] == value['net_before_funding_usdt'], 'summed attribution identity')
    return value


def entry_summary(entries):
    closed = [e for e in entries if e['original_q25_net_usdt'] is not None]
    return {'entries': len(entries), 'closed_original_q25_cycles': len(closed),
            'original_q25_net_winners': sum(e['original_q25_net_usdt'] > 0 for e in closed),
            'original_q25_net_losers': sum(e['original_q25_net_usdt'] < 0 for e in closed),
            'original_q25_net_zero': sum(e['original_q25_net_usdt'] == 0 for e in closed),
            'original_q25_net_usdt': sum((e['original_q25_net_usdt'] for e in closed), ZERO)}


def baseline_admission(baseline_entries, actual_entries, observations, limits):
    """The base PnL belongs to q25's observed path, never hypothetical q50 PnL."""
    baseline = {(e['entry_ns'], e['long_venue']): e for e in baseline_entries}
    actual = {(e['entry_ns'], e['long_venue']): e for e in actual_entries}
    require(len(baseline) == len(baseline_entries) and len(actual) == len(actual_entries), 'duplicate entry identity')
    blocked, allowed, missing_after_pass, matched, new = [], [], [], [], []
    for key, entry in baseline.items():
        row = observations[entry['entry_ns']]
        value = {'cycle': entry['cycle'], 'entry_ns': entry['entry_ns'], 'long_venue': entry['long_venue'],
                 'qty': entry['qty'], 'original_q25_net_usdt': entry['net_usdt'],
                 'activity': {'countA': row[1], 'countB': row[2], 'turnoverAunits': row[3], 'turnoverBunits': row[4], 'ageAns': row[5], 'ageBns': row[6], 'warm_mask': row[7]},
                 'rejection_reasons': rejection_reasons(row, limits)}
        if admitted(row, limits):
            allowed.append(value)
            if key not in actual:
                missing_after_pass.append(value)
        else:
            blocked.append(value)
            require(key not in actual, 'actual entry violates its higher activity gate')
        if key in actual:
            other = actual[key]
            matched.append({'entry_ns': entry['entry_ns'], 'long_venue': entry['long_venue'],
                            'q25_cycle': entry['cycle'], 'actual_cycle': other['cycle'],
                            'q25_qty': entry['qty'], 'actual_qty': other['qty'],
                            'q25_net_usdt': entry['net_usdt'], 'actual_net_usdt': other['net_usdt']})
    for key, entry in actual.items():
        require(admitted(observations[entry['entry_ns']], limits), 'actual entry activity check failed')
        if key not in baseline:
            new.append(entry)
    reasons = defaultdict(int)
    for row in blocked:
        for reason in row['rejection_reasons']:
            reasons[reason] += 1
    return {'blocked_q25_entries': entry_summary(blocked), 'passed_q25_entries': entry_summary(allowed),
            'passed_but_not_actual_entries': entry_summary(missing_after_pass),
            'matched_entries': len(matched), 'matched_with_different_qty': sum(r['q25_qty'] != r['actual_qty'] for r in matched),
            'new_actual_entries': len(new), 'missing_q25_entries': len(set(baseline)-set(actual)),
            'blocked_rejection_reasons_nonexclusive': dict(sorted(reasons.items())),
            'blocked_details': blocked, 'passed_but_not_actual_details': missing_after_pass,
            'matched_details': matched, 'new_entry_details': new,
            'matching_identity': 'exact native entry_ns + long venue; quantity is compared separately',
            'causal_caveat': 'Blocked baseline PnL is q25 historical PnL, not avoided loss or predicted higher-gate PnL. Gates change holding, cooldown, cash, sizing, subsequent opportunities and loss-cap timing.'}


def checked_day(root, source_manifest, day, phase, gate, policy, hashes):
    folder = root/'variants'/gate/phase/day
    paths = {name: folder/name for name in ('aggregate.json', 'perfect-audit.json', 'perfect-deep-audit.json', 'perfect-deep-quotes.json')}
    aggregate, light, deep, quotes = (load(paths[name]) for name in paths)
    aggregate_sha = sha(paths['aggregate.json'])
    require(aggregate['execution_enabled'] is False and light['status'] == deep['status'] == quotes['status'] == 'PASS', 'all audits must pass: '+day+'/'+gate)
    require(light['aggregate_sha256'] == deep['aggregate_sha256'] == quotes['aggregate_sha256'] == aggregate_sha, 'stale audit aggregate hash')
    require(deep['light_audit_sha256'] == sha(paths['perfect-audit.json']) and deep['source_entry_exit_quotes_sha256'] == sha(paths['perfect-deep-quotes.json']), 'stale audited quotes/light ledger')
    require(set(deep['audited_ids']) == {policy['id']} and quotes['side_order'] == SIDE_ORDER, 'deep audit policy/layout')
    require(len(aggregate['scenarios']) == 1 and aggregate['scenarios'][0]['id'] == policy['id'], 'only fixed policy may be compared')
    scenario = aggregate['scenarios'][0]
    for key in ('entry_bps', 'tp_bps', 'sl_bps', 'hard_bps', 'hold_ms'):
        require(scenario[key] == policy[key], 'trading parameter changed across activity variants')
    files = source_manifest['days'][day]['files']
    require(deep['source_sha256']['activity'] == files['activity']['sha256'], 'activity differs from source experiment')
    require(deep['source_sha256']['tape'] == quotes['tape_sha256'] == files['tape']['sha256'], 'quote/tape binding differs from source experiment')
    threshold_path = root/'calibration'/day/gate/'thresholds.csv'
    calibration_path = threshold_path.with_name('calibration.json')
    calibration = load(calibration_path)
    require(calibration['status'] == 'PASS' and calibration['true_quantile_percentile'] == int(gate[1:]), 'calibration quantile identity')
    require(calibration['thresholds_sha256'] == sha(threshold_path) and calibration['protocol_sha256'] == sha(root/'protocol.json'), 'calibration hash binding')
    require(deep['source_sha256']['thresholds'] == sha(threshold_path), 'wrong quantile threshold alias used')
    limits = threshold(threshold_path)
    hashes[str(threshold_path)] = sha(threshold_path)
    hashes[str(calibration_path)] = sha(calibration_path)
    for label in ('orders', 'cycles', 'equity'):
        path = folder/policy['id']/(label+'.csv')
        require(light['scenarios'][policy['id']]['source_sha256'][label+'.csv'] == sha(path), 'ledger changed since audit')
        paths[label] = path
    orders, cycles = read_csv(paths['orders']), read_csv(paths['cycles'])
    order_groups = defaultdict(list)
    for order in orders:
        order_groups[int(order['cycle'])].append(order)
    require(len({c['cycle'] for c in cycles}) == len(cycles), 'duplicate cycle')
    quote_map = {(q['id'], q['cycle']): q for q in quotes['cycles']}
    require(len(quote_map) == len(quotes['cycles']) and set(quote_map) == {(policy['id'], int(c['cycle'])) for c in cycles}, 'deep quotes must cover each closed cycle exactly')
    records = []
    for cycle in cycles:
        number = int(cycle['cycle'])
        record = cycle_record(day, policy['id'], cycle, order_groups[number])
        quote = quote_map[policy['id'], number]
        require(all(int(cycle[k]) == quote[k] for k in ('qty', 'entry_ns', 'exit_ns')) and cycle['long_venue'] == quote['long_venue'], 'source quote cycle identity')
        require(quote['price_decimals'] == aggregate['price_decimals'], 'source quote scale')
        unit = D(10)**-quote['price_decimals']
        identity = decompose([D(p)*unit for p in quote['entry_prices']], [D(p)*unit for p in quote['exit_prices']], quote['qty'], quote['long_venue'])
        for field, other in (('gross_pnl', 'gross_usdt'), ('fees', 'fees_usdt'), ('net', 'net_before_funding_usdt')):
            require(identity[field] == record[other], 'source midpoint decomposition differs from ledger')
        record.update(gate=gate, quantile=int(gate[1:]), phase=phase,
                      mid_basis_convergence_usdt=identity['mid_basis_convergence'], bid_ask_crossing_cost_usdt=identity['bid_ask_crossing_cost'])
        records.append(record)
    values = totals(records)
    require(len(records) == scenario['cycles'] and len(orders) == scenario['orders'], 'summary row count')
    require(values['gross_usdt'] == D(scenario['gross_pnl']) and values['net_before_funding_usdt'] == D(scenario['closed_net_before_funding']), 'summary realized gross/net')
    require(sum((D(o['fee']) for o in orders), ZERO) == D(scenario['fees']), 'summary paid fees')
    require(values['net_wins'] == scenario['wins'] and values['net_losses'] == scenario['losses'], 'summary winners/losers')
    lookup = {r['cycle']: r for r in records}
    entries = []
    for number, rows in order_groups.items():
        opened = [r for r in rows if r['intent'] == 'entry']
        require(len(opened) == 2 and len({r['ns'] for r in opened}) == len({r['qty'] for r in opened}) == 1, 'entry pair identity')
        long_order = next(r for r in opened if r['side'] == 'buy')
        record = lookup.get(number)
        entries.append({'cycle': number, 'entry_ns': int(long_order['ns']), 'long_venue': long_order['venue'], 'qty': int(long_order['qty']),
                        'net_usdt': record['net_before_funding_usdt'] if record else None})
    for path in paths.values():
        hashes[str(path)] = sha(path)
    row = {'date': day, 'phase': phase, 'gate': gate, 'quantile': int(gate[1:]), 'policy_id': policy['id'],
           'cycles': scenario['cycles'], 'orders': scenario['orders'], 'wins': scenario['wins'], 'losses': scenario['losses'],
           'gross_usdt': D(scenario['gross_pnl']), 'fees_usdt': D(scenario['fees']), 'net_before_funding_usdt': D(scenario['net_before_funding']),
           'closed_net_before_funding_usdt': D(scenario['closed_net_before_funding']), 'liquidation_max_drawdown_usdt': D(scenario['complete_mark_max_drawdown_usdt']),
           'sum_closed_entry_long_notional_usdt': values['entry_long_notional_usdt'],
           'closed_net_bps_on_sum_entry_long_notional': values['net_bps_on_sum_entry_long_notional'],
           'bybit_cash': D(scenario['cash']['bybit']), 'bitget_cash': D(scenario['cash']['bitget']), 'unresolved_qty': scenario['unresolved_qty'],
           'funding_status': light['scenarios'][policy['id']]['funding_status'], 'exit_reasons': scenario['exit_reasons'], 'halt_reason': scenario['halt_reason'],
           'loss_cap_hit': scenario['halt_reason'] == 'total_loss_cap',
           'loss_cap_overshoot_usdt': max(ZERO, -D(scenario['net_before_funding'])-D(10)) if scenario['halt_reason'] == 'total_loss_cap' else ZERO,
           'exit_depth_assumed_cycles': sum(r['exit_depth_assumed'] for r in records),
           'exit_depth_assumed_cycle_fraction': D(sum(r['exit_depth_assumed'] for r in records))/len(records) if records else None,
           'exit_depth_assumed_net_usdt': sum((r['net_before_funding_usdt'] for r in records if r['exit_depth_assumed']), ZERO),
           'exit_below_min_orders': scenario['exit_below_min_orders'], 'total_exit_excess_depth_qty': scenario['total_exit_excess_depth_qty'],
           'decomposition': decomposition_summary(records), 'thresholds': dict(zip(('countA', 'countB', 'turnoverAunits', 'turnoverBunits', 'maxAgeNs'), limits))}
    return row, records, entries, limits


def next_directions(rows, admission, records):
    by_gate = {gate: decomposition_summary([r for r in records if r['gate'] == gate]) for gate in GATES}
    high_cost = [r for r in records if r['entry_liquidation_cost_bps'] >= 30]
    timeout = [r for r in records if r['exit_reason'] == 'holding_timeout']
    return [
        {'direction': '检验活动门槛是否改善每次交易的可捕获空间，避免只压低样本量',
         'evidence': {'by_gate': by_gate, 'blocked_q25_by_date_and_gate': {day: {gate: stats['blocked_q25_entries'] for gate, stats in gates.items()} for day, gates in admission.items()}},
         'hypothesis': '更活跃可能缩窄盘口并提高成交质量，也可能同时消灭跨所价差。只有毛利、成本及跨日稳定性共同改善时，才支持提高门槛；不能事后放宽少于10笔的选参准入。',
         'test': '保留本轮训练选择规则，追加未使用日期；同时报告每组合/每名义金额收益、样本数和活动通过率，不把挡住的基准亏损当因果收益。'},
        {'direction': '增加入场可计算的清算成本与反向深度约束',
         'evidence': {'cost_ge_30bp_fixed_descriptive_bin': decomposition_summary(high_cost),
                      'exit_depth_assumptions': [{'date': r['date'], 'gate': r['gate'], 'cycles': r['exit_depth_assumed_cycles'], 'net_usdt': r['exit_depth_assumed_net_usdt']} for r in rows]},
         'hypothesis': '成交额和成交笔数高并不保证价差够窄或退出深度足够。可单独研究 max_entry_liquidation_cost_bps 与入场时的 min_exit_depth_coverage；30bp只是固定描述分箱。',
         'test': '先固定候选成本/覆盖阈值，在新训练期比较；恢复实际退出深度/延迟/部分成交后再判断收益，不读取未来退出盘口作入场限制。'},
        {'direction': '用价差回归进展研究超时退出',
         'evidence': {'holding_timeout': decomposition_summary(timeout)},
         'hypothesis': '中价基差有利变化仍可能不足以支付盘口价差与手续费。可记录当前净捕获、距成本覆盖的差额及持仓路径，再研究 progress_timeout。净亏并不证明价差永不回归。',
         'test': '先采集最大有利/不利进展并预设检查时点，保持硬止损；不由本次开平记录臆测未来止盈或直接延长持仓。'},
        {'direction': '分开评估连续亏损暂停和累计亏损上限影响',
         'evidence': {'loss_cap_hits': [r for r in rows if r['loss_cap_hit']],
                      'max_loss_streaks': {gate: temporal_losses([r for r in records if r['gate'] == gate]) for gate in GATES}},
         'hypothesis': '活动过滤改变冷却、现金、数量和停止交易时点，最终差异可能来自整条账户路径。连亏暂停可作独立待验证限制，但连亏不保证下一笔继续亏损。',
         'test': '新增日期预先规定少量连亏阈值和恢复条件，报告被挡住的盈利机会、覆盖时长与亏损上限越界额，不自动更改本轮默认。'},
    ]


def fmt(value, places=4):
    return '—' if value is None else f'{D(value):.{places}f}'


def report(comparison, attribution):
    lines = ['# 仅活动门槛变化：盈亏归因', '',
             f"固定交易参数为 {comparison['policy']['id']}；q25/q50/q75 分别使用同日00–12UTC标定的阈值，12UTC后允许开仓。训练期冻结主验证门槛为 q{comparison['selected_quantile']}，其余为预先声明的诊断对照。未在此报告重新选门槛。", '',
             '**净损益 = 跨所中价基差收敛收益 − 开平四次半价差成本 − 四笔手续费。** 中价仅用于事后分解，成交始终按对手一档。三日账户分别重置，合计不是连续账户收益率。', '',
             '| 日期 | 门槛 | 组合/净赢 | 中价基差收敛 | 盘口成本 | 手续费 | 净额 | 最大回撤 | 超深度退出组 |',
             '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for row in comparison['rows']:
        identity = row['decomposition']
        lines.append(f"| {row['date']} | {row['gate']} | {row['cycles']}/{row['wins']} | {fmt(identity['mid_basis_convergence_usdt'])} | {fmt(identity['bid_ask_crossing_cost_usdt'])} | {fmt(row['fees_usdt'])} | {fmt(row['net_before_funding_usdt'])} | {fmt(row['liquidation_max_drawdown_usdt'])} | {row['exit_depth_assumed_cycles']} |")
    lines += ['', '## 活动时间覆盖与名义金额归一化', '',
              '12:00≤UTC<23:58，共43080个一秒格点。允许秒数只检查活动门槛，不含盘口、资金费、余额和持仓限制。bp分母为已闭合组合入场做多单腿名义金额之和，不是账户收益率。', '',
              '| 日期 | 门槛 | 允许秒数/43080 | 时间比例 | 单腿名义金额合计 | 净额/名义金额 bp |', '|---|---|---:|---:|---:|---:|']
    for row in comparison['rows']:
        coverage = row['activity_time_coverage']
        lines.append(f"| {row['date']} | {row['gate']} | {coverage['activity_allowed_seconds']}/{coverage['activity_total_seconds']} | {fmt((coverage['activity_time_fraction'] or ZERO)*100, 2)}% | {fmt(row['sum_closed_entry_long_notional_usdt'])} | {fmt(row['closed_net_bps_on_sum_entry_long_notional'], 2)} |")
    lines += ['', '## 基准入场被门槛阻挡的情况', '',
              '下表是原q25入场时点的精确活动条件检查，盈亏仍属于原q25账户路径；它不是更高门槛的“避免损失”或反事实收益。匹配按相同纳秒入场时点和相同多空方向，数量差异单独记录。', '',
              '| 日期 | 门槛 | 被挡q25组 | 原赢家/输家 | 被挡组原净额 | 同时点同方向 | 更高门槛新入场 | 条件通过但未重现 |',
              '|---|---|---:|---:|---:|---:|---:|---:|']
    for day, gates in comparison['baseline_admission'].items():
        for gate, value in gates.items():
            blocked = value['blocked_q25_entries']
            lines.append(f"| {day} | {gate} | {blocked['entries']} | {blocked['original_q25_net_winners']}/{blocked['original_q25_net_losers']} | {fmt(blocked['original_q25_net_usdt'])} | {value['matched_entries']} | {value['new_actual_entries']} | {value['passed_but_not_actual_entries']['entries']} |")
    lines += ['', '## 各门槛的盈亏来源', '']
    for gate in GATES:
        info = attribution['by_gate'][gate]
        identity = info['decomposition']
        lines += [f"**{gate}**：{identity['cycles']}组，{identity['converging_cycles']}组中价基差向有利方向变化，其中{identity['convergence_but_net_loss']}组扣盘口成本和手续费后仍亏；最终净赢{identity['net_winners']}组。", '',
                  '| 退出原因 | 组合 | 毛利 | 手续费 | 净额 | 净赢 |', '|---|---:|---:|---:|---:|---:|']
        for reason, value in info['groups']['exit_reason'].items():
            lines.append(f"| {reason} | {value['cycles']} | {fmt(value['gross_usdt'])} | {fmt(value['fees_usdt'])} | {fmt(value['net_before_funding_usdt'])} | {value['net_wins']} |")
        lines.append('')
        for day, stats in info['temporal_losses_by_date'].items():
            lines.append(f"- {day} 最长连续净亏 {stats['max_consecutive_net_losses']} 笔，该段净额 {fmt(stats['max_streak_net_usdt'])}U。")
        lines.append('')
    lines += ['## 下一轮可验证方向', '', '以下是待验证限制与研究方向，不保证收益改善，不改变当前政策或门槛。', '']
    for i, item in enumerate(attribution['next_round_directions'], 1):
        lines += [f"{i}. **{item['direction']}**：{item['hypothesis']} 验证：{item['test']}", '']
    lines += ['## 边界', '',
              '- 时间覆盖使用一秒等间距样本；另存的原生事件通过率不是时钟覆盖率，两者都不是最终可交易机会比例。',
              '- 活动过滤会改变持仓、冷却、余额、下次仓位及亏损上限触发时间；更高门槛的实际交易不是简单删除q25交易。',
              '- 所有数值绑定逐单与逐笔深审，资金费状态、现金、逐日停止原因和深度假设保存在comparison.json。超深度全额退出仍是理想假设。',
              '- 只有开平报价，未观察到的未来持仓路径不能用来声称错过止盈或永久不回归。所有日期此前已研究，不是全新的未见样本。', '']
    return '\n'.join(lines)


def run(experiment):
    root = Path(experiment).resolve()
    outputs = [root/name for name in ('comparison.json', 'comparison.csv', 'attribution.json', 'attribution.md')]
    require(not any(p.exists() for p in outputs), 'comparison outputs already exist; refusing overwrite')
    protocol_path, frozen_path = root/'protocol.json', root/'selection-frozen.json'
    protocol, frozen = load(protocol_path), load(frozen_path)
    require(frozen['evaluation_results_consumed'] is False and frozen['selected_quantile'] in (25, 50, 75), 'selection must be frozen without evaluation economics')
    require(frozen['protocol_sha256'] == sha(protocol_path), 'frozen protocol changed')
    require(frozen['prepared_sha256'] == sha(root/'prepared.json'), 'prepared inputs changed after gate selection')
    for name, expected in frozen['source_sha256'].items():
        require(sha(name) == expected, 'fit source changed after gate selection')
    input_audit = load(root/'activity-input-acceptance.json')
    require(input_audit['status'] == 'PASS' and input_audit['protocol_sha256'] == sha(protocol_path)
            and input_audit['prepared_sha256'] == frozen['prepared_sha256'], 'activity source audit absent or stale')
    require(protocol['quantiles'] == [25, 50, 75], 'predeclared gate grid changed')
    policy = protocol['fixed_policy']
    source_manifest_path = Path(protocol['source_experiment'])/'input-manifest.json'
    source_manifest = load(source_manifest_path)
    hashes = {str(path): sha(path) for path in (protocol_path, frozen_path, root/'prepared.json', root/'activity-input-acceptance.json', source_manifest_path, Path(__file__),
                                               OLD_CODE/'attribution.py', OLD_CODE/'microstructure_attribution.py', OLD_CODE/'run_day.py')}
    dates = [(day, 'search') for day in protocol['fit_dates']] + [(protocol['evaluation_date'], 'evaluation')]
    rows, records, admissions, entry_data = [], [], {}, {}
    for day, phase in dates:
        thresholds, entries = {}, {}
        for gate in GATES:
            row, cycles, actual, limits = checked_day(root, source_manifest, day, phase, gate, policy, hashes)
            rows.append(row);records.extend(cycles);entries[gate] = actual;thresholds[gate] = limits
        source = source_manifest['days'][day]
        activity_path = Path(source['files']['activity']['path'])
        require(sha(activity_path) == source['files']['activity']['sha256'], 'source activity hash changed')
        hashes[str(activity_path)] = source['files']['activity']['sha256']
        targets = {e['entry_ns'] for group in entries.values() for e in group}
        observations, coverage, count = activity_at_entries(activity_path, thresholds, targets, source['entry_start_ns'])
        uniform_path = activity_path.with_name('features-uniform-seconds.npz')
        uniform_hash = sha(uniform_path)
        require(input_audit['days'][day]['uniform_features_sha256'] == uniform_hash
                and input_audit['days'][day]['native_activity_sha256'] == hashes[str(activity_path)], 'independent activity verification input changed')
        for gate in GATES:
            calibration = load(root/'calibration'/day/gate/'calibration.json')
            require(calibration['source_sha256'] == uniform_hash, 'uniform grid differs from calibrated input')
        hashes[str(uniform_path)] = uniform_hash
        with np.load(uniform_path, allow_pickle=False) as archive:
            features = archive['features']
        require(features.shape == (86400, 8), 'daily uniform features require 86400 rows')
        noon = source['entry_start_ns']
        time_coverage = uniform_coverage(features, thresholds, noon, noon+43080*10**9)
        require(all(admitted(observations[e['entry_ns']], thresholds['q25']) for e in entries['q25']), 'q25 entry does not pass q25')
        admissions[day] = {gate: baseline_admission(entries['q25'], entries[gate], observations, thresholds[gate]) for gate in ('q50', 'q75')}
        for row in rows:
            if row['date'] == day:
                row['activity_coverage'] = coverage[row['gate']]
                row['activity_native_rows_total'] = count
                row['activity_time_coverage'] = time_coverage[row['gate']]
        entry_data[day] = entries
        print(json.dumps({'date': day, 'audited_gates': len(GATES), 'closed_cycles': sum(r['cycles'] for r in rows if r['date'] == day), 'native_activity_rows': count}), flush=True)
    by_gate = {}
    for gate in GATES:
        sample = [r for r in records if r['gate'] == gate]
        by_gate[gate] = diagnostic_policy(sample)
        by_gate[gate]['decomposition'] = decomposition_summary(sample)
        by_gate[gate]['decomposition_by_date'] = {day: decomposition_summary([r for r in sample if r['date'] == day]) for day, _ in dates}
    comparison = {'status': 'PASS_AUDITED_ACTIVITY_COMPARISON', 'execution_enabled': False, 'created_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
                  'selected_quantile': frozen['selected_quantile'], 'policy': policy, 'rows': rows,
                  'baseline_admission': admissions, 'actual_entry_records': entry_data, 'source_sha256': hashes,
                  'not_gate_selection': True, 'not_live_default_change': True,
                  'account_scope': 'Daily independent continuous accounts; no cross-date portfolio return.',
                  'baseline_admission_caveat': 'Descriptive q25 entry filtering only; blocked PnL is not causal avoided loss.'}
    attribution = {'status': 'PASS_EXACT_MICROSTRUCTURE_AND_LEDGER_ATTRIBUTION', 'execution_enabled': False,
                   'identity': 'net = midpoint basis convergence - four half-spreads - four fees',
                   'selected_quantile': frozen['selected_quantile'], 'policy': policy, 'by_gate': by_gate, 'records': records,
                   'next_round_directions': next_directions(rows, admissions, records), 'source_sha256': hashes,
                   'scope': 'All three predeclared gates described; only train-frozen gate is primary evaluation. No selection or causal treatment-effect estimate.'}
    fields = ['date', 'phase', 'gate', 'quantile', 'policy_id', 'cycles', 'orders', 'wins', 'losses',
              'gross_usdt', 'fees_usdt', 'net_before_funding_usdt', 'liquidation_max_drawdown_usdt', 'bybit_cash', 'bitget_cash',
              'funding_status', 'unresolved_qty', 'halt_reason', 'loss_cap_hit', 'loss_cap_overshoot_usdt',
              'exit_depth_assumed_cycles', 'exit_depth_assumed_cycle_fraction', 'exit_depth_assumed_net_usdt',
              'exit_below_min_orders', 'total_exit_excess_depth_qty', 'mid_basis_convergence_usdt', 'bid_ask_crossing_cost_usdt',
              'sum_closed_entry_long_notional_usdt', 'closed_net_bps_on_sum_entry_long_notional',
              'activity_allowed_seconds', 'activity_total_seconds', 'activity_time_fraction',
              'converging_cycles', 'convergence_but_net_loss', 'activity_pass_rows', 'post_noon_native_rows', 'activity_pass_fraction', 'exit_reasons']
    for path, value in ((outputs[0], comparison), (outputs[2], attribution)):
        with path.open('x') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, default=str);stream.write('\n')
    with outputs[1].open('x', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            value = {**row['decomposition'], **row['activity_coverage'], **row['activity_time_coverage'], **row}
            value['exit_reasons'] = json.dumps(row['exit_reasons'], sort_keys=True)
            writer.writerow({field: value[field] for field in fields})
    with outputs[3].open('x') as stream:
        stream.write(report(comparison, attribution))
    return comparison, attribution


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True)
    result, detailed = run(parser.parse_args().experiment)
    print(json.dumps({'status': result['status'], 'day_gate_rows': len(result['rows']), 'closed_cycle_records': len(detailed['records'])}))
