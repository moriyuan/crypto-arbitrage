"""Exact closed-pair PnL identity: mid-basis convergence minus spreads and fees."""
import argparse
from collections import defaultdict
import csv
from decimal import Decimal as D
import json
from pathlib import Path

from run_day import sha, write


def decompose(entry, exit_prices, qty, long_venue):
    entry, exit_prices = list(map(D, entry)), list(map(D, exit_prices))
    if len(entry) != 4 or len(exit_prices) != 4 or qty <= 0 or long_venue not in ('bybit', 'bitget'):
        raise ValueError('invalid pair quote layout')
    for prices in (entry, exit_prices):
        if min(prices) <= 0 or prices[0] < prices[1] or prices[2] < prices[3]:
            raise ValueError('invalid/crossed source quotes')
    lng = 0 if long_venue == 'bybit' else 1
    short = 1 - lng
    mids = lambda p: [(p[0] + p[1]) / 2, (p[2] + p[3]) / 2]
    em, xm = mids(entry), mids(exit_prices)
    convergence = D(qty) * ((em[short] - em[lng]) - (xm[short] - xm[lng]))
    crossing = D(qty) * sum(p[0] - p[1] + p[2] - p[3] for p in (entry, exit_prices)) / 2
    gross = D(qty) * (exit_prices[2 * lng + 1] - entry[2 * lng]
                      + entry[2 * short + 1] - exit_prices[2 * short])
    fees = D(qty) * D('.0003') * (entry[2 * lng] + entry[2 * short + 1]
                                        + exit_prices[2 * lng + 1] + exit_prices[2 * short])
    if convergence - crossing != gross:
        raise AssertionError('mid-basis identity failed')
    return {'mid_basis_convergence': convergence, 'bid_ask_crossing_cost': crossing,
            'gross_pnl': gross, 'fees': fees, 'net': gross - fees}


def summarize(records):
    fields = ('mid_basis_convergence', 'bid_ask_crossing_cost', 'gross_pnl', 'fees', 'net')
    out = {key: str(sum((D(r[key]) for r in records), D(0))) for key in fields}
    out.update(cycles=len(records),
               converging_cycles=sum(D(r['mid_basis_convergence']) > 0 for r in records),
               gross_winners=sum(D(r['gross_pnl']) > 0 for r in records),
               net_winners=sum(D(r['net']) > 0 for r in records),
               convergence_but_net_loss=sum(D(r['mid_basis_convergence']) > 0 and D(r['net']) < 0 for r in records))
    return out


def run(root):
    root = Path(root).resolve()
    frozen = json.loads((root / 'selection-frozen.json').read_text())
    protocol = json.loads((root / 'protocol.json').read_text())
    identifiers = {frozen['selected_id'], frozen['baseline_id']}
    records, hashes = [], {}
    for day in protocol['fit_dates'] + [protocol['evaluation_date']]:
        phase = 'search' if day in protocol['fit_dates'] else 'evaluation'
        folder = root / phase / day
        deep = json.loads((folder / 'perfect-deep-audit.json').read_text())
        quote_path = folder / 'perfect-deep-quotes.json'
        if (deep['status'] != 'PASS' or not identifiers <= set(deep['audited_ids'])
                or deep['source_entry_exit_quotes_sha256'] != sha(quote_path)
                or deep['aggregate_sha256'] != sha(folder / 'aggregate.json')):
            raise ValueError('source quote audit not current or complete')
        quote_data = json.loads(quote_path.read_text())
        if quote_data['side_order'] != ['bybit_ask', 'bybit_bid', 'bitget_ask', 'bitget_bid']:
            raise ValueError('quote layout mismatch')
        cycles = {}
        for identifier in identifiers:
            path = folder / identifier / 'cycles.csv'
            with path.open() as stream:
                for cycle in csv.DictReader(stream):
                    key = identifier, int(cycle['cycle'])
                    if key in cycles:
                        raise ValueError('duplicate cycle')
                    cycles[key] = cycle
            hashes[str(path)] = sha(path)
        seen = set()
        for quotes in quote_data['cycles']:
            key = quotes['id'], quotes['cycle']
            if key not in cycles or key in seen:
                raise ValueError('unknown or duplicate audited quote cycle')
            seen.add(key)
            cycle = cycles[key]
            if any(int(cycle[k]) != quotes[k] for k in ('qty', 'entry_ns', 'exit_ns')) or cycle['long_venue'] != quotes['long_venue']:
                raise ValueError('source quote identity mismatch')
            unit = D(10) ** -quotes['price_decimals']
            values = decompose([D(p) * unit for p in quotes['entry_prices']],
                               [D(p) * unit for p in quotes['exit_prices']], quotes['qty'], quotes['long_venue'])
            for key_out, key_cycle in [('gross_pnl', 'gross_pnl'), ('fees', 'total_fees'), ('net', 'net_before_funding')]:
                if values[key_out] != D(cycle[key_cycle]):
                    raise ValueError('decomposition differs from cash ledger')
            records.append({'day': day, 'id': quotes['id'], 'cycle': quotes['cycle'],
                            **{key_out: str(value) for key_out, value in values.items()}})
        if seen != set(cycles):
            raise ValueError('missing audited cycle quotes')
        hashes[str(quote_path)] = sha(quote_path)
        hashes[str(folder / 'perfect-deep-audit.json')] = sha(folder / 'perfect-deep-audit.json')
    by_date = []
    for identifier in sorted(identifiers):
        for day in protocol['fit_dates'] + [protocol['evaluation_date']]:
            by_date.append({'id': identifier, 'day': day,
                            **summarize([r for r in records if r['id'] == identifier and r['day'] == day])})
    totals = {identifier: summarize([r for r in records if r['id'] == identifier]) for identifier in identifiers}
    result = {'status': 'PASS_EXACT_PNL_IDENTITY', 'execution_enabled': False,
              'identity': 'net = mid-basis convergence - four half-spreads - four fees',
              'rows': by_date, 'totals': totals, 'records': records, 'source_sha256': hashes,
              'code_sha256': sha(__file__),
              'scope': 'Accounting attribution of observed closed trades. Not causal evidence that any new filter will improve future returns.'}
    if (root / 'microstructure-attribution.json').exists():
        raise FileExistsError('refusing to overwrite attribution')
    write(root / 'microstructure-attribution.json', result)
    lines = ['**净损益 = 跨所中价基差收敛收益 − 开平两所买卖价差成本 − 四笔手续费。**', '',
             '这里的买卖价差成本是四次成交相对当时本所中价的半价差之和；中价只用于事后记账分解，不是假设可按中价成交。所有数值已与真实回放四单账本逐组合精确对齐。', '',
             '| 日期 | 参数 | 中价基差收敛 | 买卖价差成本 | 手续费 | 净损益 | 基差收敛但净亏的组合 |',
             '|---|---|---:|---:|---:|---:|---:|']
    for row in by_date:
        lines.append(f"| {row['day']} | {row['id']} | {D(row['mid_basis_convergence']):+.6f} | {D(row['bid_ask_crossing_cost']):.6f} | {D(row['fees']):.6f} | {D(row['net']):+.6f} | {row['convergence_but_net_loss']} |")
    selected = totals[frozen['selected_id']]
    lines += ['', f"入选参数三日{selected['cycles']}组中，{selected['converging_cycles']}组的跨所中价基差向有利方向变化，但其中{selected['convergence_but_net_loss']}组在支付买卖价差与交易费后仍亏损。扣买卖价差后{selected['gross_winners']}组盈利，再扣手续费后{selected['net_winners']}组盈利。", '',
              '0延迟和全额成交没有消除对手一档吃单的买卖价差成本。即使价差发生回归，回归幅度不足以支付执行成本仍会亏损。应将“回归是否发生”和“回归能否覆盖成本”分别检验；不能仅从净亏损推断没有回归。', '',
              '三日是各自重置的账户样本，合计不是连续账户收益率。此项分解不估计未交易路径，也不证明加入某个事后过滤条件一定改善未来收益。']
    (root / 'microstructure-attribution.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({'status': result['status'], 'closed_cycles_checked': len(records), 'selected_totals': selected}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True)
    run(parser.parse_args().experiment)
