"""Independent evidence audit for AS quote diagnostics; no production imports."""
import argparse
from collections import defaultdict,Counter
from decimal import Decimal as D,localcontext,ROUND_FLOOR,ROUND_CEILING
import csv,hashlib,json,math,struct,time
from pathlib import Path
import numpy as np

NS=10**9;LAT=10**8
HEADER=struct.Struct('<qBBBBI8q');UPDATE=struct.Struct('<B7xqq');TRADE=struct.Struct('<qqBB6xqqqII8x')

def digest(p):
    with open(p,'rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def lines(p):return [json.loads(s) for s in Path(p).read_text().splitlines()]
def near(a,b):return abs(D(a)-D(b))<=D('1e-25')*max(1,abs(D(b)))

def audit(directory):
    started=time.time();root=Path(directory);meta=json.loads((root/'summary.json').read_text());pd=meta['price_schema']['price_decimals'];money=10**(pd+4);scale=10**(8-pd);tick=D(1)/10**pd
    for info in meta['inputs'].values():assert digest(info['path'])==info['sha256']
    rows=lines(root/'quotes.jsonl');events=lines(root/'events.jsonl');features={r['ns']:r for r in lines(root/'signal-features.jsonl')}
    snapshots={r['cycle_id']:r for r in lines(meta['inputs']['entry_snapshots']['path'])}
    assert len(snapshots)==meta['evaluation_candidate_count']
    assert meta['excluded_training_candidates']==0
    assert all(meta['evaluation_start_ns']<=s['entry_ns']<meta['evaluation_end_ns_exclusive'] for s in snapshots.values())
    assert len(rows)==3*len(snapshots) and len({(r['cycle'],r['scenario_id']) for r in rows})==len(rows)
    assert {r['scenario_id'] for r in rows}==({'fixed_ticks_1','fixed_ticks_5','as_inside'} if rows else set())
    cohort_root=Path(meta['inputs']['entry_snapshots']['path']).parent
    cohort_orders=defaultdict(list)
    with (cohort_root/'orders.csv').open() as stream:
        for order in csv.DictReader(stream):
            if order['intent']=='entry':assert int(order['ns'])>=meta['evaluation_start_ns']
            cohort_orders[int(order['ns'])].append(order)
    calibration=json.loads(Path(meta['inputs']['calibration']['path']).read_text())
    assert calibration['date_utc']==meta['date_utc'] and calibration['evaluation_start_ns']==meta['evaluation_start_ns']
    assert calibration['training_receipt_end_ns_exclusive']==meta['evaluation_start_ns']
    for name in ('tape','trades','grid','protocol'):
        assert calibration['inputs'][name]['sha256']==meta['inputs'][name]['sha256']
    for fit in calibration['venues'].values():
        assert fit['trained_through_ns']==meta['evaluation_start_ns']-1
        assert fit['valid_from_ns']==meta['evaluation_start_ns'] and fit['valid_until_ns']==meta['evaluation_end_ns_exclusive']
    with np.load(meta['inputs']['grid']['path'],allow_pickle=False) as z:grid=z['data']
    bykey=defaultdict(list)
    for event in events:bykey[event['cycle'],event['scenario_id']].append(event)
    assert set(bykey)=={(r['cycle'],r['scenario_id']) for r in rows}
    fills={};hedge_at=defaultdict(list);arrival_at=defaultdict(list);starts=defaultdict(list);quotes={};counters=Counter();sums={}
    for row in rows:
        key=(row['cycle'],row['scenario_id']);quotes[key]=row;s=snapshots[row['cycle']];p=s['prices'];v=0 if p[0]-p[1]>=p[2]-p[3] else 1
        buy=(s['long']==('bybit','bitget')[v]);side=0 if buy else 1;ask,bid=p[2*v:2*v+2];native=meta['price_schema']['native_tick_units'][v]
        assert row['signal_ns']==s['entry_ns']>=meta['evaluation_start_ns'];assert row['target_qty']==s['qty'];assert row['maker_venue']==('bybit','bitget')[v]
        assert row['maker_side']==('buy' if buy else 'sell');assert row['quote_evidence']['inventory_signed_coins']=='0'
        ev=bykey[key];assert len(ev)==row['event_count'];assert ev[0]['type']=='quote_request';assert ev[0]['ns']==row['signal_ns']
        if row['scenario_id'].startswith('fixed_ticks_'):
            distance=int(row['scenario_id'].rsplit('_',1)[1])*native
            assert row['quote_price_units']==(ask-distance if buy else bid+distance)
        else:
            fit=calibration['venues'][('bybit','bitget')[v]];vol=features[row['signal_ns']][('bybit','bitget')[v]]
            idx=(row['signal_ns']-int(grid[0,0]))//NS;sample=grid[max(0,idx-60):idx+1];good=len(sample)==61 and bool(np.all(sample[:,3]&(1<<v))) and len(set(sample[:,4+v]))==1
            assert good==vol['ready']
            if good:
                increments=[(D(int(b)-int(a))*tick*5)**2 for a,b in zip(sample[:-1,1+v],sample[1:,1+v])]
                sigma=sum(increments)/60;assert near(sigma,vol['sigma2_usdt2_per_second']);assert vol['known_through_ns']==int(sample[-1,0])<=row['signal_ns'];counters['sigma_checks']+=1
            if fit['accepted'] and good:
                assert fit['trained_through_ns']<fit['valid_from_ns']==meta['evaluation_start_ns']<=row['signal_ns']<fit['valid_until_ns'];q=row['quote_evidence']['as']
                with localcontext() as ctx:
                    ctx.prec=50;mid=D(ask+bid)*tick*5;k=D(fit['k_per_usdt']);gamma=D('.004');half=gamma*sigma*2/2+(1+gamma/k).ln()/gamma
                    raw=(mid-half if buy else mid+half)/10
                    expected=int((raw/tick/native).to_integral_value(rounding=ROUND_FLOOR if buy else ROUND_CEILING))*native
                assert near(half,q['half_spread_usdt_per_lot']);assert expected==row['quote_price_units'];counters['as_formula_checks']+=1
                inside=bid*tick<raw<ask*tick and bid<expected<ask
                if not inside:assert row['reason']=='as_outside_spread'
            elif not fit['accepted']:assert row['reason']=='as_calibration_unaccepted'
            else:assert row['reason']=='as_sigma_unavailable'
        maker=[e for e in ev if e['type']=='maker_fill'];hedges=[e for e in ev if e['type']=='hedge_fill']
        mq=sum(e['qty'] for e in maker);hq=sum(e['qty'] for e in hedges)
        assert (mq,hq,mq-hq)==(row['maker_qty'],row['hedged_qty'],row['unhedged_qty']);assert mq<=row['target_qty']
        assert D(row['maker_fees'])*money==sum(e['fee_units'] for e in maker);assert D(row['hedge_fees'])*money==sum(e['fee_units'] for e in hedges)
        remaining=defaultdict(int);running_maker=running_hedged=0
        for event in ev:
            if event['type']=='maker_fill':
                remaining[event['ns']]+=event['qty'];running_maker+=event['qty']
                assert event['unhedged_qty']==running_maker-running_hedged
            elif event['type']=='hedge_fill':
                for allocation in event['allocations']:
                    assert allocation['qty']>0 and allocation['qty']<=remaining[allocation['fill_ns']]
                    assert event['ns']-allocation['fill_ns']>=LAT
                    remaining[allocation['fill_ns']]-=allocation['qty']
                running_hedged+=event['qty'];assert event['unhedged_qty']==running_maker-running_hedged
        assert sum(remaining.values())==row['unhedged_qty']
        totals=Counter();long=0 if s['long']=='bybit' else 1;hv=1-v;hside=hv*2+(0 if hv==long else 1);pm=p[2*v+side];ph=p[hside]
        prior_maker=0
        for e in maker:
            assert e['price_units']==row['quote_price_units'];assert e['fee_units']==3*e['price_units']*e['qty'];assert e['hedge_eligible_ns']==e['ns']+LAT
            assert row['arrival_ns']<=e['source_ns']<row['cancel_ack_ns'];assert e['source_ns']<=e['ns'];assert e['qty']%(10 if v==0 else 1)==0
            quantum=10 if v==0 else 1
            assert e['qty']==min(row['target_qty']-prior_maker,e['trade_qty_1e8']//(4*10**8*quantum)*quantum)
            prior_maker+=e['qty']
            fills.setdefault((e['ns'],e['venue'],e['source_sequence'],e['row_ordinal']),[]).append((row,e))
        for e in hedges:
            assert e['venue']==hv and e['side']==('buy' if hv==long else 'sell');assert e['qty']%10==0;assert e['qty']*e['price_units']>=5*10**pd
            assert e['fee_units']==3*e['price_units']*e['qty'];assert sum(a['qty'] for a in e['allocations'])==e['qty'];assert all(a['latency_ns']==e['ns']-a['fill_ns']>=LAT for a in e['allocations'])
            saving=e['qty']*10000*(pm-row['quote_price_units'])*(1 if buy else -1);slip=e['qty']*10000*(e['price_units']-ph)*(1 if hv==long else -1)
            delta=3*e['qty']*(row['quote_price_units']+e['price_units']-pm-ph)
            assert e['maker_price_saving_units']==saving;assert e['hedge_slippage_units']==slip;assert e['entry_price_improvement_units']==saving-slip;assert e['entry_fee_delta_units']==delta
            totals.update(saving=saving,slippage=slip,improvement=saving-slip,fee_delta=delta,hedge_notional=e['qty']*e['price_units']);hedge_at[e['ns']].append((key,e,hside))
        for field,total in [('maker_price_saving_on_hedged_qty','saving'),('hedge_slippage_on_hedged_qty','slippage'),('entry_price_improvement_on_hedged_qty','improvement'),('entry_fee_delta_on_hedged_qty','fee_delta')]:assert D(row[field])*money==totals[total]
        assert D(row['entry_cost_advantage_on_hedged_qty'])*money==totals['improvement']-totals['fee_delta'];assert D(row['hedge_notional'])*10**pd==totals['hedge_notional']
        for e in ev:
            if e['type']=='quote_arrival':arrival_at[e['ns']].append((key,e))
            if e['type']=='cancel_request':assert e['effective_ns']==e['ns']+LAT
        if row['arrival_ns'] is not None:assert row['arrival_ns']>=row['signal_ns']+LAT
        starts[row['signal_ns']].append(key);counters['quotes']+=1;counters['maker_fills']+=len(maker);counters['hedge_fills']+=len(hedges)
    seen=set();maxlag=0
    with open(meta['inputs']['trades']['path'],'rb') as f:
        assert f.read(8)==b'BTWTRD01'
        while raw:=f.read(64):
            ns,source,v,side,price,qty,seq,ordinal,flags=TRADE.unpack(raw);assert 0<=ns-source<=NS;maxlag=max(maxlag,ns-source)
            key=(ns,v,seq,ordinal)
            for row,e in fills.get(key,[]):
                assert flags==0 and side==(1 if row['maker_side']=='buy' else 0);assert source==e['source_ns'];assert price==e['trade_price_1e8'] and qty==e['trade_qty_1e8']
                assert price<row['quote_price_units']*scale if row['maker_side']=='buy' else price>row['quote_price_units']*scale
                assert e['qty']<=qty//(4*10**8*(10 if v==0 else 1))*(10 if v==0 else 1)
                seen.add((e['cycle'],e['scenario_id'],key))
    assert len(seen)==counters['maker_fills']
    active={};arrived=hedged=cohort_order_checks=0
    # Independent consumed-depth representation: available quarters = displayed
    # coins minus consumed debt; a display reduction clips debt, deletion resets.
    global_display=[{} for _ in range(4)];global_debt=[{} for _ in range(4)]
    with open(meta['inputs']['tape']['path'],'rb') as f:
        assert f.read(8)==(b'BTWTP002' if pd==5 else b'BTWTP003')
        while raw:=f.read(80):
            ns,valid,force,hist,pad,n,*vals=HEADER.unpack(raw);updates=f.read(n*24);px=vals[:4];display=vals[4:]
            if valid:
                for side,price,qty in UPDATE.iter_unpack(updates):
                    if qty==0:
                        global_display[side].pop(price,None);global_debt[side].pop(price,None)
                    else:
                        global_display[side][price]=qty;global_debt[side][price]=min(global_debt[side].get(price,0),qty)
                    for st in active.values():
                        book=st['budget'][side]
                        if qty==0:book.pop(price,None)
                        else:
                            prior_debt=book.get(price,(0,0))[1];book[price]=[qty,min(prior_debt,qty)]
            for key in starts.get(ns,[]):
                row=quotes[key];s=snapshots[row['cycle']];assert valid and hist and not force;assert s['prices']==px
                for side,levels in enumerate(s['sides']):
                    expected={p:(q,q-global_debt[side].get(p,0)) for p,q in global_display[side].items()}
                    actual={x['price_units_1e8']:(x['displayed_qty'],x['remaining_quarter']) for x in levels}
                    assert actual==expected,('cohort pre-entry budget',key,side)
                active[key]={'budget':[{x['price_units_1e8']:[x['displayed_qty'],x['displayed_qty']-x['remaining_quarter']] for x in side} for side in s['sides']],
                  'hedged':0,'notional':0,'last_ns':max(e['ns'] for e in bykey[key])}
            for key,e in arrival_at.get(ns,[]):
                row=quotes[key];v=0 if row['maker_venue']=='bybit' else 1;assert valid
                assert e['own_ask_price_units']==px[2*v] and e['own_bid_price_units']==px[2*v+1]
                assert row['quote_price_units']<px[2*v] if row['maker_side']=='buy' else row['quote_price_units']>px[2*v+1];arrived+=1
            for key,e,side in hedge_at.get(ns,[]):
                st=active[key];assert valid and px[side]==e['price_units'];level=st['budget'][side][px[side]*scale];assert level[0]==display[side]
                ready=sum(x['qty'] for x in bykey[key] if x['type']=='maker_fill' and x['ns']+LAT<=ns)-st['hedged']
                q=min(ready,(level[0]-level[1])//4)//10*10;q=min(q,max(0,250*10**pd-st['notional'])//px[side]//10*10)
                assert e['qty']==q;level[1]+=q*4;assert level[0]-level[1]==e['budget_remaining_quarter'];st['hedged']+=q;st['notional']+=q*px[side];hedged+=1
            for order in cohort_orders.get(ns,[]):
                side=2*(0 if order['venue']=='bybit' else 1)+(0 if order['side']=='buy' else 1)
                q=int(D(order['qty']));price=int(D(order['price'])*10**pd);key=price*scale
                assert valid and price==px[side]
                assert 4*q<=global_display[side][key]-global_debt[side].get(key,0)
                global_debt[side][key]=global_debt[side].get(key,0)+4*q;cohort_order_checks+=1
            active={k:s for k,s in active.items() if s['last_ns']>ns}
    assert arrived==sum(e['type']=='quote_arrival' for e in events);assert hedged==counters['hedge_fills']
    assert cohort_order_checks==sum(map(len,cohort_orders.values()))
    scenarios={}
    for name in ('fixed_ticks_1','fixed_ticks_5','as_inside'):
        selected=[r for r in rows if r['scenario_id']==name];m=meta['scenarios'][name]
        assert len(selected)==m['quotes']==len(snapshots)
        calc={'quotes_rejected':sum(r['state']=='REJECTED' for r in selected),
              'quotes_with_maker_fill':sum(r['maker_qty']>0 for r in selected),
              'quotes_with_any_hedge':sum(r['hedged_qty']>0 for r in selected),
              'quotes_fully_hedged':sum(r['maker_qty']>0 and r['unhedged_qty']==0 for r in selected),
              'quotes_with_unhedged_inventory':sum(r['unhedged_qty']>0 for r in selected),
              'maker_qty':sum(r['maker_qty'] for r in selected),'hedged_qty':sum(r['hedged_qty'] for r in selected),
              'unhedged_qty':sum(r['unhedged_qty'] for r in selected),
              'reasons':dict(Counter(r['reason'] for r in selected))}
        for field,value in calc.items():assert m[field]==value,(name,field)
        adv=sum((D(r['entry_cost_advantage_on_hedged_qty']) for r in selected),D(0))
        paired=sum((r['hedged_qty']*D(snapshots[r['cycle']]['prices'][0 if snapshots[r['cycle']]['long']=='bybit' else 2])*tick for r in selected),D(0))
        fixed=sum((r['target_qty']*D(snapshots[r['cycle']]['prices'][0 if snapshots[r['cycle']]['long']=='bybit' else 2])*tick for r in selected),D(0))
        calc.update(entry_advantage_usdt=str(adv),paired_reference_notional_usdt=str(paired),
                    all_candidate_reference_notional_usdt=str(fixed),
                    entry_advantage_bps_on_paired_notional=str(10000*adv/paired) if paired else None,
                    fixed_cohort_normalized_advantage_bps_not_pnl=str(10000*adv/fixed) if fixed else None)
        scenarios[name]=calc
    return {'status':'PASS','date_utc':meta['date_utc'],'method':'Independent quote/sigma/fee/inventory formulas, native trade tuple matching and sequential exact depth-budget reconstruction; no production imports',
      'checks':dict(counters),'arrivals_verified':arrived,'cohort_order_depth_checks':cohort_order_checks,'scenarios':scenarios,
      'max_source_receive_lag_ns':maxlag,'summary_sha256':digest(root/'summary.json'),'audit_code_sha256':digest(__file__),
      'elapsed_seconds':time.time()-started,'limits':'Audits hypothetical entry execution only; queue truth and full portfolio PnL are not established.'}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--replay',required=True);p.add_argument('--output',required=True);a=p.parse_args();report=audit(a.replay)
    with open(a.output,'x') as f:json.dump(report,f,indent=2);f.write('\n')
    print(json.dumps(report),flush=True)
