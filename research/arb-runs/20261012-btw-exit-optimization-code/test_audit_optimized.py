"""Independent auditor contracts; only synthetic books and records."""
from copy import deepcopy
from decimal import Decimal as D
from types import SimpleNamespace
import json
import pytest
import audit_accounting as accounting
import depth_fill_audit as depth


def metadata(mode='entry_deterioration'):
    policy={**accounting.EXPECTED_POLICY,'take_profit_bps':'5','stop_loss_bps':'20',
            'risk_mode':mode,'hard_stop_loss_bps':'100'}
    basis={'take_profit':'remaining_pair_net_before_funding',
           'stop_loss':'remaining_pair_net_before_funding'}
    if mode=='entry_deterioration':
        basis.update(stop_loss='remaining_pair_net_deterioration_from_entry_quotes',
                     hard_stop_loss='realized_plus_remaining_pair_net_before_funding')
    return {'symbol':'BTWUSDT','requested_day_utc':'2026-10-01',
            'execution_enabled':False,'prediction_model_used':False,
            'initial_balance_per_venue':'1000','mode':'offline_native_l1_taker_paper',
            'quote_model':'counterparty_best','execution_book_levels':1,'budget_observation_levels':50,
            'depth_sizing':{'band_bps':'10','participation':'.25'},'policy':policy,
            'risk_mode':mode,'risk_basis':'remaining_long_entry_notional','risk_pnl_basis':basis}


@pytest.mark.parametrize('mode',['absolute_net','entry_deterioration'])
@pytest.mark.parametrize('tp,sl',[('2','5'),('5','20'),('2','25'),('5','100')])
def test_accounting_accepts_run_parameters_without_fixed_tp2_sl2(mode,tp,sl):
    meta=metadata(mode);meta['policy'].update(take_profit_bps=tp,stop_loss_bps=sl)
    assert accounting.l1_policy_errors(meta)==[]
    depth.validate_l1_configuration(meta,deepcopy(meta))


@pytest.mark.parametrize('field,value',[('take_profit_bps','0'),('stop_loss_bps','NaN'),
    ('hard_stop_loss_bps','-1'),('hard_stop_loss_bps','Infinity'),('risk_mode','unknown')])
def test_auditors_reject_invalid_risk_configuration(field,value):
    meta=metadata();meta['policy'][field]=value
    assert accounting.l1_policy_errors(meta)
    with pytest.raises(depth.AuditFailure):depth.validate_l1_configuration(meta,deepcopy(meta))


def test_accounting_keeps_nonrisk_contract_and_net_basis_strict():
    for field,value in [('bybit_fee_bps','0'),('min_entry_net_bps','4'),('max_notional','251')]:
        meta=metadata();meta['policy'][field]=value
        assert 'policy:'+field in accounting.l1_policy_errors(meta)
    meta=metadata();meta['risk_pnl_basis']['stop_loss']='remaining_pair_net_before_funding'
    assert 'risk_pnl_basis' in accounting.l1_policy_errors(meta)
    with pytest.raises(depth.AuditFailure):depth.validate_l1_configuration(meta,deepcopy(meta))


def fixture(reverse=False):
    long,short=('bitget','bybit') if reverse else ('bybit','bitget')
    entries=[{'cycle_id':'c1','venue':v,'position_side':'long' if v==long else 'short',
              'side':'buy' if v==long else 'sell','intent':'entry','qty':'20',
              'price':'1' if v==long else '1.003','fee_rate':'.0003',
              'decision_cursor_ns':1_000_000_001,'timestamp_ms':1000}
             for v in ('bybit','bitget')]
    baseline={'timestamp_ms':1000,'initial_qty':'20','initial_long_entry_notional':'20',
              'prices':{long:'.99990',short:'1.00310'},'net_per_coin':'-.0014018',
              'initial_net_pnl':'-.028036','initial_hard_budget_breach':False,
              'entry_breach_action':'preserve_entry_then_protect_at_next_risk_decision',
              'mode':'entry_l1_quote_only_no_exit_budget_required'}
    event={'kind':'pair_opened','timestamp_ms':1000,'decision_cursor_ns':1_000_000_001,
           'cycle':{'cycle_id':'c1','long':long,'short':short,'entry_risk_baseline':baseline}}
    books={}
    for venue in ('bybit','bitget'):
        bid,ask=(('.99990','1') if venue==long else ('1.003','1.00310'))
        # Reverse displays deliberately cannot support even one 10-coin exit.
        books[venue]=SimpleNamespace(bid=SimpleNamespace(top=((D(bid),D(1)),)),
             ask=SimpleNamespace(top=((D(ask),D(1)),)),local_ns=1_000_000_000,
             engine_ms=1000,publish_ms=1000,provenance={'synthetic':venue})
    return entries,event,books,metadata()['policy']


@pytest.mark.parametrize('reverse',[False,True])
def test_native_reverse_bbo_proves_baseline_without_exit_budget(reverse):
    result=depth.verify_entry_baseline(*fixture(reverse))
    assert result['status']=='PASS'
    assert D(result['initial_net_pnl'])==D('-.028036')
    assert result['reverse_liquidity_required'] is False


@pytest.mark.parametrize('field,value',[('timestamp_ms',1001),('initial_qty','10'),
    ('net_per_coin','0'),('initial_net_pnl','0'),('initial_long_entry_notional','40'),
    ('initial_hard_budget_breach',True),('entry_breach_action','reject'),
    ('mode','first_executable_exit')])
def test_native_baseline_rejects_mutated_freeze_or_arithmetic(field,value):
    args=fixture();assert depth.verify_entry_baseline(*args)['status']=='PASS'
    args[1]['cycle']['entry_risk_baseline'][field]=value
    with pytest.raises(depth.AuditFailure):depth.verify_entry_baseline(*args)


def test_native_baseline_rejects_self_consistent_but_wrong_reverse_quote():
    args=fixture();b=args[1]['cycle']['entry_risk_baseline']
    # This is internally consistent arithmetic but not the actual entry BBO.
    b['prices']['bybit']='.99980';b['net_per_coin']='-.00150177';b['initial_net_pnl']='-.03003540'
    with pytest.raises(depth.AuditFailure,match='reverse quote'):depth.verify_entry_baseline(*args)


def test_baseline_exact_cursor_not_nearby_millisecond_and_source_causal():
    args=fixture();args[1]['decision_cursor_ns']+=1
    with pytest.raises(depth.AuditFailure,match='cursor'):depth.verify_entry_baseline(*args)
    args=fixture();args[2]['bybit'].local_ns=1_000_000_002
    with pytest.raises(depth.AuditFailure,match='future'):depth.verify_entry_baseline(*args)


def test_baseline_provenance_helper_rejects_wrong_order_entry_quote():
    args=fixture();args[0][0]['price']='1.00001'
    with pytest.raises(depth.AuditFailure,match='entry quote'):depth.verify_entry_baseline(*args)


def test_depth_native_budget_selftest_retained():
    result=depth.selftest()
    assert result['status']=='PASS' and result['random_top50_mutations']==2000


def test_accounting_refuses_running_ledger_and_existing_output(tmp_path):
    (tmp_path/'status.json').write_text(json.dumps({'status':'RUNNING'}))
    assert accounting.main(tmp_path)==3
    assert not (tmp_path/'ledger.sqlite').exists()
    (tmp_path/'status.json').write_text(json.dumps({'status':'COMPLETED'}))
    (tmp_path/'acceptance.json').write_text('preserve')
    with pytest.raises(RuntimeError,match='refuse_to_overwrite'):accounting.main(tmp_path)
    assert (tmp_path/'acceptance.json').read_text()=='preserve'


def test_accounting_keeps_price_fee_tick_contract():
    rules={'bybit':{'price_tick':'.00001'}}
    row={'venue':'bybit','price':'1.00001','limit_price':'1.00001'}
    assert accounting.l1_order_errors(row,rules)==[]
    row['limit_price']='1.00002'
    assert 'filled_price_not_limit' in accounting.l1_order_errors(row,rules)
    row.update(price='1.000015',limit_price='1.000015')
    assert 'price_or_limit_off_tick_grid' in accounting.l1_order_errors(row,rules)
