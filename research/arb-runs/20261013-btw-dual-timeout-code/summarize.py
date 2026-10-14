"""Export independent lifecycle records without inventing an account equity curve."""
from collections import Counter
import csv
from decimal import Decimal as D
import json
from pathlib import Path
import argparse


def records(path):
    return [json.loads(line) for line in Path(path).open()]


def export_day(folder):
    folder = Path(folder)
    summary = json.loads((folder / 'summary.json').read_text())
    audit = json.loads((folder / 'independent-audit.json').read_text())
    funding_audit = json.loads((folder / 'funding-interval-audit.json').read_text())
    if audit['status'] != 'PASS':
        raise ValueError('independent lifecycle audit has not passed')
    rows, events = records(folder / 'cycles.jsonl'), records(folder / 'events.jsonl')
    fields = ['cycle', 'state', 'reason', 'maker_venue', 'maker_side', 'long_venue',
              'target_qty', 'maker_open_qty', 'hedge_open_qty', 'unhedged_closed_qty',
              'paired_closed_qty', 'unhedged_open_qty', 'paired_open_qty',
              'first_fill_ns', 'first_hedge_ns', 'paired_deadline_ns',
              'paired_exit_trigger_ns', 'paired_exit_reason', 'gross_realized',
              'fees', 'net_before_funding', 'pnl_complete_before_funding']
    with (folder / 'cycles.csv').open('x', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row[k] for k in fields})
    fills = [e for e in events if e['type'] == 'fill']
    with (folder / 'fills.csv').open('x', newline='') as f:
        fields = ['cycle', 'fill_id', 'ns', 'intent', 'venue', 'side', 'qty',
                  'price', 'fee', 'gross_realized', 'trigger_ns', 'reason',
                  'source_ns', 'source_sequence', 'row_ordinal', 'venue_position_after']
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        ps = summary['price_schema']['price_decimals']
        ms = summary['price_schema']['money_decimals']
        for e in fills:
            row = {k: e.get(k, '') for k in fields}
            row.update(venue=('bybit', 'bitget')[e['venue']],
                       price=str(D(e['price_units']).scaleb(-ps)),
                       fee=str(D(e['fee_units']).scaleb(-ms)),
                       gross_realized=str(D(e['gross_realized_units']).scaleb(-ms)))
            writer.writerow(row)
    filled = [r for r in rows if r['maker_open_qty']]
    closed = [r for r in filled if r['pnl_complete_before_funding']]
    residual = [r for r in rows if any(r['venue_positions'].values())]
    return {'date_utc': summary['date_utc'], 'candidates': len(rows),
            'maker_filled_opportunities': len(filled), 'closed_filled_opportunities': len(closed),
            'residual_opportunities': len(residual),
            'unresolved_flat_opportunities': sum(r['state'] == 'UNRESOLVED' and not any(r['venue_positions'].values()) for r in rows),
            'unhedged_residual_qty': sum(r['unhedged_open_qty'] for r in rows),
            'paired_residual_qty': sum(r['paired_open_qty'] for r in rows),
            'fill_records': len(fills), 'fill_intents': dict(Counter(e['intent'] for e in fills)),
            'unhedged_timeout_request_count': sum(e['type'] == 'unhedged_exit_request' and e['reason'] == 'unhedged_timeout' for e in events),
            'paired_exit_reasons': dict(Counter(r['paired_exit_reason'] for r in rows if r['paired_exit_reason'])),
            'closed_opportunities_net_before_funding_sum': str(sum((D(r['net_before_funding']) for r in closed), D(0))),
            'funding_status': funding_audit['status'],
            'independent_audit_status': audit['status'],
            'execution_enabled': False, 'portfolio_aggregation_valid': False,
            'scope': 'Counts and conditional sums of independent historical opportunities; '
                     'not a continuous portfolio or final net return.'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('experiment_directory')
    p.add_argument('--days-subdirectory', default='validated-days')
    args = p.parse_args()
    root = Path(args.experiment_directory)
    stats = [export_day(d) for d in sorted((root / args.days_subdirectory).iterdir()) if (d / 'summary.json').exists()]
    with (root / 'statistics.json').open('x') as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == '__main__':
    main()
