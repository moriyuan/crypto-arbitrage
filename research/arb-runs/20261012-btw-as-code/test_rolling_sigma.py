from decimal import Decimal as D
import pytest
from rolling_sigma import RollingSigma,NS


def test_exact_61_samples_and_quote_unit_scaling():
    s=RollingSigma()
    for i in range(60):assert not s.add(i*NS,200000+2*i,True,1)['ready']
    r=s.add(60*NS,200120,True,1)
    assert r['ready'] and r['increments']==60
    assert D(r['sigma2_usdt2_per_second'])==D('0.00000001')
    assert r['known_through_ns']==60*NS and r['window_start_ns']==0


def test_invalid_data_and_new_segments_need_a_whole_new_window():
    for kind in ('invalid','segment'):
        s=RollingSigma(2)
        s.add(0,200000,True,1);s.add(NS,200010,True,1)
        r=s.add(2*NS,200020,kind!='invalid',2 if kind=='segment' else 1)
        assert not r['ready']
        assert not s.add(3*NS,200030,True,2 if kind=='segment' else 1)['ready']
        if kind=='segment':assert s.add(4*NS,200040,True,2)['ready']
        else:
            assert not s.add(4*NS,200040,True,1)['ready']
            assert s.add(5*NS,200050,True,1)['ready']


def test_gap_does_not_become_a_one_second_return():
    s=RollingSigma(2);s.add(0,200000,True,1);s.add(NS,200100,True,1)
    r=s.add(3*NS,210000,True,1)
    assert not r['ready'] and r['increments']==0


def test_old_shock_rolls_out_and_flat_variance_can_be_zero():
    s=RollingSigma(2)
    s.add(0,200000,True,1);s.add(NS,210000,True,1)
    assert D(s.add(2*NS,210000,True,1)['sigma2_usdt2_per_second'])>0
    assert D(s.add(3*NS,210000,True,1)['sigma2_usdt2_per_second'])==0


def test_out_of_order_and_non_grid_inputs_rejected():
    s=RollingSigma();s.add(0,200000,True,1)
    for timestamp in (0,1,-NS):
        with pytest.raises(ValueError):s.add(timestamp,200000,True,1)
