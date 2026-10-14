"""Publish audited ideal-fill baseline/selected metrics; no parameter changes."""
import argparse
import csv
from decimal import Decimal as D
import json
from pathlib import Path

from run_day import sha, write


def summarize(root):
    root = Path(root).resolve()
    frozen = json.loads((root / 'selection-frozen.json').read_text())
    protocol = json.loads((root / 'protocol.json').read_text())
    if frozen['protocol_sha256'] != sha(root / 'protocol.json'):
        raise ValueError('protocol changed')
    for path, digest in frozen['source_sha256'].items():
        if sha(path) != digest:
            raise ValueError('frozen selection evidence changed')
    ids = sorted({frozen['selected_id'], frozen['baseline_id']})
    rows = []
    sources = {}
    for day in protocol['fit_dates'] + [protocol['evaluation_date']]:
        phase = 'search' if day in protocol['fit_dates'] else 'evaluation'
        folder = root / phase / day
        agg = json.loads((folder / 'aggregate.json').read_text())
        audit = json.loads((folder / 'perfect-audit.json').read_text())
        deep = json.loads((folder / 'perfect-deep-audit.json').read_text())
        if audit['status'] != 'PASS' or deep['status'] != 'PASS':
            raise ValueError('independent audits not passed')
        if audit['aggregate_sha256'] != sha(folder / 'aggregate.json'):
            raise ValueError('stale accounting audit')
        if (deep['aggregate_sha256'] != sha(folder / 'aggregate.json')
                or deep['light_audit_sha256'] != sha(folder / 'perfect-audit.json')
                or not set(ids) <= set(deep['audited_ids'])):
            raise ValueError('deep audit does not cover current selected/baseline results')
        for check in (audit, deep):
            if (check['day'] != day or check['launch_sha256'] != sha(folder.with_suffix('.launch.json'))
                    or check['input_manifest_sha256'] != sha(root / 'input-manifest.json')):
                raise ValueError('audit identity or provenance mismatch')
        if phase == 'evaluation':
            launch = json.loads(folder.with_suffix('.launch.json').read_text())
            if launch['selection_frozen_sha256'] != sha(root / 'selection-frozen.json'):
                raise ValueError('evaluation did not use frozen selection')
        candidates = {s['id']: s for s in agg['scenarios']}
        for name in ids:
            s = candidates[name]
            check = audit['scenarios'][name]
            if check['status'] != 'PASS' or check['funding_status'] != 'VERIFIED_NO_SETTLEMENT' or s['unresolved_qty']:
                raise ValueError('cannot publish final flat no-settlement metrics')
            for filename, digest in check['source_sha256'].items():
                if sha(folder / name / filename) != digest:
                    raise ValueError('ledger changed after audit')
            row = {'day': day, 'phase': phase, 'id': name,
                   'role': 'selected' if name == frozen['selected_id'] else 'baseline',
                   **{k: s[k] for k in ('entry_bps', 'tp_bps', 'sl_bps', 'hard_bps', 'hold_ms',
                                        'cycles', 'orders', 'wins', 'losses', 'gross_pnl', 'fees',
                                        'net_before_funding', 'complete_mark_max_drawdown_usdt',
                                        'exit_depth_assumed_orders', 'exit_below_min_orders')},
                   'win_rate_pct': str((D(s['wins']) * 100 / s['cycles']).quantize(D('.0001'))) if s['cycles'] else '0',
                   'bybit_cash': s['cash']['bybit'], 'bitget_cash': s['cash']['bitget'],
                   'funding_status': check['funding_status'], 'funding_pnl': '0', 'unresolved_qty': 0,
                   'exit_reasons': s['exit_reasons']}
            if D(row['bybit_cash']) + D(row['bitget_cash']) - 2000 != D(row['net_before_funding']):
                raise ValueError('final balance does not match net result')
            rows.append(row)
        for filename in ('aggregate.json', 'perfect-audit.json', 'perfect-deep-audit.json'):
            sources[str(folder / filename)] = sha(folder / filename)
    totals = {}
    for name in ids:
        subset = [r for r in rows if r['id'] == name]
        totals[name] = {key: str(sum(D(r[key]) for r in subset)) for key in ('gross_pnl', 'fees', 'net_before_funding')}
        totals[name].update({key: sum(r[key] for r in subset) for key in ('cycles', 'orders', 'wins', 'losses')})
        totals[name]['scope'] = 'Sum across three independently reset single-date accounts; not an uninterrupted equity curve.'
    result = {'status': 'AUDITED_IDEALIZED_REPLAY', 'execution_enabled': False,
              'selected_id': frozen['selected_id'], 'baseline_id': frozen['baseline_id'],
              'rows': rows, 'totals': totals, 'source_sha256': sources,
              'selection_frozen_sha256': sha(root / 'selection-frozen.json'),
              'limitations': protocol['limitations']}
    if (root / 'comparison.json').exists() or (root / 'comparison.csv').exists():
        raise FileExistsError('refusing to overwrite comparison')
    write(root / 'comparison.json', result)
    fields = [key for key in rows[0] if key != 'exit_reasons']
    with (root / 'comparison.csv').open('x', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: row[key] for key in fields} for row in rows)
    print(json.dumps({'status': result['status'], 'rows': len(rows), 'totals': totals}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('experiment')
    summarize(parser.parse_args().experiment)
