"""Causal uniform-time activity quantiles, transported through the frozen q25 gate."""
import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

FIELDS = ['ns', 'count_bybit', 'count_bitget', 'turnover_bybit_1e8',
          'turnover_bitget_1e8', 'last_trade_age_bybit_ns',
          'last_trade_age_bitget_ns', 'warm_valid_mask']
HEADER = ['name', 'countA', 'countB', 'turnoverAunits', 'turnoverBunits', 'maxAgeNs']
MAX_AGE_NS = 5_000_000_000


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def write_json(path, value):
    with Path(path).open('x') as f:
        json.dump(value, f, indent=2)
        f.write('\n')


def higher_quantiles(values, percentile):
    require(isinstance(percentile, int) and 0 <= percentile <= 100, 'integer percentile required')
    require(values.dtype == np.dtype('int64') and values.ndim == 2 and
            values.shape[1] == 4 and len(values) > 0, 'nonempty int64 four-metric data required')
    require(bool(np.all(values >= 0)), 'negative activity metrics')
    # ceil((n-1)*q/100), exact integer arithmetic avoids binary float rank changes.
    index = ((len(values) - 1) * percentile + 99) // 100
    return [int(np.partition(values[:, col], index)[index]) for col in range(4)]


def training_rows(features, start_ns, cut_ns):
    require(features.dtype == np.dtype('int64') and features.ndim == 2 and
            features.shape[1] == 8, 'int64 eight-field features required')
    require(start_ns < cut_ns, 'nonempty calibration time range required')
    rows = features[(features[:, 0] >= start_ns) & (features[:, 0] < cut_ns) &
                    (features[:, 7] == 3)]
    require(len(rows) > 0, 'no complete warm training observations')
    return rows


def validate_daily_features(features, start_ns):
    require(features.dtype == np.dtype('int64') and features.shape == (86400, 8),
            'daily uniform features require (86400,8) int64')
    require(np.array_equal(features[:, 0], start_ns + np.arange(86400, dtype='int64')*10**9),
            'uniform one-second daily grid differs')
    require(bool(np.all((features[:, 7] >= 0) & (features[:, 7] <= 3))), 'invalid warm mask')


def calibrate(experiment, source_manifest):
    experiment, source_manifest = Path(experiment).resolve(), Path(source_manifest).resolve()
    protocol_path = experiment / 'protocol.json'
    protocol = json.loads(protocol_path.read_text())
    manifest = json.loads(source_manifest.read_text())
    require(protocol['execution_enabled'] is False and protocol['quantiles'] == [25, 50, 75],
            'unexpected experiment protocol')
    require(Path(protocol['source_experiment']).resolve() == source_manifest.parent,
            'source manifest is outside declared source experiment')
    days = [*protocol['fit_dates'], protocol['evaluation_date']]
    root = experiment / 'calibration'
    require(not root.exists(), 'refusing to overwrite calibration outputs')
    prepared = []
    # Validate every date and q25 identity before creating any calibration outputs.
    for day in days:
        spec = manifest['days'][day]
        start = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())*10**9
        cut = start + 43200*10**9
        require(spec['entry_start_ns'] == cut, 'source entry time is not noon UTC')
        source = Path(spec['files']['activity']['path']).with_name('features-uniform-seconds.npz')
        old_thresholds = Path(spec['files']['thresholds']['path'])
        require(sha(old_thresholds) == spec['files']['thresholds']['sha256'], 'old threshold hash changed')
        prior_calibration = old_thresholds.with_suffix('.calibration.json')
        if not prior_calibration.exists():
            prior_calibration = old_thresholds.parent / 'calibration.json'
        prior = json.loads(prior_calibration.read_text())
        source_hash = sha(source)
        require(prior['source_sha256'] == source_hash and prior['calibrated_from_pnl'] is False
                and prior['method'] == 'higher', 'prior calibration source/method differs')
        with np.load(source, allow_pickle=False) as data:
            require(data['fields'].tolist() == FIELDS, 'source field mapping differs')
            features = data['features']
        validate_daily_features(features, start)
        train = training_rows(features, start, cut)
        require(len(train) == prior.get('training_samples', prior.get('train_samples')),
                'prior training count differs')
        with old_thresholds.open(newline='') as f:
            reader = csv.DictReader(f)
            require(reader.fieldnames == HEADER, 'old threshold header differs')
            old_rows = list(reader)
        require(len(old_rows) == 2 and {r['name'] for r in old_rows} == {'q10', 'q25'},
                'legacy thresholds require q10 and q25')
        original = {r['name']: [int(r[k]) for k in HEADER[1:]] for r in old_rows}
        values_by_q = {q: higher_quantiles(train[:, 1:5], q) for q in protocol['quantiles']}
        require(values_by_q[25] + [MAX_AGE_NS] == original['q25'], 'q25 does not reproduce prior calibration')
        prepared.append((day, start, cut, source, source_hash, old_thresholds,
                         prior_calibration, original, train, values_by_q))

    root.mkdir()
    result = {'status': 'PASS', 'execution_enabled': False, 'calibrated_from_pnl': False,
              'protocol_sha256': sha(protocol_path), 'source_manifest': str(source_manifest),
              'source_manifest_sha256': sha(source_manifest), 'code_sha256': sha(__file__), 'days': {}}
    for day, start, cut, source, source_hash, old, prior_path, original, train, values_by_q in prepared:
        result['days'][day] = {}
        for q, values in values_by_q.items():
            directory = root / day / f'q{q}'
            directory.mkdir(parents=True)
            csv_path = directory / 'thresholds.csv'
            rows = [['q10', *original['q10']], ['q25', *values, MAX_AGE_NS]]
            with csv_path.open('x', newline='') as f:
                writer = csv.writer(f, lineterminator='\n')
                writer.writerow(HEADER)
                writer.writerows(rows)
            meta = {
                'status': 'PASS', 'date': day, 'true_quantile_percentile': q,
                'transport_gate_alias': 'q25',
                'transport_alias_explanation': 'The unchanged frozen engine requires q10 and q25 rows. q25 carries the actual quantile identified here; q10 is an unused legacy compatibility row.',
                'q10_original_preserved': True, 'q25_reproduces_prior_exact_values': True,
                'method': 'higher', 'integer_quantile_index': ((len(train)-1)*q+99)//100,
                'quantile_formula': 'ceil((n-1)*q/100)',
                'train_start_ns': start, 'train_end_ns_exclusive': cut,
                'first_eligible_entry_ns': cut, 'training_samples': len(train),
                'training_first_ns': int(train[0, 0]), 'training_last_ns': int(train[-1, 0]),
                'required_warm_mask': 3, 'activity_window_seconds': 60,
                'max_trade_age_ns': MAX_AGE_NS, 'fields': FIELDS,
                'source': str(source), 'source_sha256': source_hash,
                'prior_thresholds': str(old), 'prior_thresholds_sha256': sha(old),
                'prior_calibration': str(prior_path), 'prior_calibration_sha256': sha(prior_path),
                'thresholds_path': str(csv_path), 'thresholds_sha256': sha(csv_path),
                'actual_thresholds': dict(zip(HEADER[1:], values + [MAX_AGE_NS])), 'rows': rows,
                'calibrated_from_pnl': False, 'execution_enabled': False,
                'protocol_sha256': result['protocol_sha256'], 'code_sha256': result['code_sha256'],
            }
            metadata_path = directory / 'calibration.json'
            write_json(metadata_path, meta)
            result['days'][day][f'q{q}'] = {
                'thresholds': str(csv_path), 'thresholds_sha256': sha(csv_path),
                'metadata': str(metadata_path), 'metadata_sha256': sha(metadata_path),
                'training_samples': len(train), 'actual_thresholds': meta['actual_thresholds'],
            }
    write_json(root / 'manifest.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--experiment', required=True, type=Path)
    parser.add_argument('--source-manifest', required=True, type=Path)
    args = parser.parse_args()
    result = calibrate(args.experiment, args.source_manifest)
    print(json.dumps({'status': result['status'], 'days': result['days']}, indent=2))
