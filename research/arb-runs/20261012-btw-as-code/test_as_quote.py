from decimal import Decimal as D
import pytest
from as_quote import Calibration, QuoteParameters, as_quotes, CalibrationError


def calibration(**changes):
    data={'schema_version':1,'symbol':'BTWUSDT','venue':'bybit','quote_unit_coins':'10',
      'trained_from_ns':1,'trained_through_ns':100,'valid_from_ns':101,'valid_until_ns':10000,
      'accepted':True,'a_per_second':'2','k_per_usdt':'100',
      'source_sha256':['a'*64],'fit_diagnostics':{'observations':1000}}
    data.update(changes);return Calibration.from_mapping(data)


def quote(q='0',gamma='0.1',horizon='5',cal=None,**overrides):
    params={'symbol':'BTWUSDT','venue':'bybit','signal_ns':200,'bid_ticks':100000,'ask_ticks':100100,
      'tick_size':'0.00001','signed_unhedged_coins':q,'gamma_per_usdt':gamma,'horizon_seconds':horizon,
      'sigma2_usdt2_per_second':'0.01','sigma_known_through_ns':199,'sigma_ready':True}
    params.update(overrides)
    return as_quotes(QuoteParameters.from_mapping(params),cal or calibration())


def test_risk_neutral_limit_and_units():
    q=quote(gamma='0',horizon='5')
    assert q['mid_usdt_per_lot']=='10.00500'
    assert D(q['half_spread_usdt_per_lot'])==D('.01')
    assert q['buy_price_ticks']==99950 and q['sell_price_ticks']==100150
    assert q['a_used_in_quote_formula'] is False


def test_positive_inventory_moves_both_quotes_down_and_short_moves_up():
    mid=quote();long=quote(q='20');short=quote(q='-20')
    assert D(long['reservation_usdt_per_lot'])<D(mid['reservation_usdt_per_lot'])<D(short['reservation_usdt_per_lot'])
    assert long['buy_price_ticks']<mid['buy_price_ticks']<short['buy_price_ticks']
    assert long['sell_price_ticks']<mid['sell_price_ticks']<short['sell_price_ticks']
    assert D(long['inventory_lots'])==2
    assert D(long['inventory_shift_usdt_per_lot'])==D('.01')


def test_longer_horizon_widens_quotes_and_amplifies_inventory_shift():
    a=quote(q='10',horizon='1');b=quote(q='10',horizon='5')
    assert D(a['half_spread_usdt_per_lot'])<D(b['half_spread_usdt_per_lot'])
    assert D(a['inventory_shift_usdt_per_lot'])<D(b['inventory_shift_usdt_per_lot'])


def test_outside_spread_quotes_are_not_clipped():
    q=quote(gamma='0')
    assert not q['buy_strictly_inside'] and not q['sell_strictly_inside']
    assert q['buy_price_ticks']<100000 and q['sell_price_ticks']>100100


def test_rounding_is_buy_floor_sell_ceil():
    q=quote()
    assert D(q['buy_price_ticks'])*D('.00001')<=D(q['raw_buy_price_usdt_per_coin'])
    assert D(q['sell_price_ticks'])*D('.00001')>=D(q['raw_sell_price_usdt_per_coin'])
    assert D(q['raw_buy_price_usdt_per_coin'])-D(q['buy_price_ticks'])*D('.00001')<D('.00001')
    assert D(q['sell_price_ticks'])*D('.00001')-D(q['raw_sell_price_usdt_per_coin'])<D('.00001')


def test_missing_future_expired_rejected_calibration_is_not_filled_with_defaults():
    for change in ({'accepted':False},{'trained_through_ns':200,'valid_from_ns':201},{'valid_until_ns':200}):
        c=calibration(**change)
        with pytest.raises(CalibrationError):quote(cal=c)
    with pytest.raises(CalibrationError):Calibration.from_mapping({'symbol':'BTWUSDT'})
    with pytest.raises(CalibrationError):quote(venue='bitget')


@pytest.mark.parametrize('field,value',[('k_per_usdt','0'),('k_per_usdt','-1'),('a_per_second','0'),('quote_unit_coins','0'),('k_per_usdt','NaN'),('a_per_second','Infinity')])
def test_invalid_estimates_fail_closed(field,value):
    with pytest.raises(CalibrationError):calibration(**{field:value})


@pytest.mark.parametrize('field,value',[('gamma_per_usdt','-1'),('horizon_seconds','-1'),('gamma_per_usdt','NaN'),('signed_unhedged_coins','Infinity'),('sigma2_usdt2_per_second','-1')])
def test_invalid_policy_parameters_fail(field,value):
    with pytest.raises(ValueError):quote(**{field:value})


def test_half_open_calibration_validity():
    assert quote(signal_ns=101,sigma_known_through_ns=100)
    with pytest.raises(CalibrationError):quote(signal_ns=100,sigma_known_through_ns=99)
    with pytest.raises(CalibrationError):quote(signal_ns=10000)


def test_very_small_gamma_is_continuous_at_zero():
    a=quote(gamma='0');b=quote(gamma='0.00000000000000000001')
    assert abs(D(a['half_spread_usdt_per_lot'])-D(b['half_spread_usdt_per_lot']))<D('1e-18')


def test_a_changes_intensity_diagnostic_not_classic_quote():
    a=quote(cal=calibration(a_per_second='1'));b=quote(cal=calibration(a_per_second='10'))
    assert a['buy_price_ticks']==b['buy_price_ticks'] and a['sell_price_ticks']==b['sell_price_ticks']
    assert abs(D(a['predicted_buy_arrival_rate_per_second'])*10-D(b['predicted_buy_arrival_rate_per_second']))<D('1e-25')


def test_volatility_must_be_ready_and_causal():
    for change in ({'sigma_ready':False},{'sigma_known_through_ns':201}):
        with pytest.raises(ValueError):quote(**change)
