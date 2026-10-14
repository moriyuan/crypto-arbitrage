"""Retain strict August failure; bind its one exact startup orphan to gap evidence."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from audit_public_day import BASE, CONTINUITY, CRITICAL, load_module, sha, write_new

def main(root):
    root = root.resolve()
    proof_path = root.parent / 'startup-orphan-evidence.json'
    proof = json.loads(proof_path.read_text())
    audit = json.loads((root / 'audit.json').read_text())
    assert proof['status'] == 'EXACT_STARTUP_ORPHAN_PROVEN'
    assert proof['date'] == audit['date_utc'] == '2026-08-01'
    for path, digest in proof['files_sha256'].items():
        assert sha(Path(path)) == digest
    for venue in audit['venues']:
        assert not venue['examples']
        for key in CRITICAL:
            assert venue['counts'].get(key, 0) == (1 if venue['venue'] == 'bybit' and key == 'book_updates_while_uninitialized' else 0)
    scanner = load_module('frozen_continuity_for_startup', CONTINUITY)
    raw = load_module('frozen_time_for_startup', BASE / '20261009-btw-tardis-audit.py')
    scanner.ROOT = root
    venues = [scanner.scan(v) for v in ('bybit', 'bitget-futures')]
    start = raw.local_ns('2026-08-01T00:00:00.0000000Z')
    for venue in venues:
        for boundary in venue['disconnect_markers']:
            before, after = boundary.get('previous_book_local_timestamp'), boundary.get('next_book_local_timestamp')
            boundary['book_resync_gap_ns'] = raw.local_ns(after) - raw.local_ns(before) if before and after else None
            boundary['gap_semantics'] = 'Last observed book to recovery; no fabricated clock assigned to timestamp-free marker.'
            if after is None and before:
                boundary['uncovered_tail_to_day_end_ns'] = start + 86400 * 10**9 - raw.local_ns(before)
    common = {'status': 'STARTUP_ORPHAN_LIMITED_ACCEPTANCE', 'date_utc': '2026-08-01',
              'raw_integrity_status': 'PASS_WITH_EXACT_STARTUP_ORPHAN',
              'strict_original_preflight_status': 'FAILED_PRESERVED',
              'created_at_utc': datetime.now(timezone.utc).isoformat(),
              'audit_sha256': sha(root / 'audit.json'),
              'manifest_sha256': sha(root / 'manifest.json'),
              'startup_orphan_evidence_path': str(proof_path),
              'startup_orphan_evidence_sha256': sha(proof_path),
              'script_sha256': sha(Path(__file__)),
              'frozen_continuity_scanner_sha256': sha(CONTINUITY),
              'execution_enabled': False, 'raw_frames_modified': False,
              'whole_day_uninterrupted_capture': False,
              'direct_existing_load_archive_compatible': False,
              'scope': 'Full archive audit preserved. Sole allowed startup orphan is proved identical to later valid sequence delta; every other critical count is zero.',
              'ignored_for_book_reconstruction_only': {
                  'venue': 'bybit', 'file': 'bybit/offset0000.jsonl.gz',
                  **proof['orphan'],
                  'received_ns': raw.local_ns(proof['orphan']['receipt_timestamp'])},
              'first_valid_snapshot': proof['first_snapshot'],
              'earliest_initial_bybit_activity_ns': proof['earliest_permitted_bybit_activity_from_initial_snapshot_ns'],
              'adapter_requirements': ['Exact raw file/line/ns/body match for sole startup discard; any other orphan fails.',
                                       'No usable book before snapshot; no entry until snapshot plus full60s warmup.',
                                       'All ordinary disconnects invalidate books and activity until valid recovery and full60s warmup.',
                                       'Separate date-matched history and funding must pass before replay.'],
              'venues': venues}
    continuity_path = root / 'startup-continuity-audit.json'
    write_new(continuity_path, common)
    write_new(root / 'startup-replay-readiness.json', {
        **common, 'continuity_audit_sha256': sha(continuity_path),
        'strategy_replay_ready': False,
        'historical_universe_and_funding': 'Separate bundles required; not granted by limited raw acceptance.',
        'tail_policy': 'No invented fills or forward book extrapolation after final valid frame.'})
    print(json.dumps({'status': common['status'], 'path': str(continuity_path),
                      'disconnect_markers': {v['venue']: len(v['disconnect_markers']) for v in venues}}))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path)
    args = parser.parse_args()
    main(args.archive)
