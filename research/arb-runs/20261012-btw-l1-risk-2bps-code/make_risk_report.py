"""Report a completed, triply audited BTW gross TP2/SL2 L1 replay.

Only small result artifacts are read; this script does not access a ledger,
network, API credentials, or invoke replay/audit execution. --self-test exercises
report calculation and rejection gates using the verified old result as a
fixture source; it never changes either real replay directory.
"""
from __future__ import annotations

import argparse
import copy
import csv
import importlib.util
import json
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

BASE = Path('/Users/mori/code/crypto/state/arb-runs')
RUN = BASE / '20261012-btw-l1-tp2-gross-sl2-gross-20261001'
BASELINE = BASE / '20261009-btw-l1-replay-20261001'
SHARED = BASE / '20261009-btw-l1-replay-code/make_report.py'
spec = importlib.util.spec_from_file_location('verified_l1_report_helpers', SHARED)
shared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared)
require, sha, equal, num = shared.require, shared.sha, shared.equal, shared.num
RISK_BASIS = 'remaining_long_entry_notional'
PNL_BASIS = {k: 'remaining_pair_gross_price_pnl' for k in ('take_profit', 'stop_loss')}
VENUES = ('bybit', 'bitget')


def validate_risk(metadata, risk, metadata_sha):
    require(risk['status'] == 'PASS' and risk['run_status'] == 'COMPLETED' and not risk['failures'],
            'risk trigger audit did not pass for a completed run')
    require(risk['run_metadata_sha256'] == metadata_sha, 'risk audit metadata changed')
    require(metadata['risk_basis'] == risk['risk_basis'] == RISK_BASIS, 'wrong risk notional basis')
    require(metadata['risk_pnl_basis'] == risk['risk_pnl_basis'] == PNL_BASIS, 'wrong gross risk PnL basis')
    require(risk['source_code_sha256'] == metadata['source_code_sha256'], 'risk source hashes mismatch')
    expected = {'runner.py': metadata['runner_sha256'], 'paper_snapshot.py': metadata['risk_snapshot_sha256'],
                'risk_policy.py': metadata['risk_policy_sha256'], 'tardis_adapter.py': metadata['adapter_sha256']}
    actual = {Path(k).name: v for k, v in risk['execution_code_sha256'].items()}
    require(actual == expected, 'risk execution hashes mismatch')
    policy = metadata['policy']
    require(D(policy['take_profit_bps']) == D(policy['stop_loss_bps']) == 2, 'not the requested 2bp/2bp run')
    require('stop_loss_usdt' not in policy, 'legacy fixed cash stop still present')
    require(all(D(policy[v + '_fee_bps']) == 3 for v in VENUES), 'actual fees must stay 3bp')
    counts = Counter(t['reason'] for t in risk['triggers'])
    require(set(counts) <= {'take_profit', 'stop_loss'}, 'unexpected trigger reason')
    require(all(counts[k] == risk['trigger_counts'].get(k, 0) for k in ('take_profit', 'stop_loss')),
            'risk trigger counts differ from detail')


def enrich_metrics(data):
    """Add order-derived turnover and net losing-cycle diagnostics."""
    root, metrics, cycles = data['root'], data['metrics'], data['cycles']
    with (root / 'orders.csv').open(newline='') as handle:
        orders = list(csv.DictReader(handle))
    require(len(orders) == metrics['orders'], 'orders CSV count mismatch')
    known = {c['cycle_id'] for c in cycles}
    counts, fees, notionals = Counter(), defaultdict(lambda: D(0)), defaultdict(lambda: D(0))
    for row in orders:
        require(row['cycle_id'] in known and row['venue'] in VENUES, 'unknown order cycle/venue')
        require(row['symbol'] == 'BTWUSDT' and row['pair'] == 'BTW/USDT:USDT', 'wrong order instrument')
        require(row['status'] == 'FILLED' and row['mode'] == 'ideal_signal_fill', 'unexpected fill mode')
        require(D(row['fee_rate']) == D('.0003'), 'wrong actual fill fee')
        require(equal(D(row['qty']) * D(row['price']), row['notional']), 'wrong fill notional')
        require(equal(D(row['notional']) * D(row['fee_rate']), row['fee']), 'wrong actual fee amount')
        counts[row['cycle_id']] += 1
        fees[row['cycle_id']] += D(row['fee'])
        notionals[row['venue']] += D(row['notional'])
    for cycle in cycles:
        require(counts[cycle['cycle_id']] == cycle['orders'], 'cycle order count differs from audit')
        require(equal(fees[cycle['cycle_id']], cycle['fees']), 'cycle fees differ from orders')
    turnover = sum(notionals.values(), D(0))
    metrics['fill_notional_usdt'] = turnover
    metrics['two_sided_turnover_pct_on_2000'] = turnover / 20
    metrics['half_turnover_pct_on_2000'] = turnover / 40
    metrics['turnover_by_venue'] = dict(notionals)
    losses = [c for c in cycles if D(c['net_before_funding']) < 0]
    wins = [c for c in cycles if D(c['net_before_funding']) > 0]
    worst = min(losses, key=lambda c: D(c['net_before_funding'])) if losses else None
    metrics['loss_analysis'] = {
        'cycles': len(losses), 'loss_rate_pct': D(100) * len(losses) / len(cycles) if cycles else None,
        'gross_usdt': sum((D(c['gross_pnl']) for c in losses), D(0)),
        'fees_usdt': sum((D(c['fees']) for c in losses), D(0)),
        'net_usdt': sum((D(c['net_before_funding']) for c in losses), D(0)),
        'mean_net_usdt': sum((D(c['net_before_funding']) for c in losses), D(0)) / len(losses) if losses else None,
        'gross_positive_net_negative': sum(D(c['gross_pnl']) > 0 for c in losses),
        'gross_zero_net_negative': sum(D(c['gross_pnl']) == 0 for c in losses),
        'gross_negative': sum(D(c['gross_pnl']) < 0 for c in losses),
        'winning_net_usdt': sum((D(c['net_before_funding']) for c in wins), D(0)),
        'worst_cycle_id': worst['cycle_id'] if worst else None,
        'worst_net_usdt': D(worst['net_before_funding']) if worst else None,
        'holding_seconds': shared.distribution([D(c['holding_seconds']) for c in losses]),
    }
    for reason, group in metrics['exit_reason_groups'].items():
        rows = [c for c in cycles if c['exit_reason'] == reason]
        group['losses'] = sum(D(c['net_before_funding']) < 0 for c in rows)
        group['flat'] = group['cycles'] - group['wins'] - group['losses']
        group['loss_net_usdt'] = sum((D(c['net_before_funding']) for c in rows if D(c['net_before_funding']) < 0), D(0))
        group['exit_orders'] = 2 * sum(c['exit_parts'] for c in rows)
    data['input_sha256']['orders.csv'] = sha(root / 'orders.csv')
    return data


def load_verified(root, *, new):
    data = shared.load_verified(root, l1=True)
    funding = json.loads((data['root'] / 'funding-audit.json').read_text())
    require(funding['status'] == 'VERIFIED_NO_SETTLEMENT' and not funding['crossings']
            and not funding['unresolved'] and D(funding['funding_usdt']) == 0,
            'funding audit is not verified without settlement')
    data['input_sha256']['funding-audit.json'] = sha(data['root'] / 'funding-audit.json')
    if new:
        risk = json.loads((data['root'] / 'risk-audit.json').read_text())
        validate_risk(data['metadata'], risk, sha(data['root'] / 'run_metadata.json'))
        data['risk'] = risk
        data['input_sha256']['risk-audit.json'] = sha(data['root'] / 'risk-audit.json')
    return enrich_metrics(data)


def compare(new, old):
    nm, om = new['metadata'], old['metadata']
    for key in ('source_archive', 'source_evidence_sha256', 'rules', 'depth_sizing',
                'day_end_entry_cutoff_ms', 'initial_balance_per_venue', 'history_gate',
                'quote_model', 'execution_book_levels', 'budget_observation_levels',
                'replay_start_ms', 'replay_end_exclusive_ms'):
        require(nm[key] == om[key], 'unexpected comparison change: ' + key)
    risk_keys = {'take_profit_bps', 'stop_loss_bps', 'stop_loss_usdt'}
    require({k: v for k, v in nm['policy'].items() if k not in risk_keys} ==
            {k: v for k, v in om['policy'].items() if k not in risk_keys}, 'non-risk policy changed')
    require(D(om['policy']['take_profit_bps']) == 1 and D(om['policy']['stop_loss_usdt']) == 5,
            'baseline is not original net TP1bp / net SL5 USDT')
    fields = ('cycles', 'orders', 'gross_usdt', 'fees_usdt', 'net_usdt', 'return_pct_on_2000',
              'two_sided_turnover_pct_on_2000')
    return {
        'schema_version': 1, 'generated_utc': datetime.now(timezone.utc).isoformat(),
        'generator_sha256': sha(__file__), 'shared_helper_sha256': sha(SHARED),
        'comparison_kind': 'same_native_L1_replay_changed_pair_risk_triggers',
        'changes': ['TP: paired net >= long-entry N * 1bp -> paired gross >= remaining long-entry N * 2bp',
                    'SL: paired net <= -5 USDT -> paired gross <= -remaining long-entry N * 2bp',
                    'Actual 3bp per-fill fees remain debited; TP/SL labels are first trigger reasons, not final net outcomes.'],
        'baseline': {'directory': str(old['root']), 'input_sha256': old['input_sha256'], 'metrics': old['metrics']},
        'gross_2bp': {'directory': str(new['root']), 'input_sha256': new['input_sha256'], 'metrics': new['metrics'],
                      'risk_trigger_counts': new['risk']['trigger_counts']},
        'delta_new_minus_baseline': {k: new['metrics'][k] - old['metrics'][k] for k in fields},
        'risk_note': '2bp is a trigger, not a gross or net loss cap. Drawdown omits incomplete liquidation marks.',
        'turnover_basis': 'All filled entry+exit notionals on both venues divided by initial total cash 2000 USDT.',
    }


def render(new, old, comp):
    n, b, root = new['metrics'], old['metrics'], new['root']
    lines = ['# BTW 对手一档回放：毛止盈 2bp / 毛止损 2bp', '',
        '回放范围：2026-10-01 00:00–24:00 UTC（北京时间10月1日08:00至10月2日08:00），Bybit / Bitget BTWUSDT。新旧两轮均已完成；本轮账务、原始一档成交、风险触发三项独立验收均为 PASS，无残余持仓，资金费为 `VERIFIED_NO_SETTLEMENT`。', '',
        f'本轮完成 **{n["cycles"]} 组、{n["orders"]} 条模拟订单**；价差毛损益 {num(n["gross_usdt"])} USDT，手续费 {num(n["fees_usdt"])} USDT，实际资金费0，净损益 **{num(n["net_usdt"])} USDT**。盈利 {n["wins"]} 组、亏损 {n["losses"]} 组、持平 {n["flat"]} 组。', '',
        '按本轮最终设置，止盈和止损均不扣除手续费再判断。单腿基准沿用**剩余多头开仓名义额**：`N = 剩余 BTW 数量 × 多头原始开仓价`；双腿预计毛价格损益 `G = q × (多头退出买一价 − 多头开仓价 + 空头开仓价 − 空头退出卖一价)`。`G ≥ N × 2 / 10000` 触发止盈，`G ≤ −N × 2 / 10000` 触发止损。分母不是双腿合计名义额，也不是账户本金。', '',
        '**手续费仍按每笔成交名义额的 0.03%（3bp）正常扣除。** 例如单腿 N=250 USDT 时，两条毛触发线约为 ±0.05 USDT；完整四笔往返费用约0.30 USDT。恰在毛止盈线上退出仍可能净亏约0.25 USDT，实际费用取决于各笔模拟成交名义额。止盈标签不保证扣费后盈利。', '',
        '| 指标 | 原一档：净止盈1bp / 净止损5U | 本轮：毛止盈2bp / 毛止损2bp |', '|---|---:|---:|']
    for label, key, places in (
        ('完整组合数', 'cycles', 0), ('模拟订单数', 'orders', 0), ('毛损益 USDT', 'gross_usdt', 6),
        ('手续费 USDT', 'fees_usdt', 6), ('净损益 USDT', 'net_usdt', 6), ('总本金收益率 %', 'return_pct_on_2000', 6),
        ('盈利组合', 'wins', 0), ('亏损组合', 'losses', 0), ('盈利比例 %', 'win_rate_pct', 3),
        ('成交名义金额合计 USDT', 'fill_notional_usdt', 6), ('双边换手率 %', 'two_sided_turnover_pct_on_2000', 3),
        ('单边折半换手率 %', 'half_turnover_pct_on_2000', 3), ('分批退出组合', 'partial_exit_cycles', 0),
        ('持仓不足1秒的组合', 'holding_under_1_second', 0)):
        lines.append(f'| {label} | {num(b[key], places)} | {num(n[key], places)} |')
    for label, metric in (('持仓秒数', 'holding_seconds'), ('每腿开仓名义额 USDT', 'entry_notional_usdt_per_leg')):
        for name, key in (('平均', 'mean'), ('中位', 'median'), ('P95', 'p95'), ('最大', 'max')):
            lines.append(f'| {label}：{name} | {num(b[metric][key])} | {num(n[metric][key])} |')
    lines += ['', f'净损益变化（本轮减原一档）：{num(comp["delta_new_minus_baseline"]["net_usdt"])} USDT。双边换手率 = 所有开平仓成交名义金额之和 / 2,000 USDT；单边口径取其一半。持仓按原始接收纳秒差统计，P95采用最近秩法。', '',
        '| 账户 | 毛损益 USDT | 手续费 USDT | 净损益 USDT | 最终现金 USDT |', '|---|---:|---:|---:|---:|']
    for venue in VENUES:
        v = n['venues'][venue]
        lines.append(f'| {venue} | {num(v["gross_usdt"])} | {num(v["fees_usdt"])} | {num(v["net_usdt"])} | {num(v["final_cash_usdt"])} |')
    lines += ['', '退出原因是首次触发原因，后续分批平仓沿用该标签。分类统计如下：', '',
        '| 运行 | 退出原因 | 组合 | 盈利 | 亏损 | 平仓订单 | 毛损益 | 手续费 | 净损益 USDT |',
        '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for name, data in (('原一档', b), ('本轮', n)):
        for reason, group in data['exit_reason_groups'].items():
            lines.append(f'| {name} | {reason} | {group["cycles"]} | {group["wins"]} | {group["losses"]} | {group["exit_orders"]} | {num(group["gross_usdt"])} | {num(group["fees_usdt"])} | {num(group["net_usdt"])} |')
    loss = n['loss_analysis']
    tp = n['exit_reason_groups'].get('take_profit', {})
    lines += ['', f'本轮亏损组合 {loss["cycles"]} 组，占 {num(loss["loss_rate_pct"], 3)}%；亏损组毛损益合计 {num(loss["gross_usdt"])}、费用 {num(loss["fees_usdt"])}、净损益 {num(loss["net_usdt"])} USDT，平均每组 {num(loss["mean_net_usdt"])} USDT，最差单组 {num(loss["worst_net_usdt"])} USDT。其中毛亏 {loss["gross_negative"]} 组、毛持平扣费后亏损 {loss["gross_zero_net_negative"]} 组、毛盈利扣费后亏损 {loss["gross_positive_net_negative"]} 组。', '',
        f'止盈触发组中，扣费后亏损 {tp.get("losses", 0)} 组；这与毛止盈设定并不矛盾，也可能叠加后续分批退出期间报价变化。止损2bp是触发线，盘口跳变、触发到退出、预算不足和费用都可能让最终毛亏或净亏超过2bp。', '',
        '| 行情与信号 | 原一档 | 本轮 |', '|---|---:|---:|']
    for label, key in (('Bybit实时逐笔成交', 'trade_records:bybit:trade'), ('Bitget实时逐笔成交', 'trade_records:bitget:trade'),
                       ('Bitget首包/重连历史成交（另计）', 'trade_records:bitget:historical_trade')):
        lines.append(f'| {label} | {b["raw_counts"].get(key, 0):,} | {n["raw_counts"].get(key, 0):,} |')
    for label, key in (('盘口决策次数', 'book_decisions'), ('方向信号评估数', 'signal_evaluations')):
        lines.append(f'| {label} | {b[key]:,} | {n[key]:,} |')
    for label, key, places in (('达到5bp净入场门槛', 'cost_threshold_passed', 0), ('同时通过深度/费用/历史门槛', 'eligible', 0),
                                ('实际开仓', 'entered', 0), ('最大可计算原始价差 bp', 'max_gross', 6), ('最大扣成本入场边际 bp', 'max_net', 6)):
        lines.append(f'| {label} | {num(b["signal_statistics"][key], places)} | {num(n["signal_statistics"][key], places)} |')
    lines += ['', '一个持续机会可被重复评估；上述方向评估数不是独立机会数。最大价差只统计深度允许计算数量的方向。逐笔成交已解析计数；可执行价格仍来自重建的一档盘口，不以成交价代替盘口报价。', '',
        '| 估值与运行 | 原一档 | 本轮 |', '|---|---:|---:|']
    for label, key, places in (('完整平仓估值时点', 'complete_liquidation_marks', 0), ('缺失平仓估值时点', 'incomplete_liquidation_marks', 0),
                                ('缺失率 %（含空仓时点）', 'incomplete_liquidation_pct', 3), ('可估值点最大回撤 USDT', 'max_drawdown_usdt', 6)):
        lines.append(f'| {label} | {num(b["equity_metrics"][key], places)} | {num(n["equity_metrics"][key], places)} |')
    lines += [f'| 预检至导出完成墙钟耗时 分钟 | {num(D(str(b["wall_elapsed_seconds"])) / 60, 2)} | {num(D(str(n["wall_elapsed_seconds"])) / 60, 2)} |', '',
        f'本轮停止新增原因：`{n["halt_reason"] or "无累计亏损停机"}`。完整平仓估值缺失时不填零、不沿用旧值；最大回撤可能低估路径风险。墙钟耗时不含随后的独立验收和报告生成。', '',
        '其余执行设置保持：两所各1,000 USDT，单腿上限250 USDT、1倍、两腿同币数；买入ask1、卖出bid1；仅一档显示量25%的剩余预算可成交，top50持续记忆已消耗预算。入场仍扣四笔往返费用12bp、退出价差预留2bp和资金费预留2bp，净门槛5bp，对应原始报价价差至少21bp。', '',
        '历史准入仍按每个时点可用的已完成20个UTC日平均波动率前十名，BTW本日排名第三；没有历史成交额硬下限。新旧回放使用同一历史门槛、行情档案与规则。60秒持仓触发退出、全平后30秒冷却、完整组合闭合后累计净亏损达到10 USDT停止新增，资金费结算前保护和23:58 UTC日末退出保持原设置。触发后退出原因固定，受预算限制可以分批且超过60秒。', '',
        '资金费逐腿按实际持仓区间独立核验，未跨结算，实际资金费0；预留2bp并非实际账单。行情失效、无足够可执行片段时不会制造旧价成交；不足以一次退出时用可执行片段一档价外推剩余仓位风险估计，实际数量继续受两腿共同预算限制。', '',
        '本轮是两腿按可见一档同时理想成交的模拟，未建模网络/报单延迟、单腿失败、异步成交敞口、真实队列、市场冲击或mark price强平。历史合约约束使用10月9日快照情景，20日选币池与事后挑选BTW存在研究偏差；单日结果不能证明实盘收益。', '',
        '每组损益（时间 UTC；数量为每腿初始 BTW）：', '',
        '| 组 | 开仓 | 最后平仓 | 多头 / 空头 | BTW数量 | 退出批数 | 原因 | 毛损益 | 费用 | 净损益 USDT |',
        '|---:|---|---|---|---:|---:|---|---:|---:|---:|']
    for i, cycle in enumerate(new['cycles'], 1):
        row = new['csv_by_id'][cycle['cycle_id']]
        lines.append(f'| {i} | {shared.stamp(cycle["entry_ns"])} | {shared.stamp(cycle["last_exit_ns"])} | {row["long_venue"]} / {row["short_venue"]} | {row["qty"]} | {cycle["exit_parts"]} | {cycle["exit_reason"]} | {num(cycle["gross_pnl"])} | {num(cycle["fees"])} | {num(cycle["net_before_funding"])} |')
    if not new['cycles']:
        lines.append('| — | — | — | — | — | — | 零交易 | 0 | 0 | 0 |')
    files = [('orders.csv', '全部订单'), ('cycles.csv', '组合损益'), ('signals.csv.gz', '方向信号'), ('equity.csv.gz', '估值'),
             ('summary.json', '汇总'), ('run_metadata.json', '参数来源'), ('acceptance.json', '账务验收'),
             ('depth-fill-audit.json', '一档成交验收'), ('risk-audit.json', '风险触发验收'), ('funding-audit.json', '资金费核验'),
             ('comparison.json', '比较统计与哈希')]
    lines += ['', '本轮记录：' + ' · '.join(shared.link(root, name, label) for name, label in files), '',
              '旧一档基准：' + shared.link(old['root'], 'report.md', '原报告') + ' · ' + shared.link(old['root'], 'cycles.csv', '原组合')]
    return '\n'.join(lines) + '\n'


def main(root=RUN, baseline=BASELINE):
    require(Path(root).resolve() != Path(baseline).resolve(), 'refusing to overwrite baseline report')
    new, old = load_verified(root, new=True), load_verified(baseline, new=False)
    comparison = compare(new, old)
    report = render(new, old, comparison)
    for name, content in [('comparison.json', json.dumps(comparison, default=str, ensure_ascii=False, indent=2) + '\n'), ('report.md', report)]:
        path = new['root'] / name
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_text(content)
        tmp.replace(path)
    print(json.dumps({'report': str(new['root'] / 'report.md'), 'comparison': str(new['root'] / 'comparison.json'),
                      'cycles': new['metrics']['cycles'], 'net_usdt': str(new['metrics']['net_usdt']), 'audits': 'PASS'}, ensure_ascii=False))


def self_test():
    old = load_verified(BASELINE, new=False)
    require(old['metrics']['losses'] == 98, 'old loss count regression')
    require(equal(old['metrics']['fill_notional_usdt'], '12341.33950'), 'old turnover regression')
    require(equal(old['metrics']['loss_analysis']['net_usdt'], '-2.977552780'), 'old loss net regression')
    metadata = copy.deepcopy(old['metadata'])
    metadata['risk_basis'], metadata['risk_pnl_basis'] = RISK_BASIS, PNL_BASIS.copy()
    metadata['policy']['take_profit_bps'] = metadata['policy']['stop_loss_bps'] = '2'
    metadata['policy'].pop('stop_loss_usdt')
    metadata['risk_snapshot_sha256'] = 'fixture-snapshot'
    metadata['risk_policy_sha256'] = 'fixture-policy'
    risk = {'status': 'PASS', 'run_status': 'COMPLETED', 'failures': [], 'run_metadata_sha256': 'fixture',
            'risk_basis': RISK_BASIS, 'risk_pnl_basis': PNL_BASIS.copy(), 'source_code_sha256': metadata['source_code_sha256'],
            'execution_code_sha256': {'runner.py': metadata['runner_sha256'], 'paper_snapshot.py': 'fixture-snapshot',
                                      'risk_policy.py': 'fixture-policy', 'tardis_adapter.py': metadata['adapter_sha256']},
            'trigger_counts': {'take_profit': 0, 'stop_loss': 0}, 'triggers': []}
    validate_risk(metadata, risk, 'fixture')
    checks = 4
    for label, obj, key, value in [('running', risk, 'run_status', 'RUNNING'), ('failed', risk, 'status', 'FAIL'),
                                 ('failure-list', risk, 'failures', ['bad']), ('hash', risk, 'run_metadata_sha256', 'different'),
                                 ('notional', metadata, 'risk_basis', 'pair_total'), ('net-tp', metadata, 'risk_pnl_basis', {'take_profit': 'net', 'stop_loss': 'gross'})]:
        mutated_meta, mutated_risk = copy.deepcopy(metadata), copy.deepcopy(risk)
        target = mutated_meta if obj is metadata else mutated_risk
        target[key] = value
        try:
            validate_risk(mutated_meta, mutated_risk, 'fixture')
        except RuntimeError:
            checks += 1
        else:
            raise AssertionError('did not reject ' + label)
    new = copy.deepcopy(old)
    new['metadata'], new['risk'] = metadata, risk
    compare(new, old)
    require('止盈标签不保证扣费后盈利' in render(new, old, compare(new, old)), 'gross/net caveat missing')
    checks += 2
    with tempfile.TemporaryDirectory(prefix='risk-report-selftest-') as name:
        root = Path(name)
        (root / 'status.json').write_text('{"status":"RUNNING"}')
        for fname in ('summary.json', 'run_metadata.json', 'acceptance.json', 'depth-fill-audit.json'):
            (root / fname).write_bytes((BASELINE / fname).read_bytes())
        try:
            main(root, BASELINE)
        except RuntimeError:
            checks += 1
        else:
            raise AssertionError('running run accepted')
        require(not (root / 'report.md').exists() and not (root / 'comparison.json').exists(), 'report written before verification')
        checks += 1
    print(json.dumps({'self_test': 'PASS', 'checks': checks, 'verified_baseline_cycles': old['metrics']['cycles'],
                      'fixture_only': True, 'real_replay_outputs_changed': False}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, default=RUN)
    parser.add_argument('--baseline', type=Path, default=BASELINE)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    self_test() if args.self_test else main(args.run, args.baseline)
