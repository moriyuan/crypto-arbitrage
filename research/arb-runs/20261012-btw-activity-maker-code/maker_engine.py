"""Public historical order-level maker/hedge diagnostic; never a portfolio or exchange client.

Each frozen taker entry is an independent shadow. Public L2 cannot establish
queue priority or counterfactual liquidity: strict trade-through fills are an
explicit assumption, not historical executions. Existing tape prices are ticks
of 1e-5 USDT; depth keys and public trade prices are 1e-8 USDT.
"""
from __future__ import annotations
import argparse
from collections import Counter
import csv
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import struct
import time

NS=1_000_000_000
LATENCY=100_000_000
TTL=2*NS
UNHEDGED_TIMEOUT=5*NS
NOTIFICATION_GRACE=NS
PRICE8=100_000_000
TERMINAL={'DONE','REJECTED','UNRESOLVED'}
HEADER=struct.Struct('<qBBBBI8q')
UPDATE=struct.Struct('<B7xqq')
TRADE=struct.Struct('<qqBB6xqqqII8x')
ACTIVITY=struct.Struct('<qIIqqqqQ')

@dataclass
class Tick:
    ns:int
    valid:bool
    force:int
    history:bool
    price:list[int]
    display:list[int]
    updates:list[tuple[int,int,int]]

@dataclass
class Trade:
    ns:int
    source_ns:int
    venue:int
    aggressor:int
    price8:int
    qty8:int
    sequence:int
    ordinal:int
    flags:int


def amount(n, places=9):
    return format(Decimal(n)/(10**places),'f')


def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()


class Shadow:
    """One price-alpha shadow, with explicit filled, hedged and residual inventory."""
    def __init__(self,snapshot,alpha):
        if snapshot.get('after_entry_consumption') is not False or not snapshot.get('pre_entry_consumption'):
            raise ValueError('requires audited pre-entry budget snapshot')
        if alpha not in (25,50,75):raise ValueError('alpha must be 25, 50 or 75 percent')
        self.cycle=int(snapshot['cycle_id']);self.signal_ns=int(snapshot['entry_ns']);self.alpha=alpha
        self.lng=0 if snapshot['long']=='bybit' else 1
        self.target=int(snapshot['qty']);self.reference=list(map(int,snapshot['prices']))
        if self.target<=0 or self.target%10:raise ValueError('candidate quantity must be common lot 10')
        self.budget=[{int(x['price_units_1e8']):[int(x['displayed_qty']),int(x['remaining_quarter'])] for x in side} for side in snapshot['sides']]
        self.maker=0 if self.reference[0]-self.reference[1]>=self.reference[2]-self.reference[3] else 1
        self.side='buy' if self.maker==self.lng else 'sell';self.hedge=1-self.maker
        self.state='PENDING';self.reason='';self.events=[];self.counters=Counter()
        self.arrival_target=self.signal_ns+LATENCY;self.arrival_ns=None;self.ttl_ns=None
        self.cancel_due=None;self.cancel_reason='';self.cancel_ack_ns=None
        self.maker_qty=0;self.hedged_qty=0;self.pending=[];self.first_fill_ns=None
        self.last_ns=self.signal_ns;self.max_unhedged=0;self.max_wait_ns=0
        self.maker_fees=0;self.hedge_fees=0;self.hedge_notional_ticks=0;self.entry_price_improvement=0;self.entry_fee_delta=0;self.maker_saving=0;self.hedge_slippage=0
        ask,bid=self.reference[2*self.maker:2*self.maker+2];spread=ask-bid
        step=max(1,min(spread-1,spread*alpha//100))
        self.quote=bid+step if self.side=='buy' else ask-step
        if spread<=1 or not (bid<self.quote<ask):
            self.state='REJECTED';self.reason='no_legal_inside_tick'
        elif self.quote*self.target>25_000_000 or self.quote*self.target<500_000:
            self.state='REJECTED';self.reason='maker_order_notional_out_of_bounds'
        self.event('quote_request',self.signal_ns,quote_ticks=self.quote,target_qty=self.target,
                   arrival_target_ns=self.arrival_target,side=self.side,maker_venue=self.maker,
                   reason=self.reason)
        if self.state=='REJECTED':self.event('quote_rejected',self.signal_ns,reason=self.reason)

    @property
    def unhedged_qty(self):return self.maker_qty-self.hedged_qty

    def event(self,kind,ns,**fields):
        self.events.append({'cycle':self.cycle,'alpha_percent':self.alpha,'type':kind,'ns':ns,**fields})

    def cancel(self,ns,reason):
        if self.cancel_due is None and self.state not in TERMINAL:
            self.cancel_due=ns+LATENCY;self.cancel_reason=reason
            self.event('cancel_request',ns,reason=reason,effective_ns=self.cancel_due)

    def advance(self,ns):
        if ns<self.last_ns:raise ValueError('shadow clock went backwards')
        self.last_ns=ns
        if self.state in TERMINAL:return
        if self.ttl_ns is not None and self.ttl_ns<=ns and self.cancel_due is None:self.cancel(self.ttl_ns,'ttl')
        if self.cancel_due is not None and self.cancel_due<=ns and self.cancel_ack_ns is None:
            self.cancel_ack_ns=self.cancel_due
            self.event('cancel_ack',self.cancel_due,reason=self.cancel_reason,unfilled_qty=self.target-self.maker_qty)
        if self.pending:
            age=ns-self.pending[0]['fill_ns'];self.max_wait_ns=max(self.max_wait_ns,age)
            if age>=UNHEDGED_TIMEOUT:
                self.state='UNRESOLVED';self.reason='unhedged_timeout'
                self.event('unhedged_timeout',self.pending[0]['fill_ns']+UNHEDGED_TIMEOUT,
                           qty=self.unhedged_qty,oldest_fill_ns=self.pending[0]['fill_ns'])
                return
        if self.cancel_ack_ns is not None and not self.unhedged_qty:
            self.state='DONE';self.reason='fully_hedged' if self.maker_qty else 'unfilled_cancelled'

    def book(self,t,gate):
        self.advance(t.ns)
        if self.state in ('REJECTED','UNRESOLVED'):return
        if t.valid:
            for side,p,q in t.updates:
                if not q:self.budget[side].pop(p,None);continue
                old,rem=self.budget[side].get(p,[0,0]);self.budget[side][p]=[q,max(0,rem+q-old)]
        if self.state=='DONE':return # Keep budgets current during late-notification grace.
        if not gate:self.cancel(t.ns,'inactive')
        if t.force:self.cancel(t.ns,'funding_guard' if t.force==1 else 'day_end')
        if self.state=='PENDING' and t.ns>=self.arrival_target:
            if not t.valid:
                self.state='REJECTED';self.reason='unknown_book_at_arrival';self.event('quote_rejected',t.ns,reason=self.reason);return
            crossing=self.quote>=t.price[2*self.maker] if self.side=='buy' else self.quote<=t.price[2*self.maker+1]
            if crossing:
                self.state='REJECTED';self.reason='post_only_would_cross';self.event('quote_rejected',t.ns,reason=self.reason);return
            self.state='LIVE';self.arrival_ns=t.ns;self.ttl_ns=t.ns+TTL
            self.event('quote_arrival',t.ns,latency_ns=t.ns-self.signal_ns,quote_ticks=self.quote,
                       own_ask_ticks=t.price[2*self.maker],own_bid_ticks=t.price[2*self.maker+1])
        if t.valid and self.unhedged_qty:self.try_hedge(t)
        self.advance(t.ns)

    def trade(self,tr):
        self.advance(tr.ns)
        if self.state not in ('LIVE','DONE') or self.arrival_ns is None or tr.venue!=self.maker:return
        if self.cancel_due is not None and tr.source_ns>=self.cancel_due:return
        if tr.flags:
            self.counters['excluded_rpi_or_block_trade']+=1;return
        if tr.source_ns<self.arrival_ns:
            self.counters['source_execution_before_arrival']+=1;return
        if tr.source_ns>tr.ns:raise ValueError('trade source time in future')
        correct=(tr.aggressor==1 if self.side=='buy' else tr.aggressor==0)
        through=(tr.price8<self.quote*1000 if self.side=='buy' else tr.price8>self.quote*1000)
        if not correct or not through:return
        quantum=10 if self.maker==0 else 1
        q=min(self.target-self.maker_qty,(tr.qty8//(4*PRICE8*quantum))*quantum)
        if q<=0:
            self.counters['trade_participation_below_native_increment']+=1;return
        if self.state=='DONE':
            self.state='LIVE';self.reason=''
            self.event('late_fill_notification_reopens_inventory',tr.ns,source_ns=tr.source_ns,cancel_effective_ns=self.cancel_due)
        if self.first_fill_ns is None:self.first_fill_ns=tr.ns
        f=3*self.quote*q
        self.maker_qty+=q;self.maker_fees+=f
        self.pending.append({'qty':q,'fill_ns':tr.ns,'ready_ns':tr.ns+LATENCY})
        self.max_unhedged=max(self.max_unhedged,self.unhedged_qty)
        self.event('maker_fill',tr.ns,qty=q,price_ticks=self.quote,fee_nano=f,
                   source_ns=tr.source_ns,trade_price_1e8=tr.price8,trade_qty_1e8=tr.qty8,
                   source_sequence=tr.sequence,row_ordinal=tr.ordinal,venue=tr.venue,
                   aggressor=tr.aggressor,hedge_eligible_ns=tr.ns+LATENCY,
                   unhedged_qty=self.unhedged_qty)
        self.cancel(tr.ns,'first_fill')

    def try_hedge(self,t):
        eligible=sum(p['qty'] for p in self.pending if p['ready_ns']<=t.ns)
        if not eligible:return
        hedge_buy=self.hedge==self.lng;side=2*self.hedge+(0 if hedge_buy else 1)
        px=t.price[side];level=self.budget[side].get(px*1000)
        if level is None or level[0]!=t.display[side]:raise ValueError('hedge L1 budget disagrees with tape')
        q=min(eligible,level[1]//4)//10*10
        if q<=0 or q*px<500_000:
            self.counters['hedge_depth_or_minimum_unavailable']+=1;return
        # Protection against a stale oversized order: no one-leg diagnostic order >250 USDT.
        q=min(q,max(0,25_000_000-self.hedge_notional_ticks)//px//10*10)
        if q<=0 or q*px<500_000:return
        remaining=q;allocations=[]
        for p in self.pending:
            if not remaining:break
            if p['ready_ns']>t.ns:continue
            used=min(remaining,p['qty']);remaining-=used;p['qty']-=used
            wait=t.ns-p['fill_ns'];self.max_wait_ns=max(self.max_wait_ns,wait)
            allocations.append({'qty':used,'fill_ns':p['fill_ns'],'latency_ns':wait})
        if remaining:raise AssertionError('hedged immature inventory')
        self.pending=[p for p in self.pending if p['qty']]
        level[1]-=q*4;self.hedged_qty+=q;self.hedge_notional_ticks+=q*px
        f=3*px*q;self.hedge_fees+=f
        original_maker=self.reference[2*self.maker+(0 if self.side=='buy' else 1)]
        original_hedge=self.reference[side]
        maker_saving=q*10000*(original_maker-self.quote)*(1 if self.side=='buy' else -1)
        hedge_slippage=q*10000*(px-original_hedge)*(1 if hedge_buy else -1)
        self.maker_saving+=maker_saving;self.hedge_slippage+=hedge_slippage
        improvement=maker_saving-hedge_slippage
        fee_delta=3*q*(self.quote+px-original_maker-original_hedge)
        self.entry_price_improvement+=improvement;self.entry_fee_delta+=fee_delta
        self.event('hedge_fill',t.ns,venue=self.hedge,side='buy' if hedge_buy else 'sell',qty=q,
                   price_ticks=px,fee_nano=f,allocations=allocations,unhedged_qty=self.unhedged_qty,
                   maker_price_saving_nano=maker_saving,hedge_slippage_nano=hedge_slippage,
                   entry_price_improvement_nano=improvement,entry_fee_delta_nano=fee_delta,
                   budget_remaining_quarter=level[1])

    def finish(self,ns):
        self.advance(ns)
        if self.state not in TERMINAL:
            self.state='UNRESOLVED' if self.unhedged_qty else 'INCOMPLETE'
            self.reason='archive_ended';self.event('archive_ended',ns,unhedged_qty=self.unhedged_qty)

    def result(self):
        return {'cycle':self.cycle,'alpha_percent':self.alpha,'signal_ns':self.signal_ns,
          'long_venue':'bybit' if self.lng==0 else 'bitget','maker_venue':'bybit' if self.maker==0 else 'bitget',
          'maker_side':self.side,'quote_ticks':self.quote,'target_qty':self.target,'state':self.state,'reason':self.reason,
          'arrival_ns':self.arrival_ns,'cancel_reason':self.cancel_reason,'cancel_ack_ns':self.cancel_ack_ns,
          'maker_qty':self.maker_qty,'hedged_qty':self.hedged_qty,'unhedged_qty':self.unhedged_qty,
          'first_fill_ns':self.first_fill_ns,'max_unhedged_qty':self.max_unhedged,'max_hedge_wait_ns':self.max_wait_ns,
          'maker_fees':amount(self.maker_fees),'hedge_fees':amount(self.hedge_fees),
          'hedge_notional':amount(self.hedge_notional_ticks,5),
          'maker_price_saving_on_hedged_qty':amount(self.maker_saving),
          'hedge_slippage_on_hedged_qty':amount(self.hedge_slippage),
          'entry_price_improvement_on_hedged_qty':amount(self.entry_price_improvement),
          'entry_fee_delta_on_hedged_qty':amount(self.entry_fee_delta),
          'entry_cost_advantage_on_hedged_qty':amount(self.entry_price_improvement-self.entry_fee_delta),
          'counters':dict(self.counters),'event_count':len(self.events)}


def summarize(rows):
    advantages=[Decimal(r['entry_cost_advantage_on_hedged_qty']) for r in rows if r['hedged_qty']]
    return {'execution_enabled':False,'portfolio_pnl_computed':False,'quotes':len(rows),
      'quotes_rejected':sum(r['state']=='REJECTED' for r in rows),
      'quotes_with_maker_fill':sum(r['maker_qty']>0 for r in rows),
      'quotes_with_any_hedge':sum(r['hedged_qty']>0 for r in rows),
      'quotes_fully_hedged':sum(r['maker_qty']>0 and r['unhedged_qty']==0 for r in rows),
      'quotes_with_unhedged_inventory':sum(r['unhedged_qty']>0 for r in rows),
      'quotes_with_partial_target_fill':sum(0<r['maker_qty']<r['target_qty'] for r in rows),
      'reasons':dict(Counter(r['reason'] for r in rows)),
      'maker_qty':sum(r['maker_qty'] for r in rows),'hedged_qty':sum(r['hedged_qty'] for r in rows),
      'unhedged_qty':sum(r['unhedged_qty'] for r in rows),
      'quotes_with_positive_entry_cost_advantage':sum(x>0 for x in advantages),
      'mean_entry_cost_advantage_per_hedged_quote':str(sum(advantages)/len(advantages)) if advantages else None,
      'interpretation':'Independent entry diagnostics. No exit, complete funding ledger, net strategy return or portfolio aggregation.'}


def book_records(path,activity_path):
    with open(path,'rb') as f,open(activity_path,'rb') as a:
        if f.read(8)!=b'BTWTP002' or a.read(8)!=b'BTWACT01':raise ValueError('tape magic mismatch')
        previous=-1
        while h:=f.read(HEADER.size):
            if len(h)!=HEADER.size:raise ValueError('truncated book header')
            ns,valid,force,hist,pad,n,*vals=HEADER.unpack(h)
            if ns<=previous or valid not in (0,1) or force not in (0,1,2) or hist not in (0,1) or pad or n>1000 or (not valid and n):raise ValueError('bad book header')
            previous=ns
            raw=f.read(n*UPDATE.size)
            if len(raw)!=n*UPDATE.size:raise ValueError('truncated updates')
            ah=a.read(ACTIVITY.size)
            if len(ah)!=ACTIVITY.size:raise ValueError('truncated activity')
            ar=ACTIVITY.unpack(ah)
            if ar[0]!=ns:raise ValueError('activity and book clocks differ')
            # Delay depth parsing until an active shadow needs it.
            yield Tick(ns,bool(valid),force,bool(hist),vals[:4],vals[4:],raw),ar
        if a.read(1):raise ValueError('activity has excess records')


def trade_records(path):
    with open(path,'rb') as f:
        if f.read(8)!=b'BTWTRD01':raise ValueError('trade tape magic mismatch')
        previous=-1
        while raw:=f.read(TRADE.size):
            if len(raw)!=TRADE.size:raise ValueError('truncated trade')
            tr=Trade(*TRADE.unpack(raw))
            if tr.ns<previous or tr.venue not in (0,1) or tr.aggressor not in (0,1) or tr.qty8<=0 or tr.price8<=0 or tr.source_ns>tr.ns:raise ValueError('invalid trade')
            if tr.ns-tr.source_ns>NOTIFICATION_GRACE:raise ValueError('source lag exceeds cancellation notification grace; cannot certify fills')
            previous=tr.ns;yield tr


def read_thresholds(path,gate):
    with open(path,newline='') as f:
        reader=csv.DictReader(f)
        expected=['name','countA','countB','turnoverAunits','turnoverBunits','maxAgeNs']
        if reader.fieldnames!=expected:raise ValueError('threshold schema mismatch')
        rows=list(reader)
    if {r['name'] for r in rows}!={'q10','q25'} or len(rows)!=2:raise ValueError('two unique threshold gates required')
    values={r['name']:[int(r[k]) for k in expected[1:]] for r in rows}
    if any(v<0 for vals in values.values() for v in vals):raise ValueError('negative thresholds')
    return None if gate in ('off','gateoff') else values[gate]


def allowed(ar,thresholds):
    if thresholds is None:return True
    _,ca,cb,ta,tb,aa,ab,warm=ar
    qa,qb,va,vb,maxage=thresholds
    return warm&3==3 and ca>=qa and cb>=qb and ta>=va and tb>=vb and 0<=aa<=maxage and 0<=ab<=maxage


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--tape',required=True);p.add_argument('--trades',required=True);p.add_argument('--activity',required=True)
    p.add_argument('--entry-snapshots',required=True);p.add_argument('--thresholds',required=True)
    p.add_argument('--gate',choices=['off','gateoff','q10','q25'],required=True);p.add_argument('--output',required=True)
    args=p.parse_args();out=Path(args.output)
    if out.exists():raise FileExistsError('refusing existing output')
    thresholds=read_thresholds(args.thresholds,args.gate)
    snapshots={}
    with open(args.entry_snapshots) as f:
        for line in f:
            snap=json.loads(line);ns=int(snap['entry_ns'])
            if ns in snapshots:raise ValueError('duplicate entry time')
            snapshots[ns]=snap
    if not snapshots:raise ValueError('empty frozen entry cohort')
    out.mkdir(parents=True)
    started=time.time();active=[];completed=[];activated=set();book_count=0;trade_count=0;last_ns=0
    trades=iter(trade_records(args.trades));tr=next(trades,None)
    with open(out/'events.jsonl','w') as events,open(out/'quotes.jsonl','w') as quotes:
        def drain():
            nonlocal active
            still=[]
            for s in active:
                ready=s.state in ('REJECTED','UNRESOLVED') or (s.state=='DONE' and s.cancel_ack_ns is not None and s.last_ns>=s.cancel_ack_ns+NOTIFICATION_GRACE)
                if ready:
                    r=s.result();completed.append(r);quotes.write(json.dumps(r,separators=(',',':'))+'\n')
                    for e in s.events:events.write(json.dumps(e,separators=(',',':'))+'\n')
                else:still.append(s)
            active=still
        for tick,ar in book_records(args.tape,args.activity):
            # Trades at the same receive-ns are available before the batched book decision.
            # A new maker order still waits at least 100ms before arrival.
            while tr is not None and tr.ns<=tick.ns:
                for s in active:s.trade(tr)
                trade_count+=1;last_ns=tr.ns;tr=next(trades,None)
                drain()
            gate=allowed(ar,thresholds)
            if active:
                tick.updates=list(UPDATE.iter_unpack(tick.updates))
                for s in active:s.book(tick,gate)
                drain()
            if tick.ns in snapshots:
                if not tick.valid or tick.force or not tick.history or not gate:raise ValueError('entry cohort contradicts tape / frozen gate')
                snap=snapshots[tick.ns]
                if list(map(int,snap['prices']))!=tick.price:raise ValueError('snapshot prices mismatch')
                for alpha in (25,50,75):active.append(Shadow(snap,alpha))
                activated.add(tick.ns);drain()
            last_ns=tick.ns;book_count+=1
            if book_count%250000==0:print(json.dumps({'decisions':book_count,'trades':trade_count,'completed':len(completed),'active':len(active),'elapsed':round(time.time()-started,2)}),flush=True)
        while tr is not None:
            for s in active:s.trade(tr)
            trade_count+=1;last_ns=tr.ns;tr=next(trades,None);drain()
        for s in active:
            s.finish(last_ns)
            if s.state=='DONE' and (s.cancel_ack_ns is None or last_ns<s.cancel_ack_ns+NOTIFICATION_GRACE):
                s.state='UNRESOLVED';s.reason='archive_ends_before_notification_grace'
            if s.state=='INCOMPLETE':s.state='UNRESOLVED'
        drain()
    if activated!=set(snapshots):raise ValueError('cohort has entry times missing from tape')
    inputs={k:{'path':getattr(args,k),'sha256':digest(getattr(args,k))} for k in ('tape','trades','activity','entry_snapshots','thresholds')}
    result={'status':'COMPLETED_ORDER_DIAGNOSTIC','version':1,'symbol':'BTWUSDT','execution_enabled':False,
      'portfolio_pnl_computed':False,'gate':args.gate,'source_decisions':book_count,'source_live_trades':trade_count,
      'frozen_cohort_size':len(snapshots),'elapsed_seconds':time.time()-started,'inputs':inputs,
      'code_sha256':digest(__file__),'fee_rate':'0.0003','strict_trade_through':True,'trade_participation':'0.25',
      'quote_rounding':'floor spread*fraction ticks from own-side best, clamped to strictly inside own spread',
      'excluded_trade_flags':'RPI or blocktrade; no inaccessible liquidity credited',
      'arrival_and_hedge_minimum_latency_ms':100,'arrival_and_hedge_clock':'first authoritative book decision at or after latency; invalid arrival rejected',
      'cancel_latency_ms':100,'quote_ttl_ms':2000,'unhedged_timeout_ms':5000,
      'cancel_fill_notification_grace_ms':1000,'late_notification_guard':'Every archive receive-source lag must be <=1s; receive after cancel remains eligible when source execution preceded cancel.',
      'queue_assumption':'No queue model. Correct-side ordinary public trade strictly through quote after arrival source time; participation limited to 25% and native lot increment. Counterfactual fill assumption, not proof of fill.',
      'important_limits':['Independent frozen-entry shadows; simultaneous alpha variants do not share liquidity or accounts.','No market-impact model or complete position exit/funding model; entry advantage is not profit.','Trade source versus local-receive clocks assumed aligned; no measured live latency.','Partial maker fills below common hedge lot/minimum remain real hypothetical exposure, never rounded away.','Unhedged inventory freezes after 5s; no fabricated emergency liquidation.'],
      'scenarios':{str(a):summarize([r for r in completed if r['alpha_percent']==a]) for a in (25,50,75)}}
    (out/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'status':result['status'],'quotes':len(completed),'elapsed':result['elapsed_seconds']}),flush=True)

if __name__=='__main__':main()
