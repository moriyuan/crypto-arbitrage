"""Read-only audited attribution of predeclared volatility entry gates.

No selection, execution, parameter change, or completion marker is performed.
All economics are read after the fit selection is frozen. Decimal monetary
identities use source L1 quotations, not rounded report statistics.
"""
import argparse
from collections import defaultdict
import csv
import datetime as dt
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

OLD_CODE = Path(__file__).resolve().parent.parent/'20261013-btw-perfect-taker-code'
sys.path.insert(0, str(OLD_CODE))
from attribution import cycle_record, diagnostic_policy, totals
from microstructure_attribution import decompose

ZERO = D(0)
SIDE_ORDER = ['bybit_ask', 'bybit_bid', 'bitget_ask', 'bitget_bid']
GATE_DTYPE = np.dtype([('ns', '<i8'), ('mask', '<u4')])
CAVEAT = ('Rejected reference PnL is observed original-path PnL, not avoided loss or a causal improvement. '
          'The gate changes holding, cooldown, cash, quantity, loss-cap timing and subsequent entries.')


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


def coverage_control(policy):
    if policy['require_basis'] and policy['require_cooling']:
        return 'joint_coverage'
    if policy['require_basis']:
        return 'basis_coverage'
    if policy['require_cooling']:
        return 'cooling_coverage'
    return 'baseline'


def masks_at_entries(path, policy_count, target_ns):
    """Exact native clock; never round an entry to a nearby feature second."""
    path = Path(path)
    with path.open('rb') as stream:
        require(stream.read(8) == b'BTWGATE1', 'gate magic')
    require(1 <= policy_count <= 32 and (path.stat().st_size-8) % 12 == 0, 'gate length/policy count')
    data = np.memmap(path, mode='r', dtype=GATE_DTYPE, offset=8)
    require(len(data) > 0 and np.all(data['ns'] > 0) and np.all(data['ns'][1:] > data['ns'][:-1]), 'gate native clock ordering')
    require(not np.any(data['mask'].astype(np.uint64) >> policy_count), 'gate unknown policy bits')
    require(np.all(data['mask'] & 1), 'baseline bit must always pass')
    result = {}
    for ns in target_ns:
        i = int(np.searchsorted(data['ns'], ns))
        require(i < len(data) and int(data['ns'][i]) == ns, 'entry timestamp absent from exact native gate clock')
        result[int(ns)] = int(data['mask'][i])
    return result


def feature_time_coverage(arrays, start_ns, end_ns):
    ns = arrays['ns']
    in_window = (ns >= start_ns) & (ns < end_ns)
    window = ns[in_window]
    require(np.array_equal(window, np.arange(start_ns, end_ns, 10**9, dtype=np.int64)), 'feature clock must include every second')
    basis = arrays['basis_valid'][in_window].astype(bool)
    cooling = arrays['cooling_valid'][in_window].astype(bool)
    result = {'start_ns_inclusive': start_ns, 'end_ns_exclusive': end_ns, 'total_seconds': len(window),
              'scope': 'Only required rolling-feature validity; excludes thresholds, base activity, funding, book-entry, cash, cooldown and position eligibility.'}
    hours = window//(3600*10**9) % 24
    for key, mask in [('baseline', np.ones(len(window), dtype=bool)), ('basis_coverage', basis),
                      ('cooling_coverage', cooling), ('joint_coverage', basis & cooling)]:
        hourly = {}
        for hour in sorted(set(map(int, hours))):
            part = hours == hour
            allowed, count = int((part & mask).sum()), int(part.sum())
            hourly[f'{hour:02}:00'] = {'allowed_seconds': allowed, 'total_seconds': count,
                                      'time_fraction': D(allowed)/count if count else None}
        allowed = int(mask.sum())
        result[key] = {'allowed_seconds': allowed, 'time_fraction': D(allowed)/len(window) if len(window) else None,
                       'by_UTC_hour': hourly}
    return result


def entry_summary(entries):
    closed = [e for e in entries if e['net_usdt'] is not None]
    return {'entries': len(entries), 'closed_original_cycles': len(closed),
            'original_net_winners': sum(e['net_usdt'] > 0 for e in closed),
            'original_net_losers': sum(e['net_usdt'] < 0 for e in closed),
            'original_net_zero': sum(e['net_usdt'] == 0 for e in closed),
            'original_net_usdt': sum((e['net_usdt'] for e in closed), ZERO)}


def admission_filter(reference_entries, actual_entries, masks, candidate_bit, coverage_bit):
    reference = {(e['entry_ns'], e['long_venue']): e for e in reference_entries}
    actual = {(e['entry_ns'], e['long_venue']): e for e in actual_entries}
    require(len(reference) == len(reference_entries) and len(actual) == len(actual_entries), 'duplicate entry identity')
    blocked, missing_feature, threshold_rejected, passed_missing, matched = [], [], [], [], []
    for key, entry in reference.items():
        mask = masks[entry['entry_ns']]
        allowed = bool(mask & (1 << candidate_bit))
        covered = bool(mask & (1 << coverage_bit))
        require(not allowed or covered, 'candidate permits entry without its required coverage')
        if not allowed:
            blocked.append(entry)
            (threshold_rejected if covered else missing_feature).append(entry)
            require(key not in actual, 'actual entry violates its gate')
        elif key not in actual:
            passed_missing.append(entry)
        if key in actual:
            other = actual[key]
            matched.append({'entry_ns': key[0], 'long_venue': key[1], 'reference_cycle': entry['cycle'],
                            'actual_cycle': other['cycle'], 'reference_qty': entry['qty'], 'actual_qty': other['qty'],
                            'reference_net_usdt': entry['net_usdt'], 'actual_net_usdt': other['net_usdt']})
    for entry in actual_entries:
        require(masks[entry['entry_ns']] & (1 << candidate_bit), 'actual entry gate check')
    new = [entry for key, entry in actual.items() if key not in reference]
    return {'reference_entries': len(reference), 'actual_entries': len(actual),
            'blocked': entry_summary(blocked), 'blocked_missing_required_feature': entry_summary(missing_feature),
            'blocked_threshold_given_coverage': entry_summary(threshold_rejected),
            'passed_but_not_actual': entry_summary(passed_missing), 'matched_entries': len(matched),
            'matched_with_different_qty': sum(e['reference_qty'] != e['actual_qty'] for e in matched),
            'new_actual_entries': len(new), 'blocked_details': blocked, 'matched_details': matched,
            'new_actual_details': new, 'causal_caveat': CAVEAT,
            'matching_identity': 'Exact entry_ns and long venue; different quantities are reported separately.'}


def median(values):
    values = sorted(values)
    if not values:
        return None
    n = len(values)
    return values[n//2] if n % 2 else (values[n//2-1]+values[n//2])/2


def entry_characteristics(records):
    count = len(records)
    notional = sum((r['entry_long_notional_usdt'] for r in records), ZERO)
    net = sum((r['net_before_funding_usdt'] for r in records), ZERO)
    stops = [r for r in records if r['exit_reason'] in ('stop_loss', 'hard_stop_loss')]
    return {'mean_net_usdt_per_cycle': net/count if count else None,
            'mean_entry_long_notional_usdt': notional/count if count else None,
            'net_bps_on_sum_entry_long_notional': net/notional*10000 if notional else None,
            'median_holding_seconds': median([r['holding_seconds'] for r in records]),
            'mean_holding_seconds': sum((r['holding_seconds'] for r in records), ZERO)/count if count else None,
            'median_stop_holding_seconds': median([r['holding_seconds'] for r in stops]),
            'stop_loss_fraction': D(len(stops))/count if count else None,
            'mean_entry_liquidation_cost_bps': sum((r['entry_liquidation_cost_bps'] for r in records), ZERO)/count if count else None,
            'median_entry_liquidation_cost_bps': median([r['entry_liquidation_cost_bps'] for r in records]),
            'max_entry_liquidation_cost_bps': max((r['entry_liquidation_cost_bps'] for r in records), default=None),
            'mean_entry_within_spread_sum_bps': sum((r['entry_within_spread_sum_bps'] for r in records), ZERO)/count if count else None,
            'single_tick_move_available': False,
            'path_limit': 'Entry/exit records do not identify maximum single-tick move, intermediate favorable excursion, or future take-profit after an exit.'}


def decomposition_summary(records):
    keys = ('mid_basis_convergence_usdt', 'bid_ask_crossing_cost_usdt', 'gross_usdt', 'fees_usdt', 'net_before_funding_usdt')
    result = {key: sum((r[key] for r in records), ZERO) for key in keys}
    require(result['mid_basis_convergence_usdt']-result['bid_ask_crossing_cost_usdt']-result['fees_usdt'] == result['net_before_funding_usdt'], 'exact summed decomposition')
    result.update(cycles=len(records), converging_cycles=sum(r['mid_basis_convergence_usdt'] > 0 for r in records),
                  convergence_but_net_loss=sum(r['mid_basis_convergence_usdt'] > 0 and r['net_before_funding_usdt'] < 0 for r in records))
    return result


def control_comparison(candidate, control, covered_seconds):
    cbps, bbps = candidate['closed_net_bps_on_sum_entry_long_notional'], control['closed_net_bps_on_sum_entry_long_notional']
    return {'evidence_status': 'NO_REQUIRED_FEATURE_COVERAGE' if covered_seconds == 0 else ('NO_TRADES' if candidate['cycles'] == 0 else 'DESCRIPTIVE_PATH_COMPARISON'),
            'net_difference_usdt': candidate['net_before_funding_usdt']-control['net_before_funding_usdt'],
            'cycle_difference': candidate['cycles']-control['cycles'],
            'net_bps_difference': cbps-bbps if cbps is not None and bbps is not None else None,
            'supports_threshold_improvement': False,
            'interpretation': 'No automatic causal or promotion claim. Compare the corresponding coverage-only account path, not baseline alone; selection remains frozen.'}


def checked_day(root, manifest, protocol, day, phase, hashes):
    folder = root/phase/day
    paths = {key: folder/key for key in ('aggregate.json', 'perfect-audit.json', 'perfect-deep-audit.json', 'perfect-deep-quotes.json')}
    aggregate, light, deep, quotes = (load(paths[key]) for key in paths)
    ids = [p['id'] for p in protocol['policies']]
    aggregate_sha = sha(paths['aggregate.json'])
    require(aggregate['execution_enabled'] is False and light['status'] == deep['status'] == quotes['status'] == 'PASS', 'all result audits must pass')
    require(light['aggregate_sha256'] == deep['aggregate_sha256'] == quotes['aggregate_sha256'] == aggregate_sha, 'stale aggregate audit')
    require(deep['light_audit_sha256'] == sha(paths['perfect-audit.json']) and deep['source_entry_exit_quotes_sha256'] == sha(paths['perfect-deep-quotes.json']), 'stale quote/light audit')
    require(deep['input_manifest_sha256'] == sha(root/'input-manifest.json'), 'audit input manifest binding')
    require([s['id'] for s in aggregate['scenarios']] == ids and set(deep['audited_ids']) == set(ids), 'policy/audit set mismatch')
    require(quotes['side_order'] == SIDE_ORDER, 'quote layout mismatch')
    files = manifest['days'][day]['files']
    for key in ('tape', 'activity', 'thresholds', 'funding_csv', 'entry_gates'):
        require(deep['source_sha256'][key] == files[key]['sha256'], 'deep source differs: '+key)
    require(quotes['tape_sha256'] == files['tape']['sha256'], 'quote/tape binding')
    feature_audit_path = root/'features'/day/'independent-feature-audit.json'
    feature_audit = load(feature_audit_path)
    require(feature_audit['status'] == 'PASS' and feature_audit['input_manifest_sha256'] == sha(root/'input-manifest.json')
            and feature_audit['protocol_sha256'] == sha(root/'protocol.json'), 'feature audit binding')
    feature_path = Path(manifest['days'][day]['feature_evidence']['feature_npz']['path'])
    require(feature_audit['feature_file_sha256'] == sha(feature_path)
            and feature_audit['gate_file_sha256'] == sha(files['entry_gates']['path']) == files['entry_gates']['sha256'], 'feature/gate audit current hash')
    start = manifest['days'][day]['entry_start_ns']
    end = manifest['days'][day]['end_ns_exclusive']-120*10**9
    with np.load(feature_path, allow_pickle=False) as arrays:
        coverage = feature_time_coverage(arrays, start, end)
    require(coverage['total_seconds'] == 43080, 'predeclared noon to 23:58 coverage window')
    quote_map = {(q['id'], q['cycle']): q for q in quotes['cycles']}
    require(len(quote_map) == len(quotes['cycles']), 'duplicate source quotes')
    all_records, all_entries, result_rows, used_quotes = [], {}, [], set()
    for policy, scenario in zip(protocol['policies'], aggregate['scenarios']):
        pid = policy['id']
        for key in ('entry_bps', 'tp_bps', 'sl_bps', 'hard_bps', 'hold_ms'):
            require(scenario[key] == policy[key], 'unplanned trading parameter change')
        p = {key: folder/pid/(key+'.csv') for key in ('orders', 'cycles', 'equity')}
        for key, path in p.items():
            digest = sha(path)
            require(light['scenarios'][pid]['source_sha256'][key+'.csv'] == digest, 'ledger changed since audit')
            hashes[str(path)] = digest
        orders, cycles = read_csv(p['orders']), read_csv(p['cycles'])
        groups, records = defaultdict(list), []
        for order in orders:
            groups[int(order['cycle'])].append(order)
        for cycle in cycles:
            num = int(cycle['cycle'])
            record = cycle_record(day, pid, cycle, groups[num])
            q = quote_map[pid, num]
            require((pid, num) not in used_quotes, 'duplicate closed cycle')
            used_quotes.add((pid, num))
            require(all(int(cycle[k]) == q[k] for k in ('entry_ns', 'exit_ns', 'qty')) and q['long_venue'] == cycle['long_venue'], 'source quote identity')
            require(q['price_decimals'] == aggregate['price_decimals'], 'source quote price scale')
            unit = D(10)**-q['price_decimals']
            identity = decompose([D(v)*unit for v in q['entry_prices']], [D(v)*unit for v in q['exit_prices']], q['qty'], q['long_venue'])
            for key, target in [('gross_pnl', 'gross_usdt'), ('fees', 'fees_usdt'), ('net', 'net_before_funding_usdt')]:
                require(identity[key] == record[target], 'ledger/source midpoint identity')
            record.update(phase=phase, mid_basis_convergence_usdt=identity['mid_basis_convergence'],
                          bid_ask_crossing_cost_usdt=identity['bid_ask_crossing_cost'])
            records.append(record)
        values = totals(records)
        require(len(records) == scenario['cycles'] and len(orders) == scenario['orders'], 'scenario ledger counts')
        require(values['gross_usdt'] == D(scenario['gross_pnl']) and values['net_before_funding_usdt'] == D(scenario['closed_net_before_funding']), 'scenario realized amounts')
        require(sum((D(o['fee']) for o in orders), ZERO) == D(scenario['fees']), 'scenario fees')
        require(values['net_wins'] == scenario['wins'] and values['net_losses'] == scenario['losses'], 'scenario wins/losses')
        lookup = {r['cycle']: r for r in records}
        entries = []
        for number, group in groups.items():
            opened = [o for o in group if o['intent'] == 'entry']
            require(len(opened) == 2 and len({o['qty'] for o in opened}) == len({o['ns'] for o in opened}) == 1, 'entry pair identity')
            lng = next(o for o in opened if o['side'] == 'buy')
            entries.append({'cycle': number, 'entry_ns': int(lng['ns']), 'long_venue': lng['venue'], 'qty': int(lng['qty']),
                            'net_usdt': lookup[number]['net_before_funding_usdt'] if number in lookup else None})
        all_entries[pid] = entries
        control = coverage_control(policy)
        row = {'date': day, 'phase': phase, 'policy_id': pid, 'role': policy['role'], 'coverage_control': control,
               'cycles': scenario['cycles'], 'orders': scenario['orders'], 'wins': scenario['wins'], 'losses': scenario['losses'],
               'gross_usdt': D(scenario['gross_pnl']), 'fees_usdt': D(scenario['fees']), 'net_before_funding_usdt': D(scenario['net_before_funding']),
               'closed_net_before_funding_usdt': D(scenario['closed_net_before_funding']),
               'liquidation_max_drawdown_usdt': D(scenario['complete_mark_max_drawdown_usdt']),
               'bybit_cash': D(scenario['cash']['bybit']), 'bitget_cash': D(scenario['cash']['bitget']),
               'unresolved_qty': scenario['unresolved_qty'], 'funding_status': light['scenarios'][pid]['funding_status'],
               'exit_reasons': scenario['exit_reasons'], 'halt_reason': scenario['halt_reason'],
               'loss_cap_hit': scenario['halt_reason'] == 'total_loss_cap',
               'sum_closed_entry_long_notional_usdt': values['entry_long_notional_usdt'],
               'closed_net_bps_on_sum_entry_long_notional': values['net_bps_on_sum_entry_long_notional'],
               'required_feature_covered_seconds': coverage[control]['allowed_seconds'], 'time_denominator_seconds': coverage['total_seconds'],
               'exit_depth_assumed_cycles': sum(r['exit_depth_assumed'] for r in records),
               'exit_depth_assumed_fraction': D(sum(r['exit_depth_assumed'] for r in records))/len(records) if records else None,
               'exit_depth_assumed_net_usdt': sum((r['net_before_funding_usdt'] for r in records if r['exit_depth_assumed']), ZERO),
               'exit_below_min_orders': scenario['exit_below_min_orders'], 'total_exit_excess_depth_qty': scenario['total_exit_excess_depth_qty'],
               'characteristics': entry_characteristics(records), 'decomposition': decomposition_summary(records),
               'exit_reason_attribution': {reason: {**totals([r for r in records if r['exit_reason'] == reason]),
                                                    **entry_characteristics([r for r in records if r['exit_reason'] == reason])}
                                           for reason in sorted(scenario['exit_reasons'])}}
        all_records.extend(records)
        result_rows.append(row)
    require(used_quotes == set(quote_map), 'unaccounted source quotes')
    masks = masks_at_entries(files['entry_gates']['path'], len(ids), {e['entry_ns'] for entries in all_entries.values() for e in entries})
    rows_by_id = {r['policy_id']: r for r in result_rows}
    admission = {}
    for i, policy in enumerate(protocol['policies']):
        pid, control = policy['id'], coverage_control(policy)
        row = rows_by_id[pid]
        row['relative_to_coverage_control'] = control_comparison(row, rows_by_id[control], row['required_feature_covered_seconds'])
        admission[pid] = {'baseline': admission_filter(all_entries['baseline'], all_entries[pid], masks, i, ids.index(control)),
                          'coverage_control_id': control,
                          'coverage_control': admission_filter(all_entries[control], all_entries[pid], masks, i, ids.index(control))}
    for path in [*paths.values(), feature_audit_path, feature_path, Path(files['entry_gates']['path'])]:
        hashes[str(path)] = sha(path)
    return result_rows, all_records, coverage, admission


def fmt(value, places=4):
    return '—' if value is None else f'{D(value):.{places}f}'


def make_report(comparison, attribution):
    lines = ['# 波动率与冷却比率门槛：盈亏原因及覆盖对照', '',
             f"冻结选择：**{comparison['selected_id']}**。所有新增候选均未满足两拟合日各至少10组的晋级条件。10月所有候选是预先声明的诊断，不据其结果改默认参数；三个日期此前已研究，不是真正未见样本。", '',
             '固定交易参数：入场30bp、止盈净20bp、相对入场反向清算基准恶化10bp止损、硬止损100bp、最长15秒，四笔各3bp手续费；0新增延迟，当前对手一档理想全额开平。每所每天1000U，日期间账户重置。', '',
             '新增指标有两类：①近300秒内、非重叠15秒跨所中价基差变化的波动尺度，除以当前两所点差及四笔手续费的往返成本代理；②两所各自300秒/3600秒的一秒中价收益波动比，取较小者。两者都必须达到冻结有效覆盖要求，且只控制新开仓，不阻止原仓退出。它们表示尺度或相对活跃程度，不直接预测价差会回归。', '',
             '**净额 = 中价基差有利变化收益 − 开平盘口半价差成本 − 手续费。** 分解逐组合与源L1精确核对。中价只作事后归因。bp分母为已闭合组合入场做多单腿名义金额之和。', '',
             '## 各日结果', '',
             '| 日期 | 策略 | 对应覆盖控制 | 组合/赢 | 毛利 | 费用 | 净额 | 净bp | 回撤 | 相对覆盖控制净额差 |',
             '|---|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for r in comparison['rows']:
        delta = r['relative_to_coverage_control']['net_difference_usdt']
        lines.append(f"| {r['date']} | {r['policy_id']} | {r['coverage_control']} | {r['cycles']}/{r['wins']} | {fmt(r['gross_usdt'])} | {fmt(r['fees_usdt'])} | {fmt(r['net_before_funding_usdt'])} | {fmt(r['closed_net_bps_on_sum_entry_long_notional'],2)} | {fmt(r['liquidation_max_drawdown_usdt'])} | {fmt(delta)} |")
    by_key = {(r['date'],r['policy_id']):r for r in comparison['rows']}
    aug_base, aug_cost = by_key['2026-08-01','baseline'], by_key['2026-08-01','cost_030']
    oct_base, oct_cost = by_key['2026-10-01','baseline'], by_key['2026-10-01','cost_030']
    lines += ['',
              f"**8月总额减亏没有变成更好的单位名义收益。** cost_030净{fmt(aug_cost['net_before_funding_usdt'])}U，基准{fmt(aug_base['net_before_funding_usdt'])}U；但单位单腿名义净额由基准{fmt(aug_base['closed_net_bps_on_sum_entry_long_notional'],2)}bp变为{fmt(aug_cost['closed_net_bps_on_sum_entry_long_notional'],2)}bp。持仓中位数由{fmt(aug_base['characteristics']['median_holding_seconds'],3)}秒变为{fmt(aug_cost['characteristics']['median_holding_seconds'],3)}秒。对应覆盖控制结果列于表中，不能只与未过滤基准比较。", '',
              f"**10月cost_030对理想执行假设敏感。** 它净{fmt(oct_cost['net_before_funding_usdt'])}U、单位名义{fmt(oct_cost['closed_net_bps_on_sum_entry_long_notional'],2)}bp（基准{fmt(oct_base['closed_net_bps_on_sum_entry_long_notional'],2)}bp），持仓中位只有{fmt(oct_cost['characteristics']['median_holding_seconds']*1000,1)}毫秒；这不能证明真实网络、撤改单与深度变化后仍能捕获。该观察为事后诊断，不用于更换冻结baseline。", '']
    lines += ['', '## 有效特征覆盖', '',
              '12:00≤UTC<23:58，共43080个一秒格点。以下仅表示滚动指标可计算；不含数值门槛、q25活动、资金费、现金或持仓约束。原生事件通过数不作时间比例。', '',
              '| 日期 | 基差波动有效秒 | 冷却比率有效秒 | 联合有效秒 |', '|---|---:|---:|---:|']
    for day, c in comparison['feature_time_coverage'].items():
        lines.append(f"| {day} | {c['basis_coverage']['allowed_seconds']}/43080 | {c['cooling_coverage']['allowed_seconds']}/43080 | {c['joint_coverage']['allowed_seconds']}/43080 |")
    lines += ['', '9月午后 basis_valid 和 cooling_valid 均为0，新增滚动指标未达到有效盘口覆盖要求。因此新增策略零交易属于缺乏可用特征，不是提高阈值后改善策略，也不能直接断言原始档案缺失。', '',
              '## 交易质量与退出原因', '',
              '减少总亏损须与对应覆盖控制及每组/单位名义损益同时比较。下表聚焦预先固定的中档门槛，不据表重新挑选策略。', '',
              '| 日期 | 策略 | 单组净U | 单组名义U | 净bp | 持仓中位秒 | 止损比例 | 止损持仓中位秒 | 入场即平成本均bp | 两所内点差均bp |',
              '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    focus = {'baseline','basis_coverage','cooling_coverage','joint_coverage','cost_030','cool_100','cost_030_cool_100'}
    for r in comparison['rows']:
        if r['policy_id'] in focus and r['cycles']:
            c = r['characteristics']
            lines.append(f"| {r['date']} | {r['policy_id']} | {fmt(c['mean_net_usdt_per_cycle'])} | {fmt(c['mean_entry_long_notional_usdt'],2)} | {fmt(c['net_bps_on_sum_entry_long_notional'],2)} | {fmt(c['median_holding_seconds'],3)} | {fmt(c['stop_loss_fraction']*100,1)}% | {fmt(c['median_stop_holding_seconds'],3)} | {fmt(c['mean_entry_liquidation_cost_bps'],2)} | {fmt(c['mean_entry_within_spread_sum_bps'],2)} |")
    lines += ['', '| 日期 | 策略 | 退出原因 | 组/赢 | 中价变化收益 | 盘口成本 | 手续费 | 净额 |', '|---|---|---|---:|---:|---:|---:|---:|']
    for r in comparison['rows']:
        if r['policy_id'] in focus:
            for reason, v in r['exit_reason_attribution'].items():
                records = [x for x in attribution['cycles'] if x['date'] == r['date'] and x['policy_id'] == r['policy_id'] and x['exit_reason'] == reason]
                d = decomposition_summary(records)
                lines.append(f"| {r['date']} | {r['policy_id']} | {reason} | {v['cycles']}/{v['net_wins']} | {fmt(d['mid_basis_convergence_usdt'])} | {fmt(d['bid_ask_crossing_cost_usdt'])} | {fmt(v['fees_usdt'])} | {fmt(v['net_before_funding_usdt'])} |")
    lines += ['', '止盈触发与净赢单独统计在attribution.json；“超时净亏”不说明永久不回归，短时止损也不能单凭开平记录证明是噪声。当前记录不能计算最大单tick变化或退出后可能出现的止盈，不能对此编造因果解释。', '',
              '## 基准原交易被门槛拒绝', '',
              '下列盈亏仍属于基准旧路径，不能叫避免亏损。对应覆盖控制原交易的同样检查另存comparison.json，区分数值门槛与特征覆盖；匹配为同纳秒、同做多交易所，仓位差别单独记录。', '',
              '| 日期 | 策略 | 被挡基准/原赢/原亏 | 原净额 | 其中无特征/阈值拒绝 | 匹配/新增实际入场 |', '|---|---|---:|---:|---:|---:|']
    for day, policies in comparison['entry_admission'].items():
        for pid, data in policies.items():
            if pid == 'baseline':
                continue
            a, b = data['baseline'], data['baseline']['blocked']
            lines.append(f"| {day} | {pid} | {b['entries']}/{b['original_net_winners']}/{b['original_net_losers']} | {fmt(b['original_net_usdt'])} | {a['blocked_missing_required_feature']['entries']}/{a['blocked_threshold_given_coverage']['entries']} | {a['matched_entries']}/{a['new_actual_entries']} |")
    lines += ['', '## 下一轮方向（尚未验证，不更改本轮参数）', '']
    for i, item in enumerate(attribution['next_directions'],1):
        lines.append(f"{i}. **{item['direction']}**：{item['evidence']} 待验证：{item['test']}")
    lines += ['', '## 结果边界', '',
              '- 总额、每组、每名义金额与回撤的口径不同；零交易的bp为不可计算，不写成0bp。过滤器改变连续账户路径，不能将被挡原交易净额直接加到新结果。',
              '- 每组中价变化只描述实际持仓期间的两端差值；波动尺度和短长波动比都不是价差回归概率或预期利润。',
              '- 各日手续费、现金、资金费核验、未闭合仓位及超显示深度退出数量与损益见comparison.json。超显示深度仍全额按L1退出是理想假设。',
              '- 本报告要求逐单、原生时点/盘口、资金费和新增特征审计PASS；审计证明计算符合冻结规则，不证明实盘可获利。', '']
    return '\n'.join(lines)


def run(experiment):
    root = Path(experiment).resolve()
    outputs = [root/name for name in ('comparison.json','comparison.csv','attribution.json','report.md')]
    require(not any(p.exists() for p in outputs), 'output already exists; refusing overwrite')
    protocol, frozen, manifest = (load(root/name) for name in ('protocol.json','selection-frozen.json','input-manifest.json'))
    require(frozen['evaluation_date_results_consumed'] is False and frozen['evaluation_all_predeclared_policies_diagnostic'] is True, 'selection must precede evaluation analysis')
    require(frozen['selected_id'] == 'baseline', 'this report expects frozen baseline retention; do not silently reselect')
    for key, name in [('protocol_sha256','protocol.json'),('policies_sha256','policies.csv'),('input_manifest_sha256','input-manifest.json')]:
        require(frozen[key] == sha(root/name), 'frozen binding changed: '+key)
    for path, digest in frozen['source_sha256'].items():
        require(sha(path) == digest, 'frozen fit source changed')
    acceptance = load(root/'feature-gate-acceptance.json')
    require(acceptance['status'] == 'PASS' and acceptance['input_manifest_sha256'] == sha(root/'input-manifest.json'), 'feature acceptance absent/stale')
    policies_csv = read_csv(root/'policies.csv')
    require([p['id'] for p in policies_csv] == [p['id'] for p in protocol['policies']], 'policy CSV order')
    hashes = {str(root/name): sha(root/name) for name in ('protocol.json','policies.csv','selection-frozen.json','input-manifest.json','feature-gate-acceptance.json')}
    for key in ('execution_code_sha256','feature_code_sha256'):
        for path, digest in manifest[key].items():
            require(sha(path) == digest, 'frozen code changed')
            hashes[path] = digest
    rows, records, coverage, admissions = [], [], {}, {}
    for day in [*protocol['fit_dates'], protocol['evaluation_date']]:
        phase = 'search' if day in protocol['fit_dates'] else 'evaluation'
        r, cycles, cov, admission = checked_day(root, manifest, protocol, day, phase, hashes)
        rows.extend(r); records.extend(cycles); coverage[day] = cov; admissions[day] = admission
    source_files = [Path(__file__), OLD_CODE/'attribution.py', OLD_CODE/'microstructure_attribution.py', OLD_CODE/'run_day.py']
    code_hashes = {str(path): sha(path) for path in source_files}
    by_policy = {}
    for policy in protocol['policies']:
        sample = [r for r in records if r['policy_id'] == policy['id']]
        by_policy[policy['id']] = {**diagnostic_policy(sample), 'decomposition': decomposition_summary(sample), 'characteristics': entry_characteristics(sample)}
    no_coverage = [r for r in rows if r['role'] == 'candidate' and r['required_feature_covered_seconds'] == 0]
    aug = {r['policy_id']: r for r in rows if r['date'] == protocol['fit_dates'][0]}
    cost, control = aug['cost_030'], aug['basis_coverage']
    next_directions = [
        {'direction': '先验证滚动指标覆盖，再扩大到新的日期',
         'evidence': f"{len(no_coverage)}个候选/日期组合缺乏午后所需特征覆盖，不能提供阈值优劣证据；冻结晋级规则保留baseline。",
         'test': '在未研究过的日期保留相同新鲜度、segment和样本量要求，报告有效时段；若要改变覆盖定义，单独预注册控制实验，不因零交易静默放宽。'},
        {'direction': '将波动率门槛与清算成本、单位名义损益分开评估',
         'evidence': f"首拟合日cost_030为{cost['cycles']}组、净{fmt(cost['net_before_funding_usdt'])}U、{fmt(cost['closed_net_bps_on_sum_entry_long_notional'],2)}bp；对应basis_coverage为{control['cycles']}组、净{fmt(control['net_before_funding_usdt'])}U、{fmt(control['closed_net_bps_on_sum_entry_long_notional'],2)}bp。总额减亏不足以证明单笔优势。",
         'test': '另行固定少量max_entry_liquidation_cost_bps候选，使用当前反向报价与3bp手续费，比较覆盖匹配控制的单位名义净额、手续费吞噬和新日期稳定性。'},
        {'direction': '对短时止损先记录回归进展，再测试进展超时',
         'evidence': f"首拟合日cost_030止损比例{fmt(cost['characteristics']['stop_loss_fraction']*100 if cost['characteristics']['stop_loss_fraction'] is not None else None,1)}%，止损持仓中位{fmt(cost['characteristics']['median_stop_holding_seconds'],3)}秒；逐原因净额及入场成本已拆开。",
         'test': '先在固定持仓内记录中价基差、可执行净值的最大有利/不利进展与单tick变化，再预设进展检查时点。当前数据不证明波动筛选造成噪声止损，也不支持直接延长止损或持仓。'},
        {'direction': '恢复退出深度与实际撮合摩擦作为独立压力测试',
         'evidence': f"baseline三日共有{sum(r['exit_depth_assumed_cycles'] for r in rows if r['policy_id']=='baseline')}组退出量超过当时至少一所一档显示量，当前仍假设全额L1成交。",
         'test': '使用入场时可见反向深度作预先限制，随后比较延迟、部分退出和剩余风险；不得用未来退出深度反向筛选入场，也不把现有理想损益视为实盘收益。'}]
    common = {'status':'ATTRIBUTION_READY','execution_enabled':False,'selected_id':frozen['selected_id'],
              'selection_status':frozen['status'],'all_evaluation_candidates_diagnostic':True,
              'dates_previously_studied':True,'source_sha256':hashes,'code_sha256':code_hashes}
    comparison = {**common,'policies':protocol['policies'],'rows':rows,'feature_time_coverage':coverage,'entry_admission':admissions,
                  'comparison_scope':CAVEAT,'no_required_feature_coverage_is_improvement':False}
    attribution = {**common,'exact_identity':'net = midpoint basis convergence - four half-spread crossing costs - actual four-fill fees',
                   'identity_verified_cycles':len(records),'by_policy':by_policy,'cycles':records,'next_directions':next_directions}
    report = make_report(comparison, attribution)
    with outputs[0].open('x') as stream:
        json.dump(comparison,stream,ensure_ascii=False,indent=2,default=str);stream.write('\n')
    with outputs[1].open('x',newline='') as stream:
        flat_rows = []
        for r in rows:
            flat = {k:v for k,v in r.items() if not isinstance(v,dict)}
            flat.update({'mid_basis_convergence_usdt':r['decomposition']['mid_basis_convergence_usdt'],
                         'bid_ask_crossing_cost_usdt':r['decomposition']['bid_ask_crossing_cost_usdt'],
                         **r['characteristics'],**r['relative_to_coverage_control']})
            flat_rows.append(flat)
        writer=csv.DictWriter(stream,fieldnames=list(flat_rows[0]));writer.writeheader();writer.writerows(flat_rows)
    with outputs[2].open('x') as stream:
        json.dump(attribution,stream,ensure_ascii=False,indent=2,default=str);stream.write('\n')
    with outputs[3].open('x') as stream:
        stream.write(report)
    return {'status':'ATTRIBUTION_READY','rows':len(rows),'verified_cycles':len(records),
            'outputs':{str(p):sha(p) for p in outputs},'code_sha256':code_hashes}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment',required=True)
    args=parser.parse_args()
    print(json.dumps(run(args.experiment),ensure_ascii=False,indent=2))
