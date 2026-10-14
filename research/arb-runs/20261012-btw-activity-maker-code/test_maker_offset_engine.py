import pytest
from maker_offset_engine import PricingSpec, OffsetShadow, midpoint_parity
from maker_engine import Shadow
from test_maker_engine import snapshot,tick,trade


def test_tick_offset_is_from_counterparty_not_own_side():
    s=OffsetShadow(snapshot(),PricingSpec.parse('ticks:1'))
    assert s.maker==1 and s.side=='sell' and s.quote==100301
    a=snapshot();a['long']='bitget'
    s=OffsetShadow(a,PricingSpec.parse('ticks:1'))
    assert s.maker==1 and s.side=='buy' and s.quote==100599


def test_offset_rejects_boundary_without_clipping():
    for d in (300,301):
        s=OffsetShadow(snapshot(),PricingSpec.parse(f'ticks:{d}'))
        assert s.state=='REJECTED' and s.reason=='offset_not_strictly_inside'
    s=OffsetShadow(snapshot(),PricingSpec.parse('ticks:299'))
    assert s.state=='PENDING' and s.quote==100599


def test_fraction_uses_ceiling_distance_and_odd_spread_midpoint():
    a=snapshot();a['prices']=[100100,100000,100601,100300]
    s=OffsetShadow(a,PricingSpec.parse('fraction:0.1'))
    assert s.distance==31 and s.quote==100331
    for lng,expected in [('bybit',100451),('bitget',100450)]:
        a['long']=lng
        s=OffsetShadow(a,PricingSpec.parse('fraction:0.50'))
        assert s.distance==151 and s.quote==expected


@pytest.mark.parametrize('text',['ticks:0','ticks:-1','ticks:1.5','fraction:0','fraction:1','fraction:-.1','fraction:NaN','fraction:Infinity','other:1'])
def test_invalid_pricing_parameters_rejected(text):
    with pytest.raises(ValueError):PricingSpec.parse(text)


def test_half_fraction_preserves_every_economic_event_including_late_notification():
    old=Shadow(snapshot(),50);new=OffsetShadow(snapshot(),PricingSpec.parse('fraction:0.50'))
    for s in (old,new):
        s.book(tick(1100000000),True)
        s.trade(trade(1110000000))
        s.book(tick(1210000000),True)
        s.trade(trade(1300000000,source=1200000000,qty=40))
        s.book(tick(1400000000),True)
    result=midpoint_parity([new.result()],new.events,[old.result()],old.events)
    assert result['status']=='PASS' and result['rejected_placeholder_differences']==0
    assert new.hedged_qty==old.hedged_qty==30


def test_one_tick_spread_skip_has_no_fill_and_reported_placeholder_difference():
    a=snapshot();a['prices']=[100001,100000,100301,100300]
    old=Shadow(a,50);new=OffsetShadow(a,PricingSpec.parse('fraction:.5'))
    assert old.state==new.state=='REJECTED' and old.maker_qty==new.maker_qty==0
    r=midpoint_parity([new.result()],new.events,[old.result()],old.events)
    assert r['status']=='PASS' and r['rejected_placeholder_differences']==1


def test_new_tick_quote_still_checks_postonly_at_actual_arrival():
    s=OffsetShadow(snapshot(),PricingSpec.parse('ticks:1'))
    s.book(tick(1100000000,[100100,100000,100600,100301]),True)
    assert s.state=='REJECTED' and s.reason=='post_only_would_cross'


def test_variant_names_are_canonical_and_independent_of_alpha_terminology():
    a=PricingSpec.parse('fraction:.50');b=PricingSpec.parse('fraction:0.500')
    assert a.scenario_id==b.scenario_id=='fraction_0p5'
    s=OffsetShadow(snapshot(),a)
    assert s.result()['scenario_id']=='fraction_0p5' and 'alpha_percent' not in s.result()
    assert all(e['scenario_id']=='fraction_0p5' and 'alpha_percent' not in e for e in s.events)
