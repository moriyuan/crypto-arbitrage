"""Price-only audit for the exact documented August startup orphan exception."""
import argparse
import json
from pathlib import Path
import audit_price_precision as precision
from audit_public_day import CRITICAL

def main(root):
    root = root.resolve()
    evidence_path = root.parent / 'startup-orphan-evidence.json'
    evidence = json.loads(evidence_path.read_text())
    original = json.loads((root / 'audit.json').read_text())
    assert evidence['date'] == original['date_utc'] == '2026-08-01'
    assert evidence['status'] == 'EXACT_STARTUP_ORPHAN_PROVEN'
    for path, digest in evidence['files_sha256'].items():
        assert precision.sha(Path(path)) == digest
    assert evidence['orphan']['line'] == 2 and evidence['first_snapshot']['line'] == 3
    assert evidence['post_snapshot_identical_delta']['line'] == 12
    assert evidence['orphan']['body_sha256'] == evidence['post_snapshot_identical_delta']['body_sha256']
    for row in original['venues']:
        assert not row['examples']
        for key in CRITICAL:
            expected = 1 if row['venue'] == 'bybit' and key == 'book_updates_while_uninitialized' else 0
            assert row['counts'].get(key, 0) == expected
    venues = [precision.scan(root, venue) for venue in ('bybit', 'bitget-futures')]
    for row in venues:
        expected = next(v for v in original['venues'] if v['venue'] == row['venue'])['counts']
        assert row['counts']['files'] == 144 and row['counts']['raw_lines'] == expected['lines']
        for name in ('book_snapshot', 'book_delta', 'book_update'):
            assert row['counts'].get(name, 0) == expected.get(name, 0)
        assert row['counts'].get('uninitialized_book_updates', 0) == (1 if row['venue'] == 'bybit' else 0)
    sources = [Path(__file__), Path(precision.__file__), root / 'manifest.json', root / 'audit.json', evidence_path]
    output = {'status': 'PASS' if all(v['all_bbo_exactly_representable_at_1e6'] for v in venues) else 'FAIL',
              'date': '2026-08-01', 'symbol': 'BTWUSDT', 'execution_enabled': False,
              'raw_preflight_status': 'OLD_STRICT_FAILED_PRESERVED; bounded startup exception evaluated separately',
              'definition': 'Every valid BBO after native L2 update in receipt order; documented initial orphan produces no BBO. Disconnect clears book and previous-price comparison.',
              'interpretation': 'Observed decimal precision and numerical grids only, not official historical legal tick size. Price audit does not grant strategy readiness.',
              'source_hashes': {str(p): precision.sha(p) for p in sources}, 'venues': venues}
    destination = root.parent / 'precision-audit.json'
    with destination.open('x') as f:
        json.dump(output, f, indent=2, allow_nan=False)
        f.write('\n')
    print(json.dumps({'status': output['status'], 'path': str(destination),
                      'venues': [{k: v[k] for k in ('venue', 'max_bbo_decimal_places', 'all_bbo_exactly_representable_at_1e6')} for v in venues]}))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    args = parser.parse_args()
    main(args.archive)
