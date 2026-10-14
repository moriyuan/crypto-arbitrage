"""Read-only independent Decimal cross-check of the final comparison table."""
import argparse
from collections import Counter
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def j(path):
    return json.loads(path.read_text())

def audit(root, replay_dirs):
    comparison = j(root / 'comparison.json')
    actual = {(r['date_utc'], r['scenario']): r for r in comparison['rows']}
    assert len(actual) == len(comparison['rows']) == 3 * len(replay_dirs)
    reports = []; checked = 0
    for replay in replay_dirs:
        summary = j(replay / 'summary.json'); date = summary['date_utc']
        prior = j(replay.parent / 'replay-causal-noon-independent-audit.json')
        assert prior['status'] == 'PASS' and prior['summary_sha256'] == sha(replay / 'summary.json')
        unit = D(summary['price_schema']['price_unit_usdt'])
        source = Path(summary['inputs']['entry_snapshots']['path'])
        assert sha(source) == summary['inputs']['entry_snapshots']['sha256']
        snapshots = {r['cycle_id']: r for r in map(json.loads, source.read_text().splitlines())}
        quotes = [json.loads(line) for line in (replay / 'quotes.jsonl').read_text().splitlines()]
        day_rows = []; fill_subsets = {}
        for scenario in ('fixed_ticks_1', 'fixed_ticks_5', 'as_inside'):
            rows = [r for r in quotes if r['scenario_id'] == scenario]
            assert len(rows) == len(snapshots) and {r['cycle'] for r in rows} == set(snapshots)
            count = len(rows); filled = sum(r['maker_qty'] > 0 for r in rows)
            conditional_advantages = [D(r['entry_cost_advantage_on_hedged_qty']) for r in rows if r['hedged_qty']]
            summary_row = summary['scenarios'][scenario]
            assert summary_row['quotes_with_partial_target_fill'] == sum(0 < r['maker_qty'] < r['target_qty'] for r in rows)
            assert summary_row['quotes_with_positive_entry_cost_advantage'] == sum(x > 0 for x in conditional_advantages)
            assert summary_row['mean_entry_cost_advantage_per_hedged_quote'] == (str(sum(conditional_advantages) / len(conditional_advantages)) if conditional_advantages else None)
            for row in rows:
                if row['arrival_ns'] is not None:
                    assert D(5) <= row['target_qty'] * D(row['quote_price_units']) * unit <= D(250)
            values = {k: D(0) for k in ('advantage','paired_ref','all_ref','saving','slippage','fee_delta','residual_notional','residual_fee')}
            for row in rows:
                snapshot = snapshots[row['cycle']]
                reference = D(snapshot['prices'][0 if snapshot['long'] == 'bybit' else 2]) * unit
                quote = D(row['quote_price_units']) * unit
                values['advantage'] += D(row['entry_cost_advantage_on_hedged_qty'])
                values['paired_ref'] += row['hedged_qty'] * reference
                values['all_ref'] += row['target_qty'] * reference
                values['saving'] += D(row['maker_price_saving_on_hedged_qty'])
                values['slippage'] += D(row['hedge_slippage_on_hedged_qty'])
                values['fee_delta'] += D(row['entry_fee_delta_on_hedged_qty'])
                values['residual_notional'] += row['unhedged_qty'] * quote
                values['residual_fee'] += row['unhedged_qty'] * quote * D('.0003')
            assert values['advantage'] == values['saving'] - values['slippage'] - values['fee_delta']
            expected = {'date_utc': date, 'scenario': scenario, 'candidates': count,
                        'accepted_at_arrival': sum(r['arrival_ns'] is not None for r in rows),
                        'maker_fill_quotes': filled,
                        'fully_hedged_quotes': sum(r['maker_qty'] > 0 and r['unhedged_qty'] == 0 for r in rows),
                        'paired_coins': sum(r['hedged_qty'] for r in rows),
                        'residual_coins': sum(r['unhedged_qty'] for r in rows),
                        'maker_fill_rate': str(D(filled) / count) if count else None,
                        'entry_advantage_usdt': str(values['advantage']),
                        'paired_reference_notional_usdt': str(values['paired_ref']),
                        'entry_advantage_bps_on_paired_notional': str(10000 * values['advantage'] / values['paired_ref']) if values['paired_ref'] else None,
                        'all_candidate_reference_notional_usdt': str(values['all_ref']),
                        'entry_advantage_per_candidate_usdt': str(values['advantage'] / count) if count else None,
                        'maker_price_saving_usdt': str(values['saving']),
                        'hedge_price_worsening_usdt': str(values['slippage']),
                        'entry_fee_difference_usdt': str(values['fee_delta']),
                        'residual_at_fill_notional_usdt': str(values['residual_notional']),
                        'residual_entry_fee_usdt': str(values['residual_fee']),
                        'rejection_and_terminal_reasons': dict(Counter(r['reason'] for r in rows)),
                        'portfolio_pnl_computed': False,
                        'source_summary_sha256': sha(replay / 'summary.json'),
                        'source_quotes_sha256': sha(replay / 'quotes.jsonl')}
            row = actual[date, scenario]
            for field, value in expected.items():
                checked += 1
                assert row[field] == value, (date, scenario, field, row[field], value)
            fill_subsets[scenario] = {r['cycle'] for r in rows if r['hedged_qty']}
            expected['fixed_cohort_normalized_entry_advantage_bps_NOT_PNL'] = str(10000 * values['advantage'] / values['all_ref']) if values['all_ref'] else None
            day_rows.append(expected)
        reports.append({'date_utc': date, 'rows': day_rows,
                        'paired_candidate_subsets_equal': len({tuple(sorted(s)) for s in fill_subsets.values()}) == 1,
                        'paired_candidates_common_to_all_scenarios': sorted(set.intersection(*fill_subsets.values()))})
    return {'status': 'PASS', 'execution_enabled': False, 'checks': checked,
            'comparison_sha256': sha(root / 'comparison.json'), 'audit_script_sha256': sha(Path(__file__)),
            'dates': reports,
            'definitions': {'paired_denominator': 'Actually hedged coins times original long-side entry ask; differs when matched subsets/quantities differ.',
                            'all_candidate_denominator': 'Fixed target coins times original long-side entry ask for all same-day candidates; useful normalization, never portfolio PnL.',
                            'residual_notional': 'Unhedged coins valued at their actual maker quote/fill price; not current marked risk or the long-reference denominator.',
                            'fully_hedged': 'All maker quantity that actually filled is hedged; target quote quantity may remain unfilled.',
                            'fill_rate': 'Maker fill quotes divided by all candidates, including AS model-unavailable/outside-spread rejections.'},
            'limits': 'Entry-only asynchronous hypothetical execution. Different selected fills cannot establish a winner from conditional means alone; no exit/funding/portfolio return computed.'}

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--replays', type=Path, nargs='+', required=True)
    args = parser.parse_args()
    report = audit(args.root.resolve(), [p.resolve() for p in args.replays])
    target = args.root / 'comparison-independent-audit.json'
    with target.open('x') as f:
        json.dump(report, f, indent=2, allow_nan=False)
        f.write('\n')
    print(json.dumps({'status': report['status'], 'checks': report['checks'], 'path': str(target)}))
