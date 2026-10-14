"""Freeze declared two-date selection before any evaluation-date replay."""
import argparse
import csv
import datetime as dt
import json
from pathlib import Path

from run_day import sha, write
from selection import choose


def validate_fit_artifacts(root, day):
    root = Path(root).resolve()
    folder = root / 'search' / day
    manifest_path = root / 'input-manifest.json'
    manifest = json.loads(manifest_path.read_text())
    aggregate_path = folder / 'aggregate.json'
    launch_path = folder.with_suffix('.launch.json')
    aggregate = json.loads(aggregate_path.read_text())
    audit = json.loads((folder / 'perfect-audit.json').read_text())
    launch = json.loads(launch_path.read_text())
    if audit['status'] != 'PASS' or audit.get('execution_enabled') is not False or audit['day'] != day:
        raise ValueError('wrong or failed fit-date audit')
    if launch['day'] != day or launch['phase'] != 'search':
        raise ValueError('wrong fit-date launch')
    expected = {'aggregate_sha256': sha(aggregate_path), 'launch_sha256': sha(launch_path),
                'input_manifest_sha256': sha(manifest_path)}
    if any(audit[key] != value for key, value in expected.items()):
        raise ValueError('stale audit: aggregate, launch or input manifest changed')
    if launch['inputs'] != manifest['days'][day]:
        raise ValueError('launch/input manifest mismatch')
    for key, filename in [('protocol_sha256', 'protocol.json'), ('policies_sha256', 'policies.csv')]:
        if launch[key] != sha(root / filename) or manifest[key] != launch[key]:
            raise ValueError('launch/protocol or policies mismatch')
    for name, check in audit['scenarios'].items():
        for filename, expected_hash in check['source_sha256'].items():
            if filename not in ('orders.csv', 'cycles.csv', 'equity.csv'):
                raise ValueError('unexpected audited ledger file')
            if sha(folder / name / filename) != expected_hash:
                raise ValueError('ledger changed after accounting audit')
    return aggregate, audit, launch


def freeze(root):
    root = Path(root).resolve()
    if any((root / name).exists() for name in
           ('selection-frozen.json', 'selected-policies.csv', 'ranked-fit.csv', 'grid-scores.json')):
        raise FileExistsError('selection already exists; refusing to overwrite')
    if (root / 'evaluation').exists():
        raise ValueError('cannot select after evaluation has started')
    protocol = json.loads((root / 'protocol.json').read_text())
    manifest = json.loads((root / 'input-manifest.json').read_text())
    if sha(root / 'protocol.json') != manifest['protocol_sha256'] or sha(root / 'policies.csv') != manifest['policies_sha256']:
        raise ValueError('predeclared protocol or grid changed')
    with (root / 'policies.csv').open() as stream:
        policies = list(csv.DictReader(stream))
    by_id = {row['id']: row for row in policies}
    if len(by_id) != len(policies) or len(policies) != protocol['candidate_count']:
        raise ValueError('invalid candidate identity/count')
    scores = {name: {'id': name, 'days': {}} for name in by_id}
    source_hashes = {}
    execution_code = None
    for day in protocol['fit_dates']:
        folder = root / 'search' / day
        aggregate, audit, launch = validate_fit_artifacts(root, day)
        # The wrapper may receive audit-only fixes after a run. The exact engine,
        # binary and inherited reader must be the same for both fit/evaluation.
        current_code = {path: digest for path, digest in launch['code_sha256'].items()
                        if Path(path).name in ('perfect_taker', 'perfect_taker.cpp', 'cohort_replay.cpp')}
        if len(current_code) != 3 or any(sha(path) != digest for path, digest in current_code.items()):
            raise ValueError('executed engine or reader changed after search')
        if execution_code is not None and execution_code != current_code:
            raise ValueError('fit dates used different execution engines')
        execution_code = current_code
        actual = {row['id']: row for row in aggregate['scenarios']}
        if set(actual) != set(by_id) or set(audit['scenarios']) != set(by_id):
            raise ValueError('candidate/audit identity mismatch')
        for name, scenario in actual.items():
            for key in ('entry_bps', 'tp_bps', 'sl_bps', 'hard_bps', 'hold_ms'):
                if int(by_id[name][key]) != scenario[key]:
                    raise ValueError('executed parameter differs from declared grid')
            check = audit['scenarios'][name]
            scores[name]['days'][day] = {
                'net': scenario['net_before_funding'],
                'drawdown': scenario['complete_mark_max_drawdown_usdt'],
                'cycles': scenario['cycles'], 'orders': scenario['orders'],
                'wins': scenario['wins'], 'losses': scenario['losses'],
                'unresolved_qty': scenario['unresolved_qty'],
                'funding_status': check['funding_status'],
                'accounting_pass': check['status'] == 'PASS',
                'cash': scenario['cash']}
        for name in ('aggregate.json', 'perfect-audit.json'):
            source_hashes[str(folder / name)] = sha(folder / name)
    best, ranked = choose(list(scores.values()), protocol['fit_dates'])
    selected_id = best['id'] if best else protocol['baseline_id']
    selected_ids = sorted({selected_id, protocol['baseline_id']})
    with (root / 'selected-policies.csv').open('x', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(policies[0]))
        writer.writeheader()
        writer.writerows(by_id[name] for name in selected_ids)
    write(root / 'grid-scores.json', list(scores.values()))
    with (root / 'ranked-fit.csv').open('x', newline='') as stream:
        fields = ['rank', 'id', 'entry_bps', 'tp_bps', 'sl_bps', 'hard_bps', 'hold_ms',
                  'worst_date_net', 'sum_net', 'sum_drawdown', 'profitable_both_fit_days']
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for rank, row in enumerate(ranked, 1):
            writer.writerow({'rank': rank, **by_id[row['id']],
                             **{key: row[key] for key in fields[-4:]}})
    frozen = {'frozen_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
              'execution_enabled': False, 'status': 'SELECTED' if best else 'NO_ELIGIBLE_CANDIDATE_BASELINE_DIAGNOSTIC',
              'selected_id': selected_id, 'selected_policy': by_id[selected_id],
              'baseline_id': protocol['baseline_id'], 'eligible_candidates': len(ranked),
              'candidate_count': len(policies), 'best': best, 'ranking_rule': protocol['selection'],
              'evaluation_date_results_consumed': False, 'fit_dates': protocol['fit_dates'],
              'evaluation_date': protocol['evaluation_date'], 'source_sha256': source_hashes,
              'protocol_sha256': sha(root / 'protocol.json'),
              'execution_code_sha256': execution_code,
              'selected_policies_sha256': sha(root / 'selected-policies.csv'),
              'code_sha256': {str(Path(__file__)): sha(__file__),
                             str(Path(__file__).with_name('selection.py')): sha(Path(__file__).with_name('selection.py'))}}
    write(root / 'selection-frozen.json', frozen)
    print(json.dumps({key: frozen[key] for key in ('status', 'selected_id', 'selected_policy', 'eligible_candidates', 'best')}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('experiment')
    freeze(parser.parse_args().experiment)
