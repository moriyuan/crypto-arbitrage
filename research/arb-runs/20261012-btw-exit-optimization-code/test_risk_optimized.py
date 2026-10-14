"""Behavioral tests for fixed-entry deterioration and legacy absolute net risk."""
from decimal import Decimal as D
import copy
import json
import pytest

from btc_cta.cross_arb.depth_sizing import DepthSizingConfig
from btc_cta.cross_arb.market import Book
from risk_policy import Policy
from runner import FirstLevelPaper
from test_first_level import pair, RULES, rows
from test_risk import exit_books


def session(tmp_path, **overrides):
    config = dict(bybit_fee_bps=D(3), bitget_fee_bps=D(3), price_guard_bps=D(0),
                  risk_mode='entry_deterioration', stop_loss_bps=D(5),
                  take_profit_bps=D(2), hard_stop_loss_bps=D(100))
    config.update(overrides)
    return FirstLevelPaper(tmp_path/'optimized.sqlite', Policy(**config),
                           symbol='BTWUSDT', depth_sizing=DepthSizingConfig(), checkpoint_every=10)


@pytest.mark.parametrize('reverse', [False, True])
def test_unchanged_quotes_include_four_fees_but_do_not_deteriorate(tmp_path, reverse):
    s=session(tmp_path)
    s.tick_offline(pair(reverse=reverse), RULES, 1000)
    c=s.state['cycle']; baseline=copy.deepcopy(c['entry_risk_baseline'])
    assert D(baseline['initial_qty']) == 20
    assert D(baseline['initial_long_entry_notional']) == 20
    assert D(baseline['initial_net_pnl']) == D('-.028036')
    assert D(baseline['net_per_coin']) == D('-.0014018')
    assert baseline['mode'] == 'entry_l1_quote_only_no_exit_budget_required'
    assert baseline['timestamp_ms'] == 1000
    s.tick_offline(pair(now=1100,reverse=reverse), RULES, 1100)
    assert s.state['orders']==2 and s.state['cycle']['pending_exit_reason']==''
    assert s.state['cycle']['entry_risk_baseline']==baseline
    ev=s.state['cycle']['exit_evaluation']
    assert D(ev['estimated_position_pnl']) < D(ev['stop_loss_threshold_usdt'])
    assert D(ev['net_deterioration_pnl']) == 0
    s.close()


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('bid,ask,closed', [
    ('.99966','1.00334',False), ('.99965','1.00335',True),
    ('.99964','1.00336',True),
])
def test_net_deterioration_inclusive_boundary_with_nonzero_fees(tmp_path,reverse,bid,ask,closed):
    s=session(tmp_path);s.tick_offline(pair(reverse=reverse),RULES,1000)
    s.tick_offline(exit_books(1100,bid,ask,reverse),RULES,1100)
    assert (s.state['cycle'] is None)==closed
    if closed:
        ev=next(e['evaluation'] for e in rows(s,'events') if e['kind']=='exit_triggered')
        assert rows(s,'cycles')[0]['exit_reason']=='stop_loss'
        assert D(ev['entry_baseline_remaining_net_pnl'])==D('-.028036')
        assert D(ev['stop_loss_threshold_usdt'])==D('-.010')
        if bid=='.99965': assert D(ev['net_deterioration_pnl'])==D('-.010')
    s.close()


def test_baseline_is_entry_quote_even_without_reverse_depth(tmp_path):
    s=session(tmp_path);books=pair()
    # Entry sides retain 80 coins, reverse sides are below one legal fragment.
    books['bybit']=Book('bybit',((D('.99990'),D(1)),),books['bybit'].asks,1000,1000,0)
    books['bitget']=Book('bitget',books['bitget'].bids,((D('1.00310'),D(1)),),1000,1000,0)
    s.tick_offline(books,RULES,1000)
    assert s.state['orders']==2
    assert D(s.state['cycle']['entry_risk_baseline']['initial_net_pnl'])==D('-.028036')
    # The first executable reverse book is already 5bp worse than entry.
    s.tick_offline(exit_books(1100,'.99965','1.00335'),RULES,1100)
    assert rows(s,'cycles')[0]['exit_reason']=='stop_loss'
    s.close()


def test_take_profit_requires_positive_net_not_gross_or_baseline_recovery(tmp_path):
    s=session(tmp_path);s.tick_offline(pair(),RULES,1000)
    s.tick_offline(exit_books(1100,'1.00011','1.00290'),RULES,1100)
    assert s.state['orders']==2
    ev=s.state['cycle']['exit_evaluation']
    assert D(ev['estimated_position_gross_pnl'])>D('.004')
    assert D(ev['estimated_position_pnl'])<0
    assert D(ev['net_deterioration_pnl'])>0
    s.tick_offline(exit_books(1200,'1.0008','1.0022'),RULES,1200)
    assert rows(s,'cycles')[0]['exit_reason']=='take_profit'
    assert D(rows(s,'cycles')[0]['net_before_funding'])>D('.004')
    s.close()


def test_partial_baseline_scales_without_reset_and_reason_is_sticky(tmp_path):
    s=session(tmp_path);s.tick_offline(pair(),RULES,1000)
    baseline=copy.deepcopy(s.state['cycle']['entry_risk_baseline'])
    s.tick_offline(exit_books(1100,'.99965','1.00335',top_qty=40),RULES,1100)
    c=s.state['cycle'];assert D(c['qty'])==10 and c['pending_exit_reason']=='stop_loss'
    assert c['entry_risk_baseline']==baseline
    unchanged=exit_books(1200,'.99990','1.00310',top_qty=80)
    s._observe_depth(unchanged)
    assert s._risk_exit_reason(unchanged,RULES,c,1200)==''
    ev=c['exit_evaluation']
    assert D(ev['entry_baseline_remaining_net_pnl'])==D('-.014018')
    assert D(ev['net_deterioration_pnl'])==0
    assert D(ev['risk_basis_notional'])==10
    assert D(ev['hard_stop_basis_notional'])==20
    s.tick_offline(unchanged,RULES,1200)
    assert rows(s,'cycles')[0]['exit_reason']=='stop_loss'
    assert rows(s,'cycles')[0]['exit_parts']==2
    assert sum(D(x)-1000 for x in s.state['cash'].values())==D(rows(s,'cycles')[0]['net_before_funding'])
    s.close()


def test_hard_cap_includes_realized_part_and_keeps_initial_notional(tmp_path):
    s=session(tmp_path,stop_loss_bps=D(1000));s.tick_offline(pair(),RULES,1000)
    s.tick_offline(exit_books(1100,'.995','1.008',top_qty=40),RULES,1100)
    c=s.state['cycle'];assert D(c['qty'])==10 and c['pending_exit_reason']=='hard_stop_loss'
    # Remaining-only net loss is above -0.2, but realized + remaining is below.
    quotes=exit_books(1200,'.996','1.007',top_qty=80);s._observe_depth(quotes)
    assert s._risk_exit_reason(quotes,RULES,c,1200)=='hard_stop_loss'
    ev=c['exit_evaluation']
    assert D(ev['hard_stop_basis_notional'])==20
    assert D(ev['hard_stop_threshold_usdt'])==D('-.2')
    assert D(ev['realized_pair_net_before_funding'])==D('-.112018')
    assert D(ev['estimated_position_pnl'])==D('-.092018')
    assert D(ev['estimated_cycle_net_before_funding'])==D('-.204036')
    s.tick_offline(quotes,RULES,1200)
    assert rows(s,'cycles')[0]['exit_reason']=='hard_stop_loss'
    assert D(rows(s,'cycles')[0]['net_before_funding'])==D('-.204036')
    s.close()


def test_initial_hard_breach_is_flagged_but_entry_is_preserved(tmp_path):
    s=session(tmp_path);books=exit_books(1000,'.99','1.013')
    # Restore original entry prices but retain the deliberately wide reverse sides.
    books['bybit']=Book('bybit',books['bybit'].bids,((D(1),D(80)),),1000,1000,0)
    books['bitget']=Book('bitget',((D('1.003'),D(80)),),books['bitget'].asks,1000,1000,0)
    s.tick_offline(books,RULES,1000)
    assert s.state['orders']==2
    assert s.state['cycle']['entry_risk_baseline']['initial_hard_budget_breach'] is True
    books={v:Book(v,b.bids,b.asks,1100,1100,0) for v,b in books.items()}
    s.tick_offline(books,RULES,1100)
    assert rows(s,'cycles')[0]['exit_reason']=='hard_stop_loss'
    s.close()


def test_absolute_net_has_no_extra_hard_cap(tmp_path):
    s=session(tmp_path,risk_mode='absolute_net',stop_loss_bps=D(1000),hard_stop_loss_bps=D(1))
    s.tick_offline(pair(),RULES,1000)
    s.tick_offline(pair(now=1100),RULES,1100)
    assert s.state['orders']==2 and s.state['cycle']['pending_exit_reason']==''
    s.close()


@pytest.mark.parametrize('bid,ask,reason',[
    ('.99501','1.00799',None),('.995','1.008','hard_stop_loss'),
    ('.99499','1.00801','hard_stop_loss'),
])
def test_hard_cap_inclusive_boundary_without_fee_rounding(tmp_path,bid,ask,reason):
    s=session(tmp_path,bybit_fee_bps=D(0),bitget_fee_bps=D(0),stop_loss_bps=D(1000))
    s.tick_offline(pair(),RULES,1000)
    s.tick_offline(exit_books(1100,bid,ask),RULES,1100)
    assert len(rows(s,'cycles'))==int(reason is not None)
    if reason:
        assert rows(s,'cycles')[0]['exit_reason']==reason
        if bid=='.995':assert D(rows(s,'cycles')[0]['net_before_funding'])==D('-.2')
    s.close()


def test_relative_policy_preserves_timeout_priority_and_checkpoint_rollback(tmp_path):
    s=session(tmp_path);s.tick_offline(pair(),RULES,1000)
    assert s.state['cycle']['entry_risk_baseline']
    # No checkpoint includes this entry: a failed batch must erase its baseline
    # together with its actual orders, cash debits, and depth consumption.
    bad=pair(now=1100);book=bad['bitget']
    bad['bitget']=Book('bitget',((D('1.003001'),D(80)),),book.asks,1100,1100,0)
    with pytest.raises(ValueError,match='off-grid'):s.tick_offline(bad,RULES,1100)
    assert s.state['cycle'] is None and not rows(s,'orders')
    assert s.state['cash']=={'bybit':'1000','bitget':'1000'}
    s.tick_offline(pair(),RULES,1000)
    s.tick_offline(exit_books(61000,'.99','1.013'),RULES,61000)
    assert rows(s,'cycles')[0]['exit_reason']=='holding_timeout'
    s.close()


@pytest.mark.parametrize('field,value',[('risk_mode','unknown'),('hard_stop_loss_bps',D(0)),
    ('hard_stop_loss_bps',D(-1)),('hard_stop_loss_bps',D('NaN'))])
def test_invalid_optimized_policy_rejected(field,value):
    with pytest.raises(ValueError):Policy(**{field:value})


def audit_fixture(tmp_path,mode,reason):
    from audit_risk import audit_records
    s=session(tmp_path,risk_mode=mode)
    s.context={'decision_cursor_ns':1_000_000_000}
    s.tick_offline(pair(),RULES,1000)
    quotes={'stop_loss':('.99965','1.00335'),
            'hard_stop_loss':('.995','1.008'),
            'take_profit':('1.0008','1.0022')}[reason]
    s.context={'decision_cursor_ns':1_100_000_000}
    s.tick_offline(exit_books(1100,*quotes),RULES,1100)
    state=json.loads(json.dumps(s.state,default=str))
    meta={k:copy.deepcopy(state[k]) for k in ('policy','risk_mode','risk_basis','risk_pnl_basis')}
    meta.update(symbol='BTWUSDT',execution_enabled=False)
    evidence=[meta,state,rows(s,'orders'),rows(s,'cycles'),rows(s,'events')]
    assert evidence[3][0]['exit_reason']==reason
    s.close()
    return evidence


@pytest.mark.parametrize('mode,reason',[
    ('absolute_net','stop_loss'),('absolute_net','take_profit'),
    ('entry_deterioration','stop_loss'),('entry_deterioration','take_profit'),
    ('entry_deterioration','hard_stop_loss'),
])
def test_independent_auditor_checks_each_rule_and_entry_baseline(tmp_path,mode,reason):
    from audit_risk import audit_records
    data=audit_fixture(tmp_path,mode,reason)
    audit=audit_records(*data)
    assert audit['status']=='PASS',audit['failures']
    assert audit['trigger_counts'][reason]==1
    assert audit['risk_mode']==mode


@pytest.mark.parametrize('field,value',[
    ('net_deterioration_pnl','-999'),('entry_baseline_remaining_net_pnl','0'),
    ('hard_stop_basis_notional','10'),('realized_pair_net_before_funding','1'),
    ('estimated_cycle_net_before_funding','0'),('hard_stop_threshold_usdt','-.1'),
])
def test_independent_auditor_rejects_relative_or_hard_cap_tampering(tmp_path,field,value):
    from audit_risk import audit_records
    data=audit_fixture(tmp_path,'entry_deterioration','hard_stop_loss')
    assert audit_records(*data)['status']=='PASS'
    next(e['evaluation'] for e in data[4] if e['kind']=='exit_triggered')[field]=value
    assert audit_records(*data)['status']=='FAIL'


def test_independent_auditor_rejects_baseline_reset_or_missing_entry(tmp_path):
    from audit_risk import audit_records
    data=audit_fixture(tmp_path,'entry_deterioration','stop_loss')
    assert audit_records(*data)['status']=='PASS'
    copied=copy.deepcopy(data)
    opened=next(e for e in copied[4] if e['kind']=='pair_opened')
    opened['cycle']['entry_risk_baseline']['timestamp_ms']=1100
    assert audit_records(*copied)['status']=='FAIL'
    copied=copy.deepcopy(data)
    copied[4]=[e for e in copied[4] if e['kind']!='pair_opened']
    assert audit_records(*copied)['status']=='FAIL'
