"""Counterparty-offset maker quotes with unchanged audited execution mechanics.

Sell limit = own bid + d; buy limit = own ask - d. d is either positive integer
price ticks or ceil(own spread ticks * a fraction in (0,1)). An offset without a
strictly interior price is rejected, never clipped. No orders are sent.
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
import time
import maker_engine as base

# The inherited execution contract is immutable and verified before every CLI run.
EXPECTED_BASE_SHA256='3bb67c19de4c1821e471758b82dd878e37d9a8b82df0dc66abf786954d2116fc'
META={'scenario_id','pricing_mode','counterparty_offset_ticks','counterparty_spread_fraction','effective_offset_ticks'}

@dataclass(frozen=True)
class PricingSpec:
    mode:str
    value:str
    scenario_id:str

    @classmethod
    def parse(cls,text):
        try:mode,raw=text.split(':',1)
        except ValueError:raise ValueError('pricing spec must be ticks:N or fraction:F') from None
        if mode=='ticks':
            if not re.fullmatch(r'[0-9]+',raw) or int(raw)<1:raise ValueError('offset ticks must be a positive integer')
            value=str(int(raw));return cls(mode,value,'ticks_'+value)
        if mode=='fraction':
            try:d=Decimal(raw)
            except InvalidOperation:raise ValueError('invalid spread fraction') from None
            if not d.is_finite() or not 0<d<1:raise ValueError('spread fraction must be in (0,1)')
            value=format(d.normalize(),'f');return cls(mode,value,'fraction_'+value.replace('.','p'))
        raise ValueError('unknown pricing mode')

    def distance(self,spread):
        if self.mode=='ticks':return int(self.value)
        f=Fraction(self.value);return (spread*f.numerator+f.denominator-1)//f.denominator

    def metadata(self,spread=None):
        return {'scenario_id':self.scenario_id,'pricing_mode':self.mode,
          'counterparty_offset_ticks':int(self.value) if self.mode=='ticks' else None,
          'counterparty_spread_fraction':self.value if self.mode=='fraction' else None,
          'effective_offset_ticks':None if spread is None else self.distance(spread)}


class OffsetShadow(base.Shadow):
    def __init__(self,snapshot,spec):
        self.spec=spec;self.distance=None
        # Retain all budget, inventory, fee and event-timing machinery. Replace only
        # the quote request initialized by the historical midpoint constructor.
        super().__init__(snapshot,50)
        self.events=[];self.state='PENDING';self.reason=''
        ask,bid=self.reference[2*self.maker:2*self.maker+2];spread=ask-bid
        self.distance=spec.distance(spread)
        self.quote=ask-self.distance if self.side=='buy' else bid+self.distance
        if spread<=1:
            self.state='REJECTED';self.reason='no_legal_inside_tick'
        elif not 1<=self.distance<spread:
            self.state='REJECTED';self.reason='offset_not_strictly_inside'
        elif self.quote*self.target>25_000_000 or self.quote*self.target<500_000:
            self.state='REJECTED';self.reason='maker_order_notional_out_of_bounds'
        self.event('quote_request',self.signal_ns,quote_ticks=self.quote,target_qty=self.target,
          arrival_target_ns=self.arrival_target,side=self.side,maker_venue=self.maker,reason=self.reason)
        if self.state=='REJECTED':self.event('quote_rejected',self.signal_ns,reason=self.reason)

    def event(self,kind,ns,**fields):
        super().event(kind,ns,**fields)
        row=self.events[-1];row.pop('alpha_percent',None)
        row.update(self.spec.metadata())
        row['effective_offset_ticks']=self.distance

    def result(self):
        r=super().result();r.pop('alpha_percent',None)
        r.update(self.spec.metadata());r['effective_offset_ticks']=self.distance
        return r


def midpoint_parity(new_rows,new_events,old_rows,old_events):
    nr={r['cycle']:r for r in new_rows if r.get('scenario_id')=='fraction_0p5'}
    old={r['cycle']:r for r in old_rows if r.get('alpha_percent')==50}
    if set(nr)!=set(old):raise ValueError('midpoint cohort differs')
    placeholders=0;checks=0
    for cycle,new in nr.items():
        prior=old[cycle]
        skip_placeholder=(prior['state']==new['state']=='REJECTED' and prior['reason']==new['reason']=='no_legal_inside_tick' and prior['maker_qty']==new['maker_qty']==0)
        def clean(row,placeholder=False):
            out={k:v for k,v in row.items() if k not in META and k!='alpha_percent'}
            if placeholder:out.pop('quote_ticks',None)
            return out
        if clean(new,skip_placeholder)!=clean(prior,skip_placeholder):raise ValueError(f'midpoint result mismatch cycle {cycle}')
        if skip_placeholder and new['quote_ticks']!=prior['quote_ticks']:placeholders+=1
        ne=[e for e in new_events if e.get('scenario_id')=='fraction_0p5' and e['cycle']==cycle]
        oe=[e for e in old_events if e.get('alpha_percent')==50 and e['cycle']==cycle]
        if [clean(e,skip_placeholder) for e in ne]!=[clean(e,skip_placeholder) for e in oe]:raise ValueError(f'midpoint event mismatch cycle {cycle}')
        checks+=1+len(ne)
    return {'status':'PASS','matched_quote_count':len(nr),'comparison_checks':checks,
      'rejected_placeholder_differences':placeholders,
      'allowed_exception':'Only unused quote_ticks for spread<=1, rejected without any fills. All execution events and economic fields otherwise exact.'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('tape','trades','activity','entry-snapshots','thresholds','gate','protocol','output'):
        p.add_argument('--'+name,required=True)
    p.add_argument('--pricing-specs',default='ticks:1',help='Comma-separated fixed comparisons, e.g. ticks:1,ticks:2,ticks:5,fraction:0.10,fraction:0.25,fraction:0.50')
    p.add_argument('--compare-midpoint',help='Existing immutable maker-q25 result directory; exact economic parity required for fraction:0.50')
    args=p.parse_args()
    if args.gate not in ('off','gateoff','q10','q25'):raise ValueError('unknown activity gate')
    base_hash=base.digest(base.__file__)
    if base_hash!=EXPECTED_BASE_SHA256:raise ValueError('inherited execution engine source changed')
    specs=[PricingSpec.parse(s) for s in args.pricing_specs.split(',')]
    if len({s.scenario_id for s in specs})!=len(specs):raise ValueError('duplicate pricing scenarios')
    if args.compare_midpoint and not any(s.scenario_id=='fraction_0p5' for s in specs):raise ValueError('midpoint comparison requires fraction:0.50')
    json.loads(Path(args.protocol).read_text()) # Protocol is frozen evidence, never executable instructions.
    output=Path(args.output)
    if output.exists():raise FileExistsError('refusing existing output')
    thresholds=base.read_thresholds(args.thresholds,args.gate)
    snapshots={}
    with open(args.entry_snapshots) as f:
        for line in f:
            s=json.loads(line);ns=int(s['entry_ns'])
            if ns in snapshots:raise ValueError('duplicate frozen entry time')
            snapshots[ns]=s
    if not snapshots:raise ValueError('empty frozen entry cohort')
    output.mkdir(parents=True)
    started=time.time();active=[];completed=[];all_events=[];activated=set();book_count=0;trade_count=0;last_ns=0
    trades=iter(base.trade_records(args.trades));tr=next(trades,None)
    with open(output/'events.jsonl','w') as events,open(output/'quotes.jsonl','w') as quotes:
        def drain():
            nonlocal active
            still=[]
            for s in active:
                ready=s.state in ('REJECTED','UNRESOLVED') or (s.state=='DONE' and s.cancel_ack_ns is not None and s.last_ns>=s.cancel_ack_ns+base.NOTIFICATION_GRACE)
                if ready:
                    r=s.result();completed.append(r);quotes.write(json.dumps(r,separators=(',',':'))+'\n')
                    for e in s.events:events.write(json.dumps(e,separators=(',',':'))+'\n');all_events.append(e)
                else:still.append(s)
            active=still
        for tick,ar in base.book_records(args.tape,args.activity):
            while tr is not None and tr.ns<=tick.ns:
                for s in active:s.trade(tr)
                trade_count+=1;last_ns=tr.ns;tr=next(trades,None);drain()
            gate=base.allowed(ar,thresholds)
            if active:
                tick.updates=list(base.UPDATE.iter_unpack(tick.updates))
                for s in active:s.book(tick,gate)
                drain()
            if tick.ns in snapshots:
                if not tick.valid or tick.force or not tick.history or not gate:raise ValueError('frozen cohort contradicts tape or activity gate')
                snapshot=snapshots[tick.ns]
                if list(map(int,snapshot['prices']))!=tick.price:raise ValueError('snapshot prices disagree with tape')
                for spec in specs:active.append(OffsetShadow(snapshot,spec))
                activated.add(tick.ns);drain()
            last_ns=tick.ns;book_count+=1
            if book_count%250000==0:print(json.dumps({'decisions':book_count,'trades':trade_count,'completed':len(completed),'active':len(active),'elapsed_seconds':round(time.time()-started,2)}),flush=True)
        while tr is not None:
            for s in active:s.trade(tr)
            trade_count+=1;last_ns=tr.ns;tr=next(trades,None);drain()
        for s in active:
            s.finish(last_ns)
            if s.state=='DONE' and (s.cancel_ack_ns is None or last_ns<s.cancel_ack_ns+base.NOTIFICATION_GRACE):
                s.state='UNRESOLVED';s.reason='archive_ends_before_notification_grace'
            if s.state=='INCOMPLETE':s.state='UNRESOLVED'
        drain()
    if activated!=set(snapshots):raise ValueError('frozen entry times missing from tape')
    parity=None
    if args.compare_midpoint:
        prior=Path(args.compare_midpoint)
        old_rows=[json.loads(l) for l in (prior/'quotes.jsonl').read_text().splitlines()]
        old_events=[json.loads(l) for l in (prior/'events.jsonl').read_text().splitlines()]
        parity=midpoint_parity(completed,all_events,old_rows,old_events)
        parity['source_quotes_sha256']=base.digest(prior/'quotes.jsonl');parity['source_events_sha256']=base.digest(prior/'events.jsonl')
        (output/'midpoint-equivalence.json').write_text(json.dumps(parity,indent=2)+'\n')
    inputs={name:{'path':getattr(args,name),'sha256':base.digest(getattr(args,name))} for name in ('tape','trades','activity','entry_snapshots','thresholds','protocol')}
    result={'status':'COMPLETED_ORDER_DIAGNOSTIC','version':2,'symbol':'BTWUSDT','execution_enabled':False,
      'portfolio_pnl_computed':False,'gate':args.gate,'source_decisions':book_count,'source_live_trades':trade_count,
      'frozen_cohort_size':len(snapshots),'elapsed_seconds':time.time()-started,'inputs':inputs,
      'code_sha256':base.digest(__file__),'inherited_execution_code':{'path':base.__file__,'sha256':base_hash},
      'fee_rate':'0.0003','default_user_quote':'ticks:1; sell=bid+1tick, buy=ask-1tick',
      'quote_formula':'sell=bid+d, buy=ask-d; d=ticks or ceil(spread_ticks*fraction); reject unless 1<=d<spread_ticks; no clipping',
      'quote_reference':'Own venue confirmed signal-time L1; choose venue with larger own spread, tie Bybit; no repricing before cancel.',
      'hedge_price':'Other venue confirmed ask1 for a buy hedge, bid1 for a sell hedge, at each actual simulated hedge time, at least100ms after maker fill receipt.',
      'strict_trade_through':True,'trade_participation':'0.25','excluded_trade_flags':'RPI and blocktrade',
      'arrival_and_hedge_minimum_latency_ms':100,'cancel_latency_ms':100,'quote_ttl_ms':2000,
      'unhedged_timeout_ms':5000,'cancel_notification_grace_ms':1000,
      'source_clock_guard':'Every trade receive-source lag<=1s; eligible source execution>=actualarrival and<cancel_effective, even if receipt is after cancel.',
      'risk_limits':'Each independent quote reuses original candidate quantity; native maker increments, common10/min5USDT hedge constraints, 25% consumable visible depth,250USDT leg limit. Dust and insufficient-depth inventory retained, no fabricated exit.',
      'limitations':['Independent quote shadows, no shared portfolio or complete exit/funding/strategy PnL.','Strict-through and participation are hypothetical queue assumptions; no real latency, queue priority or impact verification.','Signal-price comparison has no latency-matched taker control; fill cohorts differ between offsets.','Whole-day data previously inspected; fixed comparison grid is descriptive, no profit-based winner selection.'],
      'pricing_specs':[s.metadata() for s in specs],
      'scenarios':{s.scenario_id:base.summarize([r for r in completed if r['scenario_id']==s.scenario_id]) for s in specs},
      'midpoint_equivalence':parity}
    (output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'status':result['status'],'quotes':len(completed),'elapsed_seconds':result['elapsed_seconds']}),flush=True)

if __name__=='__main__':main()
