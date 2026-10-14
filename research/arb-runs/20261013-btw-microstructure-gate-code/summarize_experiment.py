"""Read-only audited attribution of predeclared microstructure entry gates.

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

from apply_scope import validate as validate_scope

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
    return policy['control_id']


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


def feature_native_coverage(index_path, start_ns, end_ns):
    index = load(index_path)
    result = {'start_ns_inclusive':start_ns, 'end_ns_exclusive':end_ns, 'total_observations':0,
              'native_valid_observations':0,
              'scope':'Native decision event counts, NOT clock-time coverage. Independent per-venue stream warmup and positive60s rate denominator; no thresholds, q25 or account gates.'}
    counts = {k:0 for k in ('baseline','book_coverage','trade_coverage','flow_coverage')}
    total_rows=0;previous=-1
    for chunk in index['chunks']:
        require(sha(chunk['path']) == chunk['sha256'], 'native feature chunk changed')
        with np.load(chunk['path'], allow_pickle=False) as z:
            ns=z['ns'];require(len(ns)==chunk['rows'] and np.all(ns[1:]>ns[:-1]) and int(ns[0])>previous,'native feature ordering')
            previous=int(ns[-1]);total_rows+=len(ns)
            selected=(ns>=start_ns)&(ns<end_ns)
            count=int(selected.sum());result['total_observations']+=count
            result['native_valid_observations']+=int((selected&z['native_valid']).sum())
            book=z['book_valid'];trade=z['trade_valid']
            for key,mask in [('baseline',np.ones(len(ns),dtype=bool)),('book_coverage',book),('trade_coverage',trade),('flow_coverage',book&trade)]:
                counts[key]+=int((selected&mask).sum())
    require(total_rows==index['rows'], 'native feature total count')
    for key,n in counts.items():
        result[key]={'allowed_observations':n,'fraction_of_native_observations':D(n)/result['total_observations'] if result['total_observations'] else None}
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


def control_comparison(candidate, control, covered_observations):
    cbps, bbps = candidate['closed_net_bps_on_sum_entry_long_notional'], control['closed_net_bps_on_sum_entry_long_notional']
    return {'evidence_status': 'NO_REQUIRED_FEATURE_COVERAGE' if covered_observations == 0 else ('NO_TRADES' if candidate['cycles'] == 0 else 'DESCRIPTIVE_PATH_COMPARISON'),
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
    feature_path = Path(manifest['days'][day]['feature_evidence']['feature_index']['path'])
    require(feature_audit['feature_index_sha256'] == sha(feature_path)
            and feature_audit['gate_file_sha256'] == sha(files['entry_gates']['path']) == files['entry_gates']['sha256'], 'feature/gate audit current hash')
    start = manifest['days'][day]['entry_start_ns']
    end = manifest['days'][day]['end_ns_exclusive']-120*10**9
    coverage = feature_native_coverage(feature_path, start, end)
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
               'required_feature_covered_observations': coverage[control]['allowed_observations'], 'native_event_denominator': coverage['total_observations'],
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
        row['relative_to_coverage_control'] = control_comparison(row, rows_by_id[control], row['required_feature_covered_observations'])
        admission[pid] = {'baseline': admission_filter(all_entries['baseline'], all_entries[pid], masks, i, ids.index(control)),
                          'coverage_control_id': control,
                          'coverage_control': admission_filter(all_entries[control], all_entries[pid], masks, i, ids.index(control))}
    for path in [*paths.values(), feature_audit_path, feature_path, Path(files['entry_gates']['path'])]:
        hashes[str(path)] = sha(path)
    return result_rows, all_records, coverage, admission


def fmt(value, places=4):
    return '—' if value is None else f'{D(value):.{places}f}'


def make_report(comparison, attribution):
    lines=['# 盘口与成交事件过滤回放', '',
           f"冻结选择：**{comparison['selected_id']}**；状态：{comparison['selection_status']}。先用8月1日与9月1日选参，再检查10月1日；三个日期均已研究过，只是内部探索比较。", '',
           '本轮关闭新增V/R波动率门槛。BTW、原q25绝对成交活跃度、入场30bp、净止盈20bp、相对入场即时反向清算基准恶化10bp止损、硬止损100bp、15秒持仓上限、30秒冷却、每日10U损失限额均不变。每所每日1000U，单腿最多250U；每笔手续费0.03%。', '',
           'book：各所过去5秒L2增量价位行更新速率/过去60秒速率，分别达标。trade：各所真实成交笔数的同类速率比。均按12×N5/N60计算，至少完整60秒连续窗口且分母大于0。book的快照和断线重置，trade按自己的恢复/断线重置。', '',
           '按用户最新要求，价差持续时间不作为入场条件；本报告仅保留持续时间门槛为0的10组（基准、3个覆盖对照、6个速率候选）。此前已跑完的4个持续时间组封存并排除。更正在结果已经可见之后记录于scope-amendment.json，没有改写原冻结协议或原账本；剩余候选仅用原8月/9月数据复核，选择仍为baseline。', '',
           'L2是聚合价位数据，不含逐订单ID：更新行数不能称为新委托数或撤单数。各venue单独归一不能消除频道聚合差异。没有价格波动率计算或预测。', '',
           '## 每日结果', '',
           '|日期|过滤器|组合/赢|净U|单腿成交名义净bp|最大回撤U|相对覆盖对照净额差U|',
           '|---|---|---:|---:|---:|---:|---:|']
    for r in comparison['rows']:
        lines.append(f"|{r['date']}|{r['policy_id']}|{r['cycles']}/{r['wins']}|{fmt(r['net_before_funding_usdt'])}|{fmt(r['closed_net_bps_on_sum_entry_long_notional'],2)}|{fmt(r['liquidation_max_drawdown_usdt'])}|{fmt(r['relative_to_coverage_control']['net_difference_usdt'])}|")
    lines += ['', '净bp分母为已闭合组合入场做多单腿名义金额之和，不是账户收益率。各日重置本金，各参数情景不能累计成同一账户业绩。','',
              '## 盈亏分解与退出原因','',
              '逐组精确核对：净额=中价基差收敛收益−开平买卖盘半价差成本−四笔手续费。中价仅用于事后归因，成交价格仍是对手一档。','',
              '|日期|过滤器|收敛收益U|盘口成本U|费用U|止盈/止损/超时|持仓中位毫秒|','|---|---|---:|---:|---:|---|---:|']
    for r in comparison['rows']:
        if r['role']=='coverage_control':continue
        x=r['decomposition'];ex=r['exit_reasons'];h=r['characteristics']['median_holding_seconds']
        stops=ex.get('stop_loss',0)+ex.get('hard_stop_loss',0)
        lines.append(f"|{r['date']}|{r['policy_id']}|{fmt(x['mid_basis_convergence_usdt'])}|{fmt(x['bid_ask_crossing_cost_usdt'])}|{fmt(r['fees_usdt'])}|{ex.get('take_profit',0)}/{stops}/{ex.get('holding_timeout',0)}|{fmt(h*1000 if h is not None else None,1)}|")
    lines += ['', '所有退出原因的分组损益在comparison.json，逐组合原盘口分解在attribution.json。止盈次数与净盈利次数分开统计；止损并不自动等于噪声，也不能据开平两端值判断退出后是否会回归。','',
              '## 过滤的原基准交易','',
              '以下是旧基准路径上的盈利/亏损，不是新策略实际避免的亏损。门槛会改变冷却、仓位、现金及每日停机时点，需同时看新出现的入场。','',
              '|日期|过滤器|挡住原盈利/原亏损|原净额U|因缺特征/因阈值|新增实际入场|','|---|---|---:|---:|---|---:|']
    for day,policies in comparison['entry_admission'].items():
        for pid,v in policies.items():
            if pid=='baseline':continue
            x=v['baseline'];b=x['blocked']
            lines.append(f"|{day}|{pid}|{b['original_net_winners']}/{b['original_net_losers']}|{fmt(b['original_net_usdt'])}|{x['blocked_missing_required_feature']['entries']}/{x['blocked_threshold_given_coverage']['entries']}|{x['new_actual_entries']}|")
    lines += ['', '## 有效覆盖','', '覆盖统计按原生事件次数，不称为时间占比。覆盖对照只要求可计算，缺数不当成低活跃。','',
              '|日期|原生决策数|盘口速率有效|成交速率有效|两者有效|','|---|---:|---:|---:|---:|']
    for day,c in comparison['feature_native_coverage'].items():
        lines.append(f"|{day}|{c['total_observations']}|{c['book_coverage']['allowed_observations']}|{c['trade_coverage']['allowed_observations']}|{c['flow_coverage']['allowed_observations']}|")
    lines += ['', '## 后续验证方向（本轮未实施）','']
    for x in attribution['next_directions']:lines.append('- '+x)
    lines += ['', '## 边界','', '- 本轮仍采用零新增网络延迟、双腿全额一档撮合，退出量可超过显示深度。有效策略没有价差持续等待。', '- 阈值过滤没有证明可稳定获利；活跃强度只代表发生得快，不等于订单流支持套利方向。', '- 真实新挂/撤单速率需要逐订单数据，本地L2档案无法提供；未补造订单级事件。', '- 开平触发、资金费、费用和现金按原引擎核验，没有真实订单。','']
    return '\n'.join(lines)


def run(experiment):
    root = Path(experiment).resolve()
    outputs = [root/name for name in ('comparison.json','comparison.csv','attribution.json','report.md')]
    require(not any(p.exists() for p in outputs), 'output already exists; refusing overwrite')
    protocol, frozen, manifest = (load(root/name) for name in ('protocol.json','selection-frozen.json','input-manifest.json'))
    amendment = load(root/'scope-amendment.json')
    active_policies, excluded_policies = validate_scope(root, amendment)
    active_ids = {p['id'] for p in active_policies}
    require(amendment['selected_id'] == frozen['selected_id'], 'scope changed selection; separate review required')
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
    hashes = {str(root/name): sha(root/name) for name in ('protocol.json','policies.csv','selection-frozen.json','input-manifest.json','feature-gate-acceptance.json','scope-amendment.json')}
    for key in ('execution_code_sha256','feature_code_sha256'):
        for path, digest in manifest[key].items():
            require(sha(path) == digest, 'frozen code changed')
            hashes[path] = digest
    rows, records, coverage, admissions = [], [], {}, {}
    for day in [*protocol['fit_dates'], protocol['evaluation_date']]:
        phase = 'search' if day in protocol['fit_dates'] else 'evaluation'
        r, cycles, cov, admission = checked_day(root, manifest, protocol, day, phase, hashes)
        rows.extend(r); records.extend(cycles); coverage[day] = cov; admissions[day] = admission
    archived_counts = {'scenarios':len(rows), 'cycles':len(records)}
    rows = [r for r in rows if r['policy_id'] in active_ids]
    records = [r for r in records if r['policy_id'] in active_ids]
    admissions = {day:{pid:v for pid,v in values.items() if pid in active_ids} for day,values in admissions.items()}
    source_files = [Path(__file__), Path(__file__).with_name('apply_scope.py'), Path(__file__).with_name('selection.py'), OLD_CODE/'attribution.py', OLD_CODE/'microstructure_attribution.py', OLD_CODE/'run_day.py']
    code_hashes = {str(path): sha(path) for path in source_files}
    by_policy = {}
    for policy in active_policies:
        sample = [r for r in records if r['policy_id'] == policy['id']]
        by_policy[policy['id']] = {**diagnostic_policy(sample), 'decomposition': decomposition_summary(sample), 'characteristics': entry_characteristics(sample)}
    no_coverage = [r for r in rows if r['role'] == 'candidate' and r['required_feature_covered_observations'] == 0]
    next_directions = [
        '先看拟合日与评估日是否同向改善、单位名义净额是否提高；若只是减少交易或缺数停机，不晋级参数。',
        '盘口更新快但成交不跟进时，可能只是报价频繁变化；下一轮可固定少量成交/盘口事件比阈值，保持交易规则不变，独立检验。',
        '若频率条件仍不能区分亏损，可进一步检验与既定套利方向一致的L1 OFI或主动成交压力；它们不是本轮已经验证的指标。',
        '理想快速开平收益仍需单独接受延迟、部分成交与退出深度压力测试；不把本轮结果当作真实可实现收益。']
    common = {'status':'ATTRIBUTION_READY','execution_enabled':False,'selected_id':amendment['selected_id'],
              'selection_status':frozen['status'],'all_evaluation_candidates_diagnostic':True,
              'dates_previously_studied':True,'source_sha256':hashes,'code_sha256':code_hashes,
              'spread_persistence_entry_filter_enabled':False,'volatility_entry_filters_enabled':False,
              'excluded_archived_policy_ids':[p['id'] for p in excluded_policies],
              'original_archive_counts':archived_counts,
              'scope_amendment_after_results_available':True}
    comparison = {**common,'policies':active_policies,'rows':rows,'feature_native_coverage':coverage,'entry_admission':admissions,
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
