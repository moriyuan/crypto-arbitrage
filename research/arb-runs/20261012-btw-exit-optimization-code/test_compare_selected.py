"""Synthetic C++/Python comparison tests; never run a replay."""
from copy import deepcopy
from decimal import Decimal as D
import csv
import json
import pytest
from compare_selected import compare_records, compare_selected, DEFAULT_SCENARIO

VENUES=('bybit','bitget')
DAY=1790812800000000000


def fixture():
    py_orders=[];cpp_orders=[];py_cycles=[];cpp_cycles=[]
    cash={v:D(1000) for v in VENUES}
    for index,(long,q,reason) in enumerate([('bybit',20,'take_profit'),
        ('bitget',20,'funding_guard:schedule_expired:bybit'),('bybit',10,'scheduled_day_end')],1):
        short=next(v for v in VENUES if v!=long);ns=DAY+index*3600*10**9
        en={long:D('1'),short:D('1.003')};fees={v:D(0) for v in VENUES};gross={v:D(0) for v in VENUES}
        exit_notional={v:D(0) for v in VENUES}
        parts=[(10,'1.0008','1.0022'),(10,'1.0007','1.0023')] if index==1 else [(q,'1.0006','1.0024')]
        steps=[('entry',q,en)]+[('exit',qty,{long:D(lp),short:D(sp)}) for qty,lp,sp in parts]
        for part,(intent,qty,prices) in enumerate(steps):
            cursor=ns+part*10**9
            for venue in VENUES:
                price=prices[venue];fee=D(qty)*price*D('.0003');fees[venue]+=fee
                buy=(venue==long)==(intent=='entry')
                if intent=='entry':cash[venue]-=fee
                else:
                    g=qty*(price-en[venue])*(1 if venue==long else -1)
                    gross[venue]+=g;cash[venue]+=g-fee;exit_notional[venue]+=qty*price
                py_orders.append(dict(order_id=f'random-order-{len(py_orders)}',cycle_id=f'random-cycle-{index}',
                    decision_cursor_ns=cursor,timestamp_ms=cursor//10**6,venue=venue,
                    side='buy' if buy else 'sell',intent=intent,qty=str(qty),price=str(price),fee=str(fee),
                    notional=str(qty*price),fee_rate='.0003',position_side='long' if venue==long else 'short',
                    symbol='BTWUSDT',pair='BTW/USDT:USDT'))
                cpp_reason='funding_guard' if reason.startswith('funding_guard') else 'replay_day_end' if reason=='scheduled_day_end' else reason
                cpp_orders.append(dict(cycle=str(index),ns=cursor,timestamp_ms=cursor//10**6,venue=venue,
                    side='buy' if buy else 'sell',intent=intent,qty=str(qty),price=str(price),fee=str(fee),
                    cash=str(cash[venue]),exit_reason=cpp_reason if intent=='exit' else ''))
        g=sum(gross.values());f=sum(fees.values());exit_ns=ns+len(parts)*10**9
        common=dict(qty=str(q),long_venue=long,short_venue=short,entry_timestamp_ms=ns//10**6,
            exit_timestamp_ms=exit_ns//10**6,holding_ms=len(parts)*1000,long_entry_price=str(en[long]),
            short_entry_price=str(en[short]),gross_pnl=str(g),total_fees=str(f),net_before_funding=str(g-f),
            bybit_net_before_funding=str(gross['bybit']-fees['bybit']),
            bitget_net_before_funding=str(gross['bitget']-fees['bitget']),exit_parts=len(parts))
        py_cycles.append(dict(cycle_id=f'random-cycle-{index}',decision_cursor_ns=exit_ns,exit_reason=reason,
            long_exit_price=str(exit_notional[long]/q),short_exit_price=str(exit_notional[short]/q),**common))
        cpp_cycles.append(dict(cycle=str(index),entry_ns=ns,exit_ns=exit_ns,first_trigger_ns=ns+10**9,
                               exit_reason=cpp_reason,**common))
    config=dict(name=DEFAULT_SCENARIO,mode='entry_deterioration',tp_bps=5,sl_bps=20,hard_cap_bps=100,
        unresolved_qty=0,orders=len(cpp_orders),cycles=len(cpp_cycles),cash={v:str(cash[v]) for v in VENUES},
        gross_pnl=str(sum(D(c['gross_pnl']) for c in cpp_cycles)),
        fees=str(sum(D(c['total_fees']) for c in cpp_cycles)),
        net_before_funding=str(sum(D(c['net_before_funding']) for c in cpp_cycles)))
    meta=dict(symbol='BTWUSDT',requested_day_utc='2026-10-01',execution_enabled=False,
        risk_mode='entry_deterioration',risk_basis='remaining_long_entry_notional',
        risk_pnl_basis={'take_profit':'remaining_pair_net_before_funding',
                       'stop_loss':'remaining_pair_net_deterioration_from_entry_quotes',
                       'hard_stop_loss':'realized_plus_remaining_pair_net_before_funding'},
        policy={'risk_mode':'entry_deterioration','take_profit_bps':'5','stop_loss_bps':'20',
                'hard_stop_loss_bps':'100','bybit_fee_bps':'3','bitget_fee_bps':'3'},initial_balance_per_venue='1000')
    summary=dict(cash=config['cash'],orders=config['orders'],completed_cycles=config['cycles'],cycle=None,
        closed_pair_net_before_funding=config['net_before_funding'],cash_change_before_funding=config['net_before_funding'])
    return py_orders,cpp_orders,py_cycles,cpp_cycles,config,meta,summary


def test_equal_records_ignore_random_ids_and_compare_partial_reverse_cash():
    result=compare_records(*fixture())
    assert result['status']=='PASS',result['errors']
    assert result['orders']==14 and result['cycles']==3
    assert result['partial_exit_cycles']==1
    assert result['reason_normalizations']==['funding_guard','replay_day_end']


@pytest.mark.parametrize('collection,index,key,value',[
    (1,0,'ns',DAY+3600*10**9+1),(1,0,'qty','21'),(1,0,'price','1.00001'),
    (1,0,'fee','.006000001'),(1,0,'cash','999'),(1,0,'venue','bitget'),
    (1,0,'intent','exit'),(1,0,'side','sell'),(1,2,'cycle','9'),
    (3,0,'entry_ns',DAY+3600*10**9+1),(3,0,'exit_ns',DAY+3600*10**9+1),
    (3,0,'exit_parts',1),(3,0,'gross_pnl','1'),(3,0,'total_fees','0'),
    (3,0,'net_before_funding','99'),(3,0,'qty','10'),(3,0,'long_venue','bitget'),
    (3,0,'exit_reason','stop_loss'),(2,0,'long_exit_price','1.001'),
])
def test_any_order_or_cycle_mismatch_fails(collection,index,key,value):
    data=fixture();data[collection][index][key]=value
    assert compare_records(*data)['status']=='FAIL'


def test_count_policy_summary_and_nonfinite_mismatches_fail():
    for target,key,value in [(4,'cash',{'bybit':'1000','bitget':'1000'}),
        (4,'tp_bps',2),(4,'unresolved_qty',10),(6,'cycle',{'qty':'10'}),(6,'orders',0)]:
        data=fixture();data[target][key]=value
        assert compare_records(*data)['status']=='FAIL'
    data=fixture();data[0][0]['price']='NaN'
    assert compare_records(*data)['status']=='FAIL'
    data=list(fixture());data[1]=data[1][:-1]
    assert compare_records(*data)['status']=='FAIL'


def write_csv(path,rows):
    with path.open('w',newline='') as stream:
        w=csv.DictWriter(stream,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)


def write_fixture(root):
    py_orders,cpp_orders,py_cycles,cpp_cycles,config,meta,summary=fixture()
    screen=root/'screen';scenario=screen/DEFAULT_SCENARIO;scenario.mkdir(parents=True)
    run=root/'python';run.mkdir()
    write_csv(scenario/'orders.csv',cpp_orders);write_csv(scenario/'cycles.csv',cpp_cycles)
    write_csv(run/'orders.csv',py_orders);write_csv(run/'cycles.csv',py_cycles)
    for name,value in [('run_metadata.json',meta),('summary.json',summary),('status.json',{'status':'COMPLETED'})]:
        (run/name).write_text(json.dumps(value))
    (screen/'aggregate.json').write_text(json.dumps({'status':'COMPLETED_SCREEN_ONLY','execution_enabled':False,
        'tape_format':'BTWTP002','fee_rate':'.0003','initial_cash_per_venue':'1000','scenarios':[config]}))
    return screen,run


def test_cli_adapter_requires_completed_and_never_overwrites(tmp_path):
    screen,run=write_fixture(tmp_path);out=tmp_path/'comparison.json'
    (run/'status.json').write_text(json.dumps({'status':'RUNNING'}))
    assert compare_selected(screen,run,out)['status']=='NOT_RUN_NONCOMPLETED_REPLAY'
    assert not out.exists()
    (run/'status.json').write_text(json.dumps({'status':'COMPLETED'}))
    result=compare_selected(screen,run,out)
    assert result['status']=='PASS',result
    assert all('sha256' in evidence for evidence in result['inputs'].values())
    preserved=out.read_bytes()
    with pytest.raises(FileExistsError):compare_selected(screen,run,out)
    assert out.read_bytes()==preserved
