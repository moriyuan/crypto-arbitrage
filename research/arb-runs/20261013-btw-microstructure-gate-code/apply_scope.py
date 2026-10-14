"""Record the user's removal of persistence gates without rewriting frozen evidence."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path

from selection import choose


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def partition(policies):
    active, excluded = [], []
    for policy in policies:
        duration = policy['min_spread_persistence_ms']
        if not isinstance(duration, (int, float)) or duration < 0:
            raise ValueError('invalid persistence duration')
        (active if duration == 0 else excluded).append(policy)
    ids = {p['id'] for p in active}
    if len(ids) != len(active) or 'baseline' not in ids:
        raise ValueError('active policy identity')
    if any(p['control_id'] not in ids for p in active):
        raise ValueError('active policy missing control')
    return active, excluded


def validate(root, amendment):
    root = Path(root)
    for path, digest in amendment['source_sha256'].items():
        if sha(path) != digest:
            raise ValueError('scope source changed')
    protocol = json.loads((root/'protocol.json').read_text())
    active, excluded = partition(protocol['policies'])
    if amendment['active_policy_ids'] != [p['id'] for p in active] or amendment['excluded_policy_ids'] != [p['id'] for p in excluded]:
        raise ValueError('scope policy mismatch')
    if amendment['spread_persistence_entry_filter_enabled'] is not False or amendment['volatility_entry_filters_enabled'] is not False:
        raise ValueError('excluded filter enabled')
    if amendment['applied_after_replay_results_available'] is not True:
        raise ValueError('scope chronology must be explicit')
    rows = json.loads((root/'grid-scores.json').read_text())
    rows = [r for r in rows if r['id'] in amendment['active_policy_ids']]
    best, scores = choose(rows, protocol['fit_dates'], active, protocol['selection']['minimum_cycles_each_fit_day'])
    chosen = best['id'] if best else 'baseline'
    if amendment['selected_id'] != chosen or amendment['eligible_candidates'] != sum(s['eligible'] for s in scores):
        raise ValueError('scope fit-only selection mismatch')
    return active, excluded


def create(root):
    root = Path(root).resolve()
    protocol = json.loads((root/'protocol.json').read_text())
    active, excluded = partition(protocol['policies'])
    ids = [p['id'] for p in active]
    rows = [r for r in json.loads((root/'grid-scores.json').read_text()) if r['id'] in ids]
    best, scores = choose(rows, protocol['fit_dates'], active, protocol['selection']['minimum_cycles_each_fit_day'])
    frozen = json.loads((root/'selection-frozen.json').read_text())
    value = {
        'created_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
        'status': 'USER_SCOPE_APPLIED', 'execution_enabled': False,
        'user_instruction': '价差持续不能作为入场阈值',
        'volatility_entry_filters_enabled': False,
        'spread_persistence_entry_filter_enabled': False,
        'active_policy_ids': ids, 'excluded_policy_ids': [p['id'] for p in excluded],
        'applied_after_replay_results_available': True,
        'selection_inputs': 'Original August/September fit scores only; no new thresholds or October selection',
        'selected_id': best['id'] if best else 'baseline',
        'eligible_candidates': sum(s['eligible'] for s in scores),
        'original_frozen_selected_id': frozen['selected_id'],
        'scored_active_policies': scores,
        'archive_note': 'Frozen protocol, gate bit order and all replay ledgers preserved. Persistence scenarios are excluded historical experiments, not active candidates. Do not run the original full protocol as the current strategy.',
        'source_sha256': {str(root/name): sha(root/name) for name in ['protocol.json','policies.csv','input-manifest.json','selection-frozen.json','grid-scores.json','independent-review.json']},
        'code_sha256': {str(p): sha(p) for p in [Path(__file__), Path(__file__).with_name('selection.py')]},
    }
    validate(root, value)
    with (root/'scope-amendment.json').open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    return {'active_policies': len(active), 'excluded_policies': len(excluded), 'selected_id': value['selected_id']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('experiment')
    print(json.dumps(create(parser.parse_args().experiment), ensure_ascii=False))
