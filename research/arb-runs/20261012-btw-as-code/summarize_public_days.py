"""Bind accepted multi-day public raw archives and observed price precision."""
import argparse
import hashlib
import json
from pathlib import Path

DATES = ('2026-09-01', '2026-08-01', '2026-07-01')

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main(root):
    selection = root / 'availability' / 'date-selection.json'
    assert json.loads(selection.read_text())['dates_fixed_before_new_replay'] == list(DATES)
    results = []
    for date in DATES:
        day = root / 'days' / date
        archive = day / 'archive'
        limited = date == '2026-08-01'
        continuity_name = 'startup-continuity-audit.json' if limited else 'continuity-audit.json'
        readiness_name = 'startup-replay-readiness.json' if limited else 'replay-readiness.json'
        files = {name: json.loads((archive / name).read_text()) for name in ('manifest.json', 'audit.json', continuity_name, readiness_name)}
        if limited:
            proof = json.loads((day / 'startup-orphan-evidence.json').read_text())
            assert proof['status'] == 'EXACT_STARTUP_ORPHAN_PROVEN'
            acceptance = {'status': 'STARTUP_ORPHAN_LIMITED_ACCEPTANCE', 'date': date,
                          'compressed_bytes': files['audit.json']['coverage']['total_compressed_bytes'],
                          'whole_day_uninterrupted_capture': False,
                          'venues': files['audit.json']['venues']}
            assert files[continuity_name]['status'] == acceptance['status']
            assert files[continuity_name]['startup_orphan_evidence_sha256'] == sha(day / 'startup-orphan-evidence.json')
        else:
            acceptance = json.loads((day / 'raw-acceptance.json').read_text())
            assert acceptance['status'] == 'PASS_NATIVE_PREFLIGHT'
        precision = json.loads((day / 'precision-audit.json').read_text())
        assert acceptance['date'] == date
        assert precision['status'] == 'PASS' and precision['date'] == date
        assert files['audit.json']['manifest_sha256'] == sha(archive / 'manifest.json')
        assert files[continuity_name]['audit_sha256'] == sha(archive / 'audit.json')
        assert files[readiness_name]['continuity_audit_sha256'] == sha(archive / continuity_name)
        for path, digest in precision['source_hashes'].items():
            assert sha(Path(path)) == digest
        manifest = files['manifest.json']
        assert manifest['complete_files'] == len(manifest['files']) == 288
        successful = [r for r in manifest['requests'] if r['state'] == 'COMPLETE']
        failed = [r for r in manifest['requests'] if r['state'] != 'COMPLETE']
        assert len(successful) == 288 and all(r['status_code'] == 200 for r in successful)
        assert all('ReadTimeout' in r['error'] or 'ConnectError' in r['error'] for r in failed)
        raw_identity = '\n'.join(f"{name} {row['sha256']}" for name, row in sorted(manifest['files'].items())) + '\n'
        venues = []
        for item in acceptance['venues']:
            venue = item['venue']
            grid = next(v for v in precision['venues'] if v['venue'] == venue)
            continuity = next(v for v in files[continuity_name]['venues'] if v['venue'] == venue)
            count = item['counts']
            venues.append({'venue': venue, 'raw_lines': count['lines'],
                           'live_trade_records': count.get('live_trade_records', 0),
                           'historical_trade_records': count.get('historical_snapshot_trade_records', 0),
                           'book_snapshots': count.get('book_snapshot', 0),
                           'book_deltas_or_updates': count.get('book_delta', count.get('book_update', 0)),
                           'disconnect_markers': len(continuity['disconnect_markers']),
                           'book_local_gaps_gt_5s': count.get('book_local_gaps_gt_5s', 0),
                           'max_book_local_gap_ns': item['max_book_local_gap_ns'],
                           'max_observed_bbo_decimal_places': grid['max_bbo_decimal_places'],
                           'all_bbo_exactly_representable_at_1e6': grid['all_bbo_exactly_representable_at_1e6']})
        evidence = [day / ('startup-orphan-evidence.json' if limited else 'raw-acceptance.json'), day / 'precision-audit.json'] + [archive / n for n in files]
        results.append({'date': date, 'archive': str(archive), 'status': 'PASS_LIMITED_STARTUP_AND_PRECISION' if limited else 'PASS_NATIVE_PREFLIGHT_AND_PRECISION',
                        'strict_original_preflight_status': 'FAILED_PRESERVED' if limited else 'PASS',
                        'download_requests': len(manifest['requests']), 'preserved_failed_requests': len(failed),
                        'files': 288, 'compressed_bytes': acceptance['compressed_bytes'],
                        'raw_file_identity_sha256': hashlib.sha256(raw_identity.encode()).hexdigest(),
                        'whole_day_uninterrupted_capture': acceptance['whole_day_uninterrupted_capture'],
                        'venues': venues, 'evidence_sha256': {str(p): sha(p) for p in evidence}})
    result = {'status': 'PASS_WITH_EXACT_AUGUST_STARTUP_EXCEPTION', 'dates': results, 'total_files': sum(r['files'] for r in results),
              'total_compressed_bytes': sum(r['compressed_bytes'] for r in results),
              'date_selection': str(selection), 'date_selection_sha256': sha(selection),
              'script_sha256': sha(Path(__file__)), 'execution_enabled': False, 'private_credentials_used': False,
              'public_get_requests': sum(r['download_requests'] for r in results),
              'preserved_failed_public_get_requests': sum(r['preserved_failed_requests'] for r in results),
              'data_access': 'Tardis monthly-first-day public historical feed; no key or paid access.',
              'semantics': 'L2 price-level incremental books plus public live trades, not individual L3 orders.',
              'historical_timestamp_policy': 'Unmodified native receipt and source timestamps; disconnect markers preserved.',
              'strategy_replay_ready': False,
              'remaining_requirements': ['Use date-matched historical selection and funding evidence from the separate pipeline.',
                                         'Invalidate books and activity at gaps; no extrapolation through missing data.',
                                         'Use the 1e-6 tape precision and explicit venue tick assumptions; observed grids are not official historical tick rules.']}
    destination = root / 'multi-day-raw-acceptance.json'
    with destination.open('x') as f:
        json.dump(result, f, indent=2, allow_nan=False)
        f.write('\n')
    print(json.dumps({'status': result['status'], 'files': result['total_files'], 'bytes': result['total_compressed_bytes'], 'path': str(destination)}))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    main(args.root.resolve())
