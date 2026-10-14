"""Fixed-tick versus calibrated AS-inside entry-only replay on a causal cohort.

Keeps every quote independent, with q=0 at entry. This tests quote placement,
not portfolio inventory control or complete arbitrage returns.
"""
from __future__ import annotations
import argparse
import datetime as dt
from decimal import Decimal as D
import json
from pathlib import Path
import time
from as_quote import Calibration,CalibrationError,QuoteParameters,as_quotes
from as_calibrate import load_grid
from rolling_sigma import RollingSigma
from execution_adapter import ExecutionShadow,PriceSchema,book_records,legacy,TERMINAL,NOTIFICATION_GRACE

SCENARIOS=('fixed_ticks_1','fixed_ticks_5','as_inside')


def signal_features(grid,cohort,schema):
    if not len(grid):raise ValueError('empty mid grid')
    start=int(grid[0,0]);targets={int((ns-start)//legacy.NS) for ns in cohort}
    if any(i<0 or i>=len(grid) for i in targets):raise ValueError('cohort outside grid')
    roll=[RollingSigma(60,str(schema.price_unit),'10') for _ in (0,1)];result={}
    for i,row in enumerate(grid):
        vals=[]
        for v in (0,1):vals.append(roll[v].add(int(row[0]),int(row[1+v]),bool(int(row[3])&(1<<v)),int(row[4+v])))
        if i in targets:result[int(row[0])]=vals
    return {ns:result[start+((ns-start)//legacy.NS)*legacy.NS] for ns in cohort}


def quote_for_snapshot(snapshot,scenario,schema,volatility,calibrations,gamma='0.004',horizon='2'):
    prices=list(map(int,snapshot['prices']));maker=0 if prices[0]-prices[1]>=prices[2]-prices[3] else 1
    lng=0 if snapshot['long']=='bybit' else 1;buy=maker==lng;ask,bid=prices[2*maker:2*maker+2]
    venue=('bybit','bitget')[maker];evidence={'scenario_id':scenario,'maker_venue':venue,'inventory_signed_coins':'0','inventory_scope':'independent quote; no empirical inventory skew test'}
    if scenario.startswith('fixed_ticks_'):
        count=int(scenario.rsplit('_',1)[1]);distance=count*schema.native_tick_units[maker]
        evidence.update(counterparty_native_ticks=count,distance_price_units=distance)
        quote=ask-distance if buy else bid+distance
        reason=None
        if ask-bid<=schema.native_tick_units[maker]:reason='no_legal_inside_tick'
        elif not 1<=distance<ask-bid:reason='offset_not_strictly_inside'
        return quote,reason,evidence
    if scenario!='as_inside':raise ValueError('unknown quote scenario')
    fit=calibrations.get(venue)
    if not fit or not fit.get('accepted'):
        evidence['calibration_rejection']=None if fit is None else fit.get('reason')
        return 0,'as_calibration_unaccepted',evidence
    sigma=volatility[maker];evidence['volatility']=sigma
    if not sigma['ready']:return 0,'as_sigma_unavailable',evidence
    try:
        c=Calibration.from_mapping(fit)
        params=QuoteParameters.from_mapping({'symbol':'BTWUSDT','venue':venue,'signal_ns':int(snapshot['entry_ns']),
          'bid_ticks':bid,'ask_ticks':ask,'tick_size':str(schema.price_unit),'native_tick_units':schema.native_tick_units[maker],
          'signed_unhedged_coins':'0','gamma_per_usdt':gamma,'horizon_seconds':horizon,
          'sigma2_usdt2_per_second':sigma['sigma2_usdt2_per_second'],'sigma_known_through_ns':sigma['known_through_ns'],'sigma_ready':True})
        q=as_quotes(params,c);evidence['as']=q
    except CalibrationError as err:
        evidence['calibration_rejection']=str(err);return 0,'as_calibration_unavailable',evidence
    except ValueError as err:
        evidence['quote_error']=str(err);return 0,'as_invalid_quote',evidence
    raw=D(q['raw_buy_price_usdt_per_coin'] if buy else q['raw_sell_price_usdt_per_coin'])
    quote=q['buy_price_ticks'] if buy else q['sell_price_ticks']
    # Preserve the user's inside-spread rule for both raw and exchange-rounded prices.
    if not bid*schema.price_unit<raw<ask*schema.price_unit or not bid<quote<ask:
        return quote,'as_outside_spread',evidence
    return quote,None,evidence


class ResearchShadow(ExecutionShadow):
    def __init__(self,snapshot,scenario,schema,volatility,calibrations,gamma='0.004',horizon='2'):
        quote,reason,self.quote_evidence=quote_for_snapshot(snapshot,scenario,schema,volatility,calibrations,gamma,horizon)
        super().__init__(snapshot,quote,scenario,schema,initial_rejection=reason)
        self.events[0]['quote_evidence']=self.quote_evidence
    def result(self):
        r=super().result();r['quote_evidence']=self.quote_evidence;return r


def compare_fixed(rows,events,old_dir):
    old=Path(old_dir);old_rows=[json.loads(l) for l in (old/'quotes.jsonl').read_text().splitlines()]
    old_events=[json.loads(l) for l in (old/'events.jsonl').read_text().splitlines()]
    ignore={'scenario_id','pricing_mode','counterparty_offset_ticks','counterparty_spread_fraction','effective_offset_ticks','price_schema','quote_evidence'}
    names={'quote_ticks':'quote_price_units','price_ticks':'price_units','own_ask_ticks':'own_ask_price_units','own_bid_ticks':'own_bid_price_units'}
    def clean(r):return {names.get(k,k.replace('_nano','_units')):v for k,v in r.items() if k not in ignore}
    count=0
    for name,prior_name in [('fixed_ticks_1','ticks_1'),('fixed_ticks_5','ticks_5')]:
        new={r['cycle']:r for r in rows if r['scenario_id']==name};prior={r['cycle']:r for r in old_rows if r['scenario_id']==prior_name}
        for cycle,r in new.items():
            if cycle not in prior or clean(r)!=clean(prior[cycle]):raise ValueError(f'fixed quote parity failed {name} cycle{cycle}')
            ne=[clean(e) for e in events if e['scenario_id']==name and e['cycle']==cycle]
            oe=[clean(e) for e in old_events if e['scenario_id']==prior_name and e['cycle']==cycle]
            if ne!=oe:raise ValueError(f'fixed execution event parity failed {name} cycle{cycle}')
            count+=len(ne)+1
    return {'status':'PASS','comparison_checks':count,'evaluation_quotes':sum(r['scenario_id'].startswith('fixed_ticks_') for r in rows),
      'scope':'Exact events and economic fields on same evaluation cohort, allowing only new metadata and generic price/money field names',
      'source_quotes_sha256':legacy.digest(old/'quotes.jsonl'),'source_events_sha256':legacy.digest(old/'events.jsonl')}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('date','tape','trades','activity','grid','entry-snapshots','thresholds','calibration','protocol','output'):p.add_argument('--'+name,required=True)
    p.add_argument('--gate',default='q25',choices=['q25'])
    p.add_argument('--compare-fixed',help='Oct1 immutable counterparty-offset run for exact parity')
    p.add_argument('--cohort-status',choices=['eligible','blocked'],default='eligible')
    p.add_argument('--cohort-reason',default='')
    args=p.parse_args();out=Path(args.output)
    if out.exists():raise FileExistsError('refusing existing output')
    if args.cohort_status=='blocked' and not args.cohort_reason:raise ValueError('blocked cohort requires explicit reason')
    schema=PriceSchema.from_tape(args.tape);protocol=json.loads(Path(args.protocol).read_text())
    gamma=str(protocol['as']['gamma_per_usdt']);horizon=str(protocol['as']['horizon_seconds'])
    if gamma!='0.004' or horizon!='2':raise ValueError('unexpected frozen AS scenario parameters')
    date=dt.datetime.fromisoformat(args.date).replace(tzinfo=dt.timezone.utc);start=int(date.timestamp()*legacy.NS);cut=start+43200*legacy.NS;end=start+86400*legacy.NS
    calibration=json.loads(Path(args.calibration).read_text())
    if calibration['date_utc']!=args.date or calibration['price_schema']!=schema.to_dict():raise ValueError('calibration date or price units mismatch')
    thresholds=legacy.read_thresholds(args.thresholds,args.gate);snapshots={};excluded=0;all_count=0
    with open(args.entry_snapshots) as f:
        for line in f:
            s=json.loads(line);ns=int(s['entry_ns']);all_count+=1
            if not start<=ns<end:raise ValueError('snapshot outside requested day')
            if ns<cut:excluded+=1;continue
            if ns in snapshots:raise ValueError('duplicate entry time')
            schema.validate_snapshot(s);snapshots[ns]=s
    if args.cohort_status=='blocked' and snapshots:raise ValueError('blocked cohort cannot contain evaluation orders')
    grid=load_grid(args.grid)
    if len(grid)!=86400 or int(grid[0,0])!=start:raise ValueError('wrong day mid grid')
    features=signal_features(grid,snapshots,schema);out.mkdir(parents=True)
    with open(out/'signal-features.jsonl','w') as f:
        for ns,vals in features.items():f.write(json.dumps({'ns':ns,'bybit':vals[0],'bitget':vals[1]},separators=(',',':'))+'\n')
    active=[];completed=[];all_events=[];activated=set();bc=tc=0;last_ns=0;started=time.time()
    trades=iter(legacy.trade_records(args.trades));tr=next(trades,None)
    with open(out/'events.jsonl','w') as events,open(out/'quotes.jsonl','w') as quotes:
        def drain():
            nonlocal active
            still=[]
            for s in active:
                ready=s.state in ('REJECTED','UNRESOLVED') or (s.state=='DONE' and s.cancel_ack_ns is not None and s.last_ns>=s.cancel_ack_ns+NOTIFICATION_GRACE)
                if ready:
                    r=s.result();completed.append(r);quotes.write(json.dumps(r,separators=(',',':'))+'\n')
                    for e in s.events:events.write(json.dumps(e,separators=(',',':'))+'\n');all_events.append(e)
                else:still.append(s)
            active=still
        for tick,ar in book_records(args.tape,args.activity):
            while tr is not None and tr.ns<=tick.ns:
                for s in active:s.trade(tr)
                tc+=1;last_ns=tr.ns;tr=next(trades,None);drain()
            gate=legacy.allowed(ar,thresholds)
            if active:
                tick.updates=list(legacy.UPDATE.iter_unpack(tick.updates))
                for s in active:s.book(tick,gate)
                drain()
            if tick.ns in snapshots:
                if not tick.valid or tick.force or not tick.history or not gate:raise ValueError('cohort contradicts current causal gates')
                s=snapshots[tick.ns]
                if list(map(int,s['prices']))!=tick.price:raise ValueError('snapshot and current book mismatch')
                for scenario in SCENARIOS:active.append(ResearchShadow(s,scenario,schema,features[tick.ns],calibration['venues'],gamma,horizon))
                activated.add(tick.ns);drain()
            last_ns=tick.ns;bc+=1
            if bc%250000==0:print(json.dumps({'decisions':bc,'trades':tc,'completed':len(completed),'elapsed_seconds':round(time.time()-started,2)}),flush=True)
        while tr is not None:
            for s in active:s.trade(tr)
            tc+=1;last_ns=tr.ns;tr=next(trades,None);drain()
        for s in active:
            s.finish(last_ns)
            if s.state=='DONE' and (s.cancel_ack_ns is None or last_ns<s.cancel_ack_ns+NOTIFICATION_GRACE):s.state='UNRESOLVED';s.reason='archive_ends_before_notification_grace'
            if s.state=='INCOMPLETE':s.state='UNRESOLVED'
        drain()
    if activated!=set(snapshots):raise ValueError('cohort not fully represented in replay')
    parity=None
    if args.compare_fixed:
        if schema.price_decimals!=5:raise ValueError('old fixed comparison only has 1e-5 schema')
        parity=compare_fixed(completed,all_events,args.compare_fixed)
        (out/'fixed-offset-equivalence.json').write_text(json.dumps(parity,indent=2)+'\n')
    inputs={k:{'path':getattr(args,k),'sha256':legacy.digest(getattr(args,k))} for k in ('tape','trades','activity','grid','entry_snapshots','thresholds','calibration','protocol')}
    source_files=['as_replay.py','as_quote.py','as_calibrate.py','rolling_sigma.py','execution_adapter.py']
    code={f:legacy.digest(Path(__file__).with_name(f)) for f in source_files};code['frozen_maker_engine.py']=legacy.digest(legacy.__file__)
    report={'status':'NO_COMPARABLE_COHORT' if args.cohort_status=='blocked' else 'COMPLETED_AS_QUOTE_DIAGNOSTIC',
      'execution_enabled':False,'portfolio_pnl_computed':False,'date_utc':args.date,'symbol':'BTWUSDT','price_schema':schema.to_dict(),
      'cohort_status':args.cohort_status,'cohort_reason':args.cohort_reason,'full_day_source_cohort':all_count,'excluded_training_candidates':excluded,
      'evaluation_candidate_count':len(snapshots),'evaluation_start_ns':cut,'evaluation_end_ns_exclusive':end,
      'source_decisions':bc,'source_live_trades':tc,'elapsed_seconds':time.time()-started,'inputs':inputs,'code_sha256':code,
      'scenario_parameters':{'gamma_per_usdt':gamma,'horizon_seconds':horizon,'quote_unit_coins':'10','fee_rate':'0.0003','inventory_each_candidate':'0'},
      'calibration_accepted':{v:fit['accepted'] for v,fit in calibration['venues'].items()},
      'scenarios':{name:legacy.summarize([r for r in completed if r['scenario_id']==name]) for name in SCENARIOS},
      'fixed_offset_equivalence':parity,
      'limits':['Independent entry diagnostics, no portfolio/exit/funding PnL.','Each AS quote starts actual net unhedged inventory at zero; inventory control has only deterministic unit coverage.','AS quote outside strict own spread is rejected; no clipping.','Public first-hit intensity is a queue-unverified censored reach proxy.','Daily00-12 calibration and12-24 evaluation is internal chronology, not unseen symbol selection.','Quotes unavailable from bad calibration or invalid sigma are explicit rejections, not zero opportunity evidence.']}
    (out/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'status':report['status'],'evaluation_candidates':len(snapshots),'quotes':len(completed),'elapsed_seconds':report['elapsed_seconds']}),flush=True)

if __name__=='__main__':main()
