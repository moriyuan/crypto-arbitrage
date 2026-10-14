"""Render only completed, independently verified BTW L1 and baseline results.

Read small final artifacts; never open a ledger, invoke a replay, or use a network.
Run after accounting AND native-depth audits finish. Decimal metrics serialize as
strings. Holding P95 uses nearest rank; missing liquidation marks are not filled.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

BASE = Path('/Users/mori/code/crypto/state/arb-runs')
RUN = BASE / '20261009-btw-l1-replay-20261001'
BASELINE = BASE / '20261009-btw-tardis-replay-20261001'
VENUES = ('bybit', 'bitget')
TOL = D('1e-18')


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def equal(a, b):
    return abs(D(str(a)) - D(str(b))) <= TOL


def distribution(values):
    values = sorted(values)
    if not values:
        return {'count': 0, 'min': None, 'mean': None, 'median': None, 'p95': None, 'max': None}
    return {'count': len(values), 'min': values[0], 'mean': sum(values, D(0)) / len(values),
            'median': statistics.median(values), 'p95': values[math.ceil(len(values) * .95) - 1],
            'max': values[-1]}


def load_verified(root, *, l1):
    root = Path(root).resolve()
    names = ('status.json', 'summary.json', 'run_metadata.json', 'acceptance.json', 'depth-fill-audit.json')
    docs = {name: json.loads((root / name).read_text()) for name in names}
    status, summary, metadata, audit, depth = (docs[name] for name in names)
    require(status['status'] == summary['status'] == audit['run_status'] == 'COMPLETED',
            str(root) + ': replay must be COMPLETED before report generation')
    require(audit['audit_status'] == 'PASS' and not audit['failures'], 'accounting audit did not pass')
    require(depth['status'] == 'PASS', 'native depth-fill audit did not pass')
    require(depth['run_status'] == 'COMPLETED', 'depth audit is not for a completed replay')
    require(not audit['residual_positions'] and summary['cycle'] is None, 'unresolved position')
    require(audit['funding_status'] == summary['funding_status'] == 'VERIFIED_NO_SETTLEMENT',
            'funding is not verified; cannot label final net return')
    require(not audit['funding_crossings'], 'funding settlement crossing remains')
    require(metadata['symbol'] == summary['symbol'] == 'BTWUSDT', 'unexpected symbol')
    require(metadata['requested_day_utc'] == '2026-10-01', 'unexpected replay day')
    require(metadata['execution_enabled'] is False and metadata['prediction_model_used'] is False,
            'unexpected execution/prediction mode')
    require(depth['run_metadata_sha256'] == sha(root / 'run_metadata.json'), 'depth audit metadata changed')
    require(D(metadata['policy']['price_guard_bps']) == (0 if l1 else 1), 'unexpected price guard')
    if l1:
        require(metadata['quote_model'] == 'counterparty_best' and metadata['execution_book_levels'] == 1
                and metadata['budget_observation_levels'] == 50, 'unexpected L1 execution contract')
    with (root / 'cycles.csv').open(newline='') as handle:
        csv_cycles = list(csv.DictReader(handle))
    by_id = {row['cycle_id']: row for row in csv_cycles}
    cycles = audit['cycles']
    require(len(by_id) == len(csv_cycles) == len(cycles) == summary['completed_cycles'], 'cycle count mismatch')
    require(set(by_id) == {row['cycle_id'] for row in cycles}, 'cycle identity mismatch')
    for cycle in cycles:
        row = by_id[cycle['cycle_id']]
        require(cycle['closed'], 'unclosed audit cycle')
        require(row['symbol'] == 'BTWUSDT' and row['pair'] == 'BTW/USDT:USDT', 'wrong cycle instrument')
        require({row['long_venue'], row['short_venue']} == set(VENUES), 'wrong venues')
        for csv_key, audit_key in (('gross_pnl', 'gross_pnl'), ('total_fees', 'fees'),
                                   ('net_before_funding', 'net_before_funding')):
            require(equal(row[csv_key], cycle[audit_key]), 'cycle value differs from accounting audit')
        require(int(row['exit_parts']) == cycle['exit_parts'] and row['exit_reason'] == cycle['exit_reason'],
                'exit classification mismatch')
        require(cycle['orders'] == 2 + 2 * cycle['exit_parts'], 'paired partial-exit order count mismatch')
        require(all(equal(row['qty'], cycle['legs'][v]['entry_qty']) for v in VENUES), 'quantity mismatch')
    require(summary['orders'] == audit['ledger_counts']['orders'] == depth['orders_verified'],
            'order count mismatch between final results and audits')
    metrics = derive_metrics(summary, metadata, audit, cycles, by_id)
    files = names + ('cycles.csv',)
    return {'root': root, 'summary': summary, 'metadata': metadata, 'audit': audit, 'depth': depth,
            'cycles': cycles, 'csv_by_id': by_id, 'metrics': metrics,
            'input_sha256': {name: sha(root / name) for name in files}}


def derive_metrics(summary, metadata, audit, cycles, by_id):
    gross = sum((D(c['gross_pnl']) for c in cycles), D(0))
    fees = sum((D(c['fees']) for c in cycles), D(0))
    net = gross - fees
    require(equal(net, audit['cumulative_net_before_funding']) and equal(net, summary['net_after_funding']),
            'final net mismatch')
    venue_metrics = {}
    for venue in VENUES:
        vg = sum((D(c['legs'][venue]['gross_pnl']) for c in cycles), D(0))
        vf = sum((D(c['legs'][venue]['fees']) for c in cycles), D(0))
        cash = D(audit['final_cash'][venue])
        require(equal(D(1000) + vg - vf, cash) and equal(cash, summary['cash'][venue]), 'final cash mismatch')
        venue_metrics[venue] = {'gross_usdt': vg, 'fees_usdt': vf, 'funding_usdt': D(0),
                                'net_usdt': vg - vf, 'final_cash_usdt': cash}
    holdings = [D(c['holding_seconds']) for c in cycles]
    qty = [D(by_id[c['cycle_id']]['qty']) for c in cycles]
    notionals = [D(row['qty']) * D(row[side + '_entry_price'])
                 for row in by_id.values() for side in ('long', 'short')]
    n = len(cycles)
    wins = sum(D(c['net_before_funding']) > 0 for c in cycles)
    losses = sum(D(c['net_before_funding']) < 0 for c in cycles)
    groups = defaultdict(list)
    for c in cycles:
        groups[c['exit_reason']].append(c)
    grouped = {}
    for reason, rows in sorted(groups.items()):
        grouped[reason] = {'cycles': len(rows), 'wins': sum(D(c['net_before_funding']) > 0 for c in rows),
                           'gross_usdt': sum((D(c['gross_pnl']) for c in rows), D(0)),
                           'fees_usdt': sum((D(c['fees']) for c in rows), D(0)),
                           'net_usdt': sum((D(c['net_before_funding']) for c in rows), D(0)),
                           'holding_seconds': distribution([D(c['holding_seconds']) for c in rows])}
    risk = dict(audit['equity_metrics'])
    require(risk['complete_liquidation_marks'] + risk['incomplete_liquidation_marks'] == risk['paired_ticks'],
            'liquidation mark counts inconsistent')
    risk['incomplete_liquidation_pct'] = (D(100) * risk['incomplete_liquidation_marks'] / risk['paired_ticks']
                                        if risk['paired_ticks'] else None)
    return {'cycles': n, 'orders': summary['orders'], 'gross_usdt': gross, 'fees_usdt': fees,
            'funding_usdt': D(0), 'net_usdt': net, 'return_pct_on_2000': net / 20,
            'wins': wins, 'losses': losses, 'flat': n - wins - losses,
            'win_rate_pct': D(100) * wins / n if n else None,
            'holding_seconds': distribution(holdings), 'holding_under_1_second': sum(h < 1 for h in holdings),
            'entry_qty_btw_per_leg': distribution(qty), 'entry_notional_usdt_per_leg': distribution(notionals),
            'partial_exit_cycles': sum(c['exit_parts'] > 1 for c in cycles),
            'exit_reason_groups': grouped, 'venues': venue_metrics, 'equity_metrics': risk,
            'signal_evaluations': summary['signal_evaluations'], 'book_decisions': summary['ticks'],
            'signal_statistics': audit['signal_statistics'], 'signal_reasons': audit['signal_reasons'],
            'raw_counts': summary['raw_counts'], 'wall_elapsed_seconds': summary['wall_elapsed_seconds'],
            'funding_status': audit['funding_status'], 'halt_reason': summary['halt_reason']}


def comparison(new, old):
    nm, om = new['metadata'], old['metadata']
    require(nm['source_archive'] == om['source_archive'] and nm['source_evidence_sha256'] == om['source_evidence_sha256'],
            'comparison source archives differ')
    require(nm['history_gate']['sha256'] == om['history_gate']['sha256'], 'history gates differ')
    for key in ('rules', 'depth_sizing', 'day_end_entry_cutoff_ms', 'initial_balance_per_venue'):
        require(nm[key] == om[key], 'unexpected comparison parameter change: ' + key)
    require({k: v for k, v in nm['policy'].items() if k != 'price_guard_bps'} ==
            {k: v for k, v in om['policy'].items() if k != 'price_guard_bps'}, 'other policy parameters changed')
    fields = ('cycles', 'orders', 'gross_usdt', 'fees_usdt', 'net_usdt', 'return_pct_on_2000')
    return {'schema_version': 1, 'generated_utc': datetime.now(timezone.utc).isoformat(),
            'generator_sha256': sha(Path(__file__)), 'comparison_kind': 'two_changes_not_depth_only_control',
            'changes': ['entry/exit/executable risk quotes: 10bp multi-level VWAP -> opposite best only',
                        'price_guard_bps: 1 -> 0; min_entry_net_bps stays 5'],
            'baseline': {'directory': str(old['root']), 'input_sha256': old['input_sha256'], 'metrics': old['metrics']},
            'l1': {'directory': str(new['root']), 'input_sha256': new['input_sha256'], 'metrics': new['metrics']},
            'delta_l1_minus_baseline': {key: new['metrics'][key] - old['metrics'][key] for key in fields},
            'risk_metric_note': 'Drawdown uses paired complete liquidation marks only; missing marks excluded, so risk may be understated.',
            'percentile_method': 'Nearest rank; holdings measured using native receive ns, not rounded ledger ms.',
            'funding_note': 'Both runs independently VERIFIED_NO_SETTLEMENT: funding amount 0, not a general funding-cost model.'}


def num(value, places=6):
    return '—' if value is None else f'{D(str(value)):,.{places}f}'


def stamp(ns):
    seconds, part = divmod(int(ns), 1_000_000_000)
    return datetime.fromtimestamp(seconds, timezone.utc).strftime('%H:%M:%S') + f'.{part:09d}'


def link(root, name, label=None):
    return f'[{label or name}]({root / name})'


def render_report(new, old, comp):
    n, b = new['metrics'], old['metrics']
    root, meta = new['root'], new['metadata']
    history = json.loads(Path(meta['history_gate']['source']).read_text())
    require(sha(meta['history_gate']['source']) == meta['history_gate']['sha256'], 'history artifact changed')
    lines = [
        '# BTW 跨所永续逐笔回放：对手一档报价', '',
        '2026-10-01 00:00–24:00 UTC（北京时间 10 月 1 日 08:00 至 10 月 2 日 08:00），Bybit / Bitget，BTWUSDT。两段均已完成，账务与原始盘口成交审计均为 PASS，剩余仓位为零。', '',
        f'本轮一档方案完成 **{n["cycles"]} 组、{n["orders"]} 条模拟订单**，价差毛损益 {num(n["gross_usdt"])} USDT，手续费 {num(n["fees_usdt"])} USDT，累计净损益 **{num(n["net_usdt"])} USDT**，占初始总本金 2,000 USDT 的 {num(n["return_pct_on_2000"])}%。实际持仓资金费核验为 `VERIFIED_NO_SETTLEMENT`，金额为 0。', '',
        '**本次同时改变两项设置：**开仓、平仓和可执行风控报价改为对手一档；每腿价格保护从 1bp 改为 0，使报价/限价严格等于 ask1/bid1。5bp 净入场门槛、每笔 3bp 手续费和其他策略参数保持不变。因此收益差异不能单独归因于深度限制；这不是仅改变成交档数的纯控制实验。', '',
        '| 指标 | 原多档基线 | 本轮对手一档 |', '|---|---:|---:|',
    ]
    for label, key, places in (('闭合组合数', 'cycles', 0), ('模拟订单数', 'orders', 0),
                               ('价差毛损益 USDT', 'gross_usdt', 6), ('手续费 USDT', 'fees_usdt', 6),
                               ('资金费 USDT', 'funding_usdt', 6), ('净损益 USDT', 'net_usdt', 6),
                               ('总本金收益率 %', 'return_pct_on_2000', 6), ('盈利组合', 'wins', 0),
                               ('亏损组合', 'losses', 0), ('持平组合', 'flat', 0),
                               ('盈利组合比例 %', 'win_rate_pct', 3), ('分批退出组合数', 'partial_exit_cycles', 0),
                               ('持仓不足 1 秒的组合数', 'holding_under_1_second', 0)):
        lines.append(f'| {label} | {num(b[key], places)} | {num(n[key], places)} |')
    for venue in VENUES:
        lines.append(f'| {venue} 最终虚拟现金 USDT | {num(b["venues"][venue]["final_cash_usdt"])} | {num(n["venues"][venue]["final_cash_usdt"])} |')
    for name, key, units in (('持仓', 'holding_seconds', '秒'), ('每腿初始数量', 'entry_qty_btw_per_leg', 'BTW'),
                             ('每腿入场名义额', 'entry_notional_usdt_per_leg', 'USDT')):
        for label, item in (('最小', 'min'), ('平均', 'mean'), ('中位', 'median'), ('P95', 'p95'), ('最大', 'max')):
            lines.append(f'| {name}{label} {units} | {num(b[key][item])} | {num(n[key][item])} |')
    lines.extend(['', f'净损益变化（一档减基线）：{num(comp["delta_l1_minus_baseline"]["net_usdt"])} USDT。胜率按手续费后、资金费核验后净损益大于零计算；持仓采用原始接收纳秒差，P95 为最近秩法；每腿入场名义额统计每组两腿，共 {2*n["cycles"]} 个值。', '',
                  '| 本轮账户 | 毛损益 USDT | 手续费 USDT | 资金费 USDT | 净损益 USDT | 最终现金 USDT |',
                  '|---|---:|---:|---:|---:|---:|'])
    for venue in VENUES:
        v = n['venues'][venue]
        lines.append(f'| {venue} | {num(v["gross_usdt"])} | {num(v["fees_usdt"])} | 0 | {num(v["net_usdt"])} | {num(v["final_cash_usdt"])} |')
    lines.extend(['', '退出原因分类（记录的是首次触发原因，后续分批退出继续沿用；“止盈”分类不保证最终盈利）：', '',
                  '| 运行 | 原因 | 组合 | 盈利组合 | 毛损益 USDT | 手续费 USDT | 净损益 USDT | 持仓中位数 秒 |',
                  '|---|---|---:|---:|---:|---:|---:|---:|'])
    for name, data in (('原多档', b), ('对手一档', n)):
        for reason, group in data['exit_reason_groups'].items():
            lines.append(f'| {name} | {reason} | {group["cycles"]} | {group["wins"]} | {num(group["gross_usdt"])} | {num(group["fees_usdt"])} | {num(group["net_usdt"])} | {num(group["holding_seconds"]["median"])} |')
    lines.extend(['', '行情、信号与估值覆盖：', '', '| 项目 | 原多档基线 | 本轮对手一档 |', '|---|---:|---:|'])
    for label, key in (('Bybit 实时逐笔成交记录', 'trade_records:bybit:trade'),
                       ('Bitget 实时逐笔成交记录', 'trade_records:bitget:trade'),
                       ('Bitget 首包/重连历史成交记录（单列）', 'trade_records:bitget:historical_trade')):
        lines.append(f'| {label} | {b["raw_counts"].get(key, 0):,} | {n["raw_counts"].get(key, 0):,} |')
    for label, key in (('盘口决策次数', 'book_decisions'), ('方向信号评估数', 'signal_evaluations')):
        lines.append(f'| {label} | {b[key]:,} | {n[key]:,} |')
    for label, key in (('净边际达到 5bp 的方向评估', 'cost_threshold_passed'),
                       ('同时通过深度、费用、历史准入', 'eligible'), ('最终开仓', 'entered'),
                       ('可计算数量方向的最大原始价差 bp', 'max_gross'), ('同口径最大扣成本净边际 bp', 'max_net')):
        places = 6 if key in ('max_gross', 'max_net') else 0
        lines.append(f'| {label} | {num(b["signal_statistics"][key], places)} | {num(n["signal_statistics"][key], places)} |')
    for label, key, places in (('可完整估计双腿平仓净值的时点', 'complete_liquidation_marks', 0),
                               ('无法完整估值的时点', 'incomplete_liquidation_marks', 0),
                               ('完整平仓估值缺失率 %', 'incomplete_liquidation_pct', 3),
                               ('仅可估值点的最大回撤 USDT', 'max_drawdown_usdt', 6),
                               ('最大回撤 / 对应历史峰值 %', 'max_drawdown_pct_of_peak', 6)):
        lines.append(f'| {label} | {num(b["equity_metrics"][key], places)} | {num(n["equity_metrics"][key], places)} |')
    lines.extend(['', '缺失率分母是所有成对决策时点（包括空仓），并非仅持仓时点。一档不足以全量平仓或行情失效时，完整平仓估值可能缺失。缺失点不填零、不沿用旧估值；最大回撤仅基于剩余可估值点，可能低估真实路径风险。这里的平仓净值不是交易所 mark price 或强平模型。', '',
                  '一个持续机会会被多次评估；持仓中、冷却及资金费保护会阻止重复开仓，方向信号数不是独立机会数。数量不够最小交易单位时不计算可交易价差，上表最大价差只针对有可计算数量的方向。', '',
                  '| 方向信号记录原因 | 原多档 | 对手一档 |', '|---|---:|---:|'])
    for reason in sorted(set(b['signal_reasons']) | set(n['signal_reasons'])):
        lines.append(f'| {reason} | {b["signal_reasons"].get(reason, 0):,} | {n["signal_reasons"].get(reason, 0):,} |')
    lines.extend(['', '执行与风险约束：', '',
        '- 买入只按 ask1，卖出只按 bid1，开仓和平仓均如此；只消耗该一档的剩余参与预算，绝不走向第二档。每档初始显示量的 25% 形成预算；已观察价档按显示量增加/减少调整预算，模拟成交消耗预算。继续观察完整 top50，避免仍在观察范围内的价档离开最优再返回时恢复已耗预算。这不是 L3 队列复原。',
        '- 单腿开仓名义额最多 250 USDT，初始现金各 1,000 USDT，1 倍，两腿同 BTW 数量；开仓上限不限制已有仓位平仓时的市值。数量计算沿用 10bp 加一个 tick 的保守上界；10bp 只用于数量安全余量，不是可向深档成交的价格带。每次实际成交按名义额收 0.03% 手续费。',
        '- 一档入场价差采用对称分母：`10,000 × (short bid1 − long ask1) / ((short bid1 + long ask1) / 2)`；扣除往返四笔费用 12bp、预计退出价差 2bp、资金费预留 2bp，净边际至少 5bp，即报价价差至少 21bp，同时通过数量、现金、报价新鲜度和历史准入。',
        '- 止盈为剩余仓位预计手续费后损益达到剩余多头入场名义额的 1bp（250 USDT 对应约 0.025 USDT）；数量不足时用可执行片段的一档价外推作为风控估计，实际退出仍受两所一档共同预算限制。单组估计亏损达到 5 USDT 触发退出；完整闭合后累计现金净损益达到 −10 USDT 停止新增。这些是触发值，不保证损失上限。',
        '- 持仓目标最长 60 秒，达到后待新鲜且有预算的盘口分批退出，可能超过 60 秒；全部平仓后冷却 30 秒。每组订单数为 `2 + 2 × 分批退出次数`。23:58 UTC 起停止新增并尝试平仓；不会在日末补造旧价成交。',
        '- 已知下次资金费结算前 120 秒禁止新增并促使平仓；未知或公告过期也禁止新增。仅使用历史时点已到达的 ticker 公告，每 4 小时首分钟采样并假设公告时刻到期前有效；实际持仓是否跨结算再独立核验。资金费预留不是实际账单。', '',
        f'历史准入沿用已完成 20 个 UTC 日的波动率排名，候选池 {history["pool_count"]} 个加密货币。BTW 当天第 {history["btw"]["history_rank"]}，较低一所 RV20 为 {num(min(D(history["btw"]["bybit_rv20_bps"]), D(history["btw"]["bitget_rv20_bps"])))}bp，前十门槛 {num(history["btw"]["history_cutoff_volatility_bps"])}bp；00:01 起通过准入，首 60 秒拒绝。没有历史成交额硬下限。', '',
        '限制：两腿按可见一档同时理想成交，未建模网络/报单延迟、单腿失败、部分成交后的单边风险、订单队列、真实市场冲击或 mark price 强平。逐笔成交只用于记录，不能替代可执行盘口。采用 10 月 9 日合约目录回溯存在幸存者偏差，历史 K 线发布延迟按 60 秒假设，实际历史修订未知；BTW 为事后挑选的案例。数量步长和最小名义额使用后来的规则快照作为情景假设，未证明等同 10 月 1 日规格。单日结果不能证明实盘收益。', '',
        f'输入预检至 CSV 导出完成的墙钟耗时：原多档 {num(D(str(b["wall_elapsed_seconds"])) / 60, 2)} 分钟，一档 {num(D(str(n["wall_elapsed_seconds"])) / 60, 2)} 分钟；不含随后验收与报告生成。两段回放范围均为同一完整 UTC 日。', '',
        '每组损益（时间 UTC，数量为每腿初始 BTW；精确详情见组合/订单文件）：', '',
        '| 组 | 开仓 | 最后平仓 | 多头 / 空头 | BTW 数量 | 退出批数 | 退出原因 | 毛损益 | 费用 | 净损益 USDT |',
        '|---:|---|---|---|---:|---:|---|---:|---:|---:|',
    ])
    for i, cycle in enumerate(new['cycles'], 1):
        row = new['csv_by_id'][cycle['cycle_id']]
        lines.append(f'| {i} | {stamp(cycle["entry_ns"])} | {stamp(cycle["last_exit_ns"])} | {row["long_venue"]} / {row["short_venue"]} | {row["qty"]} | {cycle["exit_parts"]} | {cycle["exit_reason"]} | {num(cycle["gross_pnl"])} | {num(cycle["fees"])} | {num(cycle["net_before_funding"])} |')
    if not new['cycles']:
        lines.append('| — | — | — | — | — | — | 零交易 | 0 | 0 | 0 |')
    files = [('orders.csv', '全部订单'), ('cycles.csv', '全部组合'), ('signals.csv.gz', '全部方向信号'),
             ('equity.csv.gz', '全部账户估值'), ('ledger.sqlite', '完整账本'), ('summary.json', '结果汇总'),
             ('run_metadata.json', '参数与来源'), ('acceptance.json', '账务验收'),
             ('depth-fill-audit.json', '原始盘口成交验收'), ('funding-audit.json', '资金费核验'),
             ('comparison.json', '完整比较统计与输入哈希')]
    lines.extend(['', '本轮完整记录：' + ' · '.join(link(root, name, label) for name, label in files), '',
                  '基线：' + ' · '.join(link(old['root'], name, label) for name, label in
                                       [('report.md', '原报告'), ('cycles.csv', '原组合'), ('summary.json', '原汇总'), ('acceptance.json', '原验收')]), '',
                  f'原始行情：[数据目录]({meta["source_archive"]})；历史门槛：[gate-input.json]({meta["history_gate"]["source"]})；资金费公告：[causal-schedule.json]({BASE / "20261009-btw-oct1-funding/causal-schedule.json"})。'])
    return '\n'.join(lines) + '\n'


def main(root=RUN, baseline=BASELINE):
    new = load_verified(root, l1=True)
    old = load_verified(baseline, l1=False)
    comp = comparison(new, old)
    report = render_report(new, old, comp)
    # All validation happens before either result file is created.
    for name, content in [('comparison.json', json.dumps(comp, default=str, ensure_ascii=False, indent=2) + '\n'),
                          ('report.md', report)]:
        path = new['root'] / name
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_text(content)
        tmp.replace(path)
    print(json.dumps({'report': str(new['root'] / 'report.md'), 'comparison': str(new['root'] / 'comparison.json'),
                      'cycles': new['metrics']['cycles'], 'net_usdt': str(new['metrics']['net_usdt']),
                      'audits': 'PASS'}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, default=RUN)
    parser.add_argument('--baseline', type=Path, default=BASELINE)
    args = parser.parse_args()
    main(args.run, args.baseline)
