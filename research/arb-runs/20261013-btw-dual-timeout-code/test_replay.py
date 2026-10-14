import json
from pathlib import Path
import pytest
from replay import load_inputs, fixed_quote, load_snapshots


def test_fixed_price_is_one_native_tick_from_opposing_l1():
    class Schema:
        native_tick_units = (10, 1)
    # Price array is askA,bidA,askB,bidB. Wider A is maker and long.
    s = {'prices': [100100, 100000, 100120, 100110], 'long': 'bybit'}
    assert fixed_quote(s, Schema(), 1) == 100090
    s['long'] = 'bitget'
    assert fixed_quote(s, Schema(), 1) == 100010


def test_source_hash_change_is_not_silently_replayed(tmp_path):
    import hashlib
    names = ('tape', 'trades', 'activity', 'entry_snapshots', 'thresholds')
    inputs = {}
    for n in names:
        f = tmp_path / n
        f.write_text('original')
        inputs[n] = {'path': str(f), 'sha256': hashlib.sha256(f.read_bytes()).hexdigest()}
    s = tmp_path / 'source.json'
    s.write_text(json.dumps({'date_utc': '2026-10-01', 'symbol': 'BTWUSDT',
                            'execution_enabled': False, 'inputs': inputs}))
    _, paths = load_inputs(s)
    assert paths['tape'] == str(tmp_path / 'tape')
    (tmp_path / 'trades').write_text('changed')
    with pytest.raises(ValueError, match='hash'):
        load_inputs(s)


def test_cohort_rejects_training_and_duplicate_times(tmp_path):
    class Schema:
        def validate_snapshot(self, s):
            pass
    p = tmp_path / 'snapshots'
    p.write_text(json.dumps({'entry_ns': 9}) + '\n')
    with pytest.raises(ValueError, match='evaluation'):
        load_snapshots(p, Schema(), 10, 20)
    p.write_text(json.dumps({'entry_ns': 12}) + '\n' + json.dumps({'entry_ns': 12}) + '\n')
    with pytest.raises(ValueError, match='duplicate'):
        load_snapshots(p, Schema(), 10, 20)
