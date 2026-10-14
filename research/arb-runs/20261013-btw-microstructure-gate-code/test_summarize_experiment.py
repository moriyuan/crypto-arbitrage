from decimal import Decimal as D
import importlib.util
from pathlib import Path
import struct

import numpy as np
import pytest

SPEC = importlib.util.spec_from_file_location('microstructure_summary', Path(__file__).with_name('summarize_experiment.py'))
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def test_coverage_control_mapping():
    for control in ['baseline','book_coverage','trade_coverage','flow_coverage']:
        assert M.coverage_control({'control_id':control}) == control


def test_empty_coverage_is_not_improvement():
    candidate = {'cycles': 0, 'net_before_funding_usdt': D(0), 'closed_net_bps_on_sum_entry_long_notional': None}
    control = dict(candidate)
    result = M.control_comparison(candidate, control, 0)
    assert result['evidence_status'] == 'NO_REQUIRED_FEATURE_COVERAGE'
    assert result['net_difference_usdt'] == 0
    assert result['supports_threshold_improvement'] is False


def test_gate_exact_lookup_and_validation(tmp_path):
    p = tmp_path/'gate.bin'
    p.write_bytes(b'BTWGATE1'+struct.pack('<qI', 100, 1)+struct.pack('<qI', 200, 7))
    assert M.masks_at_entries(p, 3, {200, 100}) == {100: 1, 200: 7}
    with pytest.raises(ValueError, match='exact native'):
        M.masks_at_entries(p, 3, {199})
    p.write_bytes(b'BTWGATE1'+struct.pack('<qI', 100, 9))
    with pytest.raises(ValueError, match='unknown'):
        M.masks_at_entries(p, 3, {100})
    p.write_bytes(b'BTWGATE1'+struct.pack('<qI', 100, 0))
    with pytest.raises(ValueError, match='baseline'):
        M.masks_at_entries(p, 3, {100})


def test_rejected_baseline_split_is_descriptive_and_matches_direction():
    reference = [dict(cycle=i, entry_ns=i, long_venue='bybit', qty=10, net_usdt=D(net))
                 for i, net in [(1, '2'), (2, '-3'), (3, '1'), (4, '-1')]]
    actual = [dict(reference[2], qty=20), dict(cycle=9, entry_ns=5, long_venue='bitget', qty=10, net_usdt=D(4))]
    masks = {1: 1, 2: 3, 3: 7, 4: 7, 5: 7}
    x = M.admission_filter(reference, actual, masks, 2, 1)
    assert x['blocked']['entries'] == 2
    assert x['blocked']['original_net_winners'] == x['blocked']['original_net_losers'] == 1
    assert x['blocked']['original_net_usdt'] == -1
    assert x['blocked_missing_required_feature']['original_net_usdt'] == 2
    assert x['blocked_threshold_given_coverage']['original_net_usdt'] == -3
    assert x['matched_entries'] == x['matched_with_different_qty'] == x['new_actual_entries'] == 1
    assert x['passed_but_not_actual']['entries'] == 1
    assert 'not avoided loss' in x['causal_caveat']


@pytest.mark.parametrize('pd', [5, 6])
@pytest.mark.parametrize('long', ['bybit', 'bitget'])
def test_exact_native_price_midpoint_identity(pd, long):
    unit = D(10)**-pd
    entry = [D(x)*unit for x in [414123, 414115, 414789, 414770]]
    close = [D(x)*unit for x in [414300, 414280, 414550, 414541]]
    x = M.decompose(entry, close, 30, long)
    assert x['mid_basis_convergence']-x['bid_ask_crossing_cost']-x['fees'] == x['net']
    li, si = (0, 1) if long == 'bybit' else (1, 0)
    expected_gross = D(30)*(close[2*li+1]-entry[2*li]+entry[2*si+1]-close[2*si])
    assert x['gross_pnl'] == expected_gross


def test_native_coverage_counts_events_not_seconds(tmp_path):
    import json
    p=tmp_path/'native-00000.npz'
    np.savez(p,ns=np.array([100,200,1000000000]),native_valid=np.array([True,False,True]),book_valid=np.array([True,False,True]),trade_valid=np.array([False,True,True]))
    index=tmp_path/'feature-index.json'
    index.write_text(json.dumps({'rows':3,'chunks':[{'path':str(p),'sha256':M.sha(p),'rows':3}]}))
    x=M.feature_native_coverage(index,100,1000000001)
    assert x['total_observations']==3 and x['native_valid_observations']==2
    assert x['book_coverage']['allowed_observations']==2
    assert x['flow_coverage']['allowed_observations']==1
    assert x['flow_coverage']['fraction_of_native_observations']==D(1)/3


def test_entry_stats_empty_and_even_median():
    assert M.entry_characteristics([])['median_holding_seconds'] is None
    rows = [dict(holding_seconds=D(i), entry_liquidation_cost_bps=D(10*i),
                 entry_within_spread_sum_bps=D(i), entry_long_notional_usdt=D(100),
                 net_before_funding_usdt=-D(i), exit_reason='stop_loss') for i in [1, 3]]
    x = M.entry_characteristics(rows)
    assert x['median_holding_seconds'] == 2
    assert x['mean_entry_liquidation_cost_bps'] == 20
    assert x['stop_loss_fraction'] == 1
    assert x['net_bps_on_sum_entry_long_notional'] == -200
