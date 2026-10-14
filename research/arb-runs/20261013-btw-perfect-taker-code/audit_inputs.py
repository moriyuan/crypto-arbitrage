"""Independent frozen-input acceptance; no production imports or network access."""
import argparse
import csv
import datetime as dt
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import numpy as np


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def require(ok, label):
    if not ok:
        raise AssertionError(label)


def run(root):
    root = Path(root).resolve()
    manifest = read(root / 'input-manifest.json')
    require(sha(root / 'protocol.json') == manifest['protocol_sha256'], 'protocol hash')
    require(sha(root / 'policies.csv') == manifest['policies_sha256'], 'policies hash')
    result = {'status': 'PASS', 'execution_enabled': False, 'days': {},
              'input_manifest_sha256': sha(root / 'input-manifest.json'),
              'auditor_sha256': sha(__file__),
              'scope': 'Rehash frozen inputs and history evidence; independently rank stored 20-day values and recompute q25 from uniform causal training samples. Prior native/history raw audits are retained, not rerun.'}
    for day, spec in manifest['days'].items():
        for label, item in {**spec['files'], 'source_summary': spec['source_summary']}.items():
            require(sha(item['path']) == item['sha256'], day + ' source hash ' + label)
        files = spec['files']
        tape = Path(files['tape']['path'])
        meta_path = tape.parent / 'tape-metadata.json'
        meta = read(meta_path)
        require(meta['tape_sha256'] == files['tape']['sha256'], 'tape metadata hash')
        history_path = Path(meta['history']['source'])
        history = read(history_path)
        history_audit_path = history_path.parent / 'independent-recalculation.json'
        history_audit = read(history_audit_path)
        require(history_audit['status'] == 'PASS', 'prior history audit')
        require(sha(history_path) == meta['history']['sha256'], 'history source hash')
        for relative, expected in history_audit['output_hashes'].items():
            require(sha(history_path.parent / relative) == expected, 'history evidence hash ' + relative)
        start = int(dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc).timestamp()) * 10**9
        require(spec['entry_start_ns'] == start + 43200 * 10**9, 'causal noon entry')
        require(spec['end_ns_exclusive'] == start + 86400 * 10**9, 'day end')
        cfg = history['config']
        require(cfg == {'window_days': 20, 'min_volatility_bps': '0', 'min_turnover_usdt': '0', 'top_n': 10, 'publication_delay_ms': 60000}, 'history config')
        require(history['pool_count'] == 420, 'history universe')
        require(history['available_from_ms'] * 10**6 == start + 60 * 10**9, 'history publication')
        btw = history['btw']
        require(btw['history_window_end_ms'] * 10**6 == start, 'completed history window')
        require(btw['history_window_start_ms'] * 10**6 == start - 20 * 86400 * 10**9, 'history 20 days')
        ranking_path = history_path.parent / history['ranking_file']
        rows = read(ranking_path)['rows']
        require(len(rows) == 420, '420 ranking rows')
        ranked = [row for row in rows if row['history_rank'] is not None]
        ranked.sort(key=lambda r: (-min(Decimal(r['bybit_rv20_bps']), Decimal(r['bitget_rv20_bps'])),
                                   -min(Decimal(r['bybit_adv20_usdt']), Decimal(r['bitget_adv20_usdt'])), r['symbol']))
        for i, row in enumerate(ranked, 1):
            require(row['history_rank'] == i, 'independent historical rank ' + row['symbol'])
        exact_btw = next(row for row in rows if row['symbol'] == 'BTWUSDT')
        require(exact_btw == btw and btw['history_filter_passed'] and btw['history_rank'] <= 10, 'BTW admission')
        activity_dir = Path(files['activity']['path']).parent
        uniform = activity_dir / 'features-uniform-seconds.npz'
        thresholds_path = Path(files['thresholds']['path'])
        calib_path = thresholds_path.with_suffix('.calibration.json')
        if not calib_path.exists():
            calib_path = thresholds_path.parent / 'calibration.json'
        calib = read(calib_path)
        require(calib['source_sha256'] == sha(uniform), 'uniform source provenance')
        require(calib['method'] == 'higher' and calib['calibrated_from_pnl'] is False, 'calibration method')
        with np.load(uniform, allow_pickle=False) as data:
            features = data['features']
            require(features.dtype == np.dtype('int64') and features.shape == (86400, 8), 'uniform exact int64 schema')
            require(np.array_equal(features[:, 0], start + np.arange(86400, dtype='int64') * 10**9), 'uniform one-second grid')
            training = features[(features[:, 0] >= start) & (features[:, 0] < spec['entry_start_ns']) & (features[:, 7] == 3)]
            require(len(training) == calib.get('training_samples', calib.get('train_samples')), 'training count')
            # Higher empirical quantile, entirely integer and no timestamp float conversions.
            index = (len(training) - 1 + 3) // 4
            values = [int(np.partition(training[:, col], index)[index]) for col in (1, 2, 3, 4)]
        with thresholds_path.open() as f:
            threshold_rows = list(csv.DictReader(f))
        q25 = next(r for r in threshold_rows if r['name'] == 'q25')
        actual = [int(q25[k]) for k in ('countA', 'countB', 'turnoverAunits', 'turnoverBunits')]
        require(actual == values and int(q25['maxAgeNs']) == 5 * 10**9, 'independent q25')
        saved = next(r for r in calib.get('rows', calib.get('thresholds')) if r[0] == 'q25')
        require(saved[1:] == values + [5 * 10**9], 'calibration threshold provenance')
        if 'thresholds_sha256' in calib:
            require(calib['thresholds_sha256'] == sha(thresholds_path), 'calibration CSV hash')
            require(calib['train_start_ns'] == start and calib['train_end_ns_exclusive'] == spec['entry_start_ns'], 'calibration causal bounds')
        schedule = read(files['funding_schedule']['path'])
        expected = sorted([(o['venue'], o['known_at_ns'], o['next_funding_timestamp_ms'] * 10**6) for o in schedule['observations']], key=lambda r: r[1])
        with Path(files['funding_csv']['path']).open() as f:
            actual = [(r['venue'], int(r['known_at_ns']), int(r['next_funding_ns'])) for r in csv.DictReader(f)]
        require(actual == expected, 'funding ns conversion and exact causal observations')
        events = read(files['funding_events']['path'])
        require(events['symbol'] == 'BTWUSDT' and events['target_day_utc'] == day, 'funding identity')
        for venue, bundle in events['venues'].items():
            require(bundle['status'] == 'BRACKETED_HISTORY', 'funding bracket')
            require(bundle['previous_event']['timestamp_ms'] * 10**6 < start and bundle['following_event']['timestamp_ms'] * 10**6 >= spec['end_ns_exclusive'], 'actual settlement coverage')
            observations = [r for r in actual if r[0] == venue]
            require(all(r[2] > r[1] for r in observations), 'causal future next-funding timestamp')
            require(all(a[1] < b[1] for a, b in zip(observations, observations[1:])), 'strict observation sequence')
        require(read(files['funding_acceptance']['path'])['status'] == 'PUBLIC_FUNDING_HISTORY_AND_CAUSAL_SCHEDULE_VERIFIED', 'funding acceptance')
        result['days'][day] = {'status': 'PASS', 'history_rank': btw['history_rank'], 'historical_ranking_rows': len(rows),
            'history_window': [btw['history_window_start_ms'], btw['history_window_end_ms']],
            'history_gate_sha256': sha(history_path), 'history_audit_sha256': sha(history_audit_path),
            'tape_metadata_sha256': sha(meta_path), 'tape_format': meta['format'], 'full_day_decisions': meta['counts']['decisions'],
            'uniform_training_samples': len(training), 'q25': q25,
            'calibration_sha256': sha(calib_path), 'uniform_features_sha256': sha(uniform),
            'funding_csv_exact_source_match': True, 'funding_observations': len(actual),
            'actual_funding_interval_audit_required_per_policy': True,
            'causal_entry_window_ns': [spec['entry_start_ns'], spec['end_ns_exclusive'] - 120 * 10**9],
            'caveats': history['caveats']}
    path = root / 'input-acceptance.json'
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'status': result['status'], 'path': str(path), 'days': list(result['days'])}))
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', required=True)
    run(p.parse_args().root)
