"""Freeze activity thresholds using only uniform training-period samples."""
import csv
import hashlib
import json
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parent.parent / '20261012-btw-activity-maker'
path = ROOT / 'activity/features-uniform-seconds.npz'
z = np.load(path)
expected = ['ns', 'count_bybit', 'count_bitget', 'turnover_bybit_1e8',
            'turnover_bitget_1e8', 'last_trade_age_bybit_ns',
            'last_trade_age_bitget_ns', 'warm_valid_mask']
assert z['fields'].tolist() == expected
x = z['features']
assert x.dtype == np.int64 and x.shape == (86400, 8)
start = 1790812800000000000
mask = (x[:, 0] >= start) & (x[:, 0] < start + 43200*10**9) & (x[:, 7] == 3)
train = x[mask]
rows = []
for q in (0.1, 0.25):
    values = np.quantile(train[:, 1:5], q, axis=0, method='higher').astype(np.int64)
    rows.append([f'q{round(100*q)}', *map(int, values), 5*10**9])
with (ROOT/'thresholds.csv').open('x') as f:
    w=csv.writer(f)
    w.writerow(['name','countA','countB','turnoverAunits','turnoverBunits','maxAgeNs'])
    w.writerows(rows)
out = dict(status='FROZEN', train_samples=len(train), fields=expected,
           thresholds=rows, source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
           protocol_sha256=hashlib.sha256((ROOT/'protocol.json').read_bytes()).hexdigest(),
           method='higher', range_utc='2026-10-01 [00:00,12:00)',
           calibrated_from_pnl=False)
(ROOT/'calibration.json').write_text(json.dumps(out, indent=2)+'\n')
print(json.dumps(out, indent=2))
