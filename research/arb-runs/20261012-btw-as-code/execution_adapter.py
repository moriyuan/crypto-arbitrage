"""Price-schema-compatible copy of the immutable audited maker execution rules.
Only price precision, money units, native tick rounding and quote injection differ.
No API/network/real orders. Source ancestry is recorded by the replay runner.
"""
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import sys
sys.path.insert(0,'/Users/mori/code/crypto/state/arb-runs/20261012-btw-activity-maker-code')
import maker_engine as legacy
NS=legacy.NS
LATENCY=legacy.LATENCY
TTL=legacy.TTL
UNHEDGED_TIMEOUT=legacy.UNHEDGED_TIMEOUT
NOTIFICATION_GRACE=legacy.NOTIFICATION_GRACE
PRICE8=legacy.PRICE8
TERMINAL=legacy.TERMINAL

@dataclass(frozen=True)
class PriceSchema:
    price_decimals:int
    native_tick_units:tuple
    def __post_init__(self):
        if self.price_decimals not in (5,6):raise ValueError('unsupported price precision')
        if self.native_tick_units not in ((1,1),(10,1)):raise ValueError('unsupported native quote increments')
        if (self.price_decimals==5)!=(self.native_tick_units==(1,1)):raise ValueError('price schema mismatch')
    @property
    def price_unit(self):return Decimal(1)/(10**self.price_decimals)
    @property
    def money_decimals(self):return self.price_decimals+4
    @property
    def depth_key_scale(self):return 10**(8-self.price_decimals)
    @property
    def min_notional_units(self):return 5*10**self.price_decimals
    @property
    def max_notional_units(self):return 250*10**self.price_decimals
    def to_dict(self):return {'price_decimals':self.price_decimals,'money_decimals':self.money_decimals,'native_tick_units':list(self.native_tick_units),'depth_key_scale':self.depth_key_scale,'price_unit_usdt':str(self.price_unit)}
    @classmethod
    def from_tape(cls,path):
        with open(path,'rb') as f:magic=f.read(8)
        if magic==b'BTWTP002':return cls(5,(1,1))
        if magic==b'BTWTP003':return cls(6,(10,1))
        raise ValueError('unsupported tape magic')
    def validate_snapshot(self,snapshot):
        if int(snapshot.get('price_decimals',5))!=self.price_decimals:raise ValueError('snapshot price precision mismatch')
        if int(snapshot.get('money_decimals',9))!=self.money_decimals:raise ValueError('snapshot money precision mismatch')
        if tuple(snapshot.get('native_tick_units',(1,1)))!=self.native_tick_units:raise ValueError('snapshot native ticks mismatch')
        if any(int(p)%self.native_tick_units[i//2] for i,p in enumerate(snapshot['prices'])):raise ValueError('snapshot L1 violates native price ticks')

class ExecutionShadow:
    """One price-alpha shadow, with explicit filled, hedged and residual inventory."""
    def __init__(self,snapshot,quote_price_units,scenario_id,price_schema,initial_rejection=None):
        if snapshot.get('after_entry_consumption') is not False or not snapshot.get('pre_entry_consumption'):
            raise ValueError('requires audited pre-entry budget snapshot')
        price_schema.validate_snapshot(snapshot)
        self.cycle=int(snapshot['cycle_id']);self.signal_ns=int(snapshot['entry_ns']);self.scenario_id=scenario_id;self.schema=price_schema
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
        self.quote=int(quote_price_units)
        if initial_rejection:
            self.state='REJECTED';self.reason=initial_rejection
        elif spread<=self.schema.native_tick_units[self.maker]:
            self.state='REJECTED';self.reason='no_legal_inside_tick'
        elif self.quote % self.schema.native_tick_units[self.maker]:
            self.state='REJECTED';self.reason='invalid_native_price_grid'
        elif not (bid<self.quote<ask):
            self.state='REJECTED';self.reason='offset_not_strictly_inside'
        elif self.quote*self.target>self.schema.max_notional_units or self.quote*self.target<self.schema.min_notional_units:
            self.state='REJECTED';self.reason='maker_order_notional_out_of_bounds'
        self.event('quote_request',self.signal_ns,quote_price_units=self.quote,target_qty=self.target,
                   arrival_target_ns=self.arrival_target,side=self.side,maker_venue=self.maker,
                   reason=self.reason)
        if self.state=='REJECTED':self.event('quote_rejected',self.signal_ns,reason=self.reason)

    def amount(self,value,places=None):
        return format(Decimal(value)/(10**(self.schema.money_decimals if places is None else places)),'f')

    @property
    def unhedged_qty(self):return self.maker_qty-self.hedged_qty

    def event(self,kind,ns,**fields):
        self.events.append({'cycle':self.cycle,'scenario_id':self.scenario_id,'type':kind,'ns':ns,**fields})

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
            self.event('quote_arrival',t.ns,latency_ns=t.ns-self.signal_ns,quote_price_units=self.quote,
                       own_ask_price_units=t.price[2*self.maker],own_bid_price_units=t.price[2*self.maker+1])
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
        through=(tr.price8<self.quote*self.schema.depth_key_scale if self.side=='buy' else tr.price8>self.quote*self.schema.depth_key_scale)
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
        self.event('maker_fill',tr.ns,qty=q,price_units=self.quote,fee_units=f,
                   source_ns=tr.source_ns,trade_price_1e8=tr.price8,trade_qty_1e8=tr.qty8,
                   source_sequence=tr.sequence,row_ordinal=tr.ordinal,venue=tr.venue,
                   aggressor=tr.aggressor,hedge_eligible_ns=tr.ns+LATENCY,
                   unhedged_qty=self.unhedged_qty)
        self.cancel(tr.ns,'first_fill')

    def try_hedge(self,t):
        eligible=sum(p['qty'] for p in self.pending if p['ready_ns']<=t.ns)
        if not eligible:return
        hedge_buy=self.hedge==self.lng;side=2*self.hedge+(0 if hedge_buy else 1)
        px=t.price[side];level=self.budget[side].get(px*self.schema.depth_key_scale)
        if level is None or level[0]!=t.display[side]:raise ValueError('hedge L1 budget disagrees with tape')
        q=min(eligible,level[1]//4)//10*10
        if q<=0 or q*px<self.schema.min_notional_units:
            self.counters['hedge_depth_or_minimum_unavailable']+=1;return
        # Protection against a stale oversized order: no one-leg diagnostic order >250 USDT.
        q=min(q,max(0,self.schema.max_notional_units-self.hedge_notional_ticks)//px//10*10)
        if q<=0 or q*px<self.schema.min_notional_units:return
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
                   price_units=px,fee_units=f,allocations=allocations,unhedged_qty=self.unhedged_qty,
                   maker_price_saving_units=maker_saving,hedge_slippage_units=hedge_slippage,
                   entry_price_improvement_units=improvement,entry_fee_delta_units=fee_delta,
                   budget_remaining_quarter=level[1])

    def finish(self,ns):
        self.advance(ns)
        if self.state not in TERMINAL:
            self.state='UNRESOLVED' if self.unhedged_qty else 'INCOMPLETE'
            self.reason='archive_ended';self.event('archive_ended',ns,unhedged_qty=self.unhedged_qty)

    def result(self):
        return {'cycle':self.cycle,'scenario_id':self.scenario_id,'signal_ns':self.signal_ns,
          'long_venue':'bybit' if self.lng==0 else 'bitget','maker_venue':'bybit' if self.maker==0 else 'bitget',
          'maker_side':self.side,'quote_price_units':self.quote,'target_qty':self.target,'state':self.state,'reason':self.reason,
          'arrival_ns':self.arrival_ns,'cancel_reason':self.cancel_reason,'cancel_ack_ns':self.cancel_ack_ns,
          'maker_qty':self.maker_qty,'hedged_qty':self.hedged_qty,'unhedged_qty':self.unhedged_qty,
          'first_fill_ns':self.first_fill_ns,'max_unhedged_qty':self.max_unhedged,'max_hedge_wait_ns':self.max_wait_ns,
          'maker_fees':self.amount(self.maker_fees),'hedge_fees':self.amount(self.hedge_fees),
          'hedge_notional':self.amount(self.hedge_notional_ticks,self.schema.price_decimals),
          'maker_price_saving_on_hedged_qty':self.amount(self.maker_saving),
          'hedge_slippage_on_hedged_qty':self.amount(self.hedge_slippage),
          'entry_price_improvement_on_hedged_qty':self.amount(self.entry_price_improvement),
          'entry_fee_delta_on_hedged_qty':self.amount(self.entry_fee_delta),
          'entry_cost_advantage_on_hedged_qty':self.amount(self.entry_price_improvement-self.entry_fee_delta),
          'counters':dict(self.counters),'event_count':len(self.events),'price_schema':self.schema.to_dict()}



def book_records(path,activity_path):
    with open(path,'rb') as f,open(activity_path,'rb') as a:
        if f.read(8) not in (b'BTWTP002',b'BTWTP003') or a.read(8)!=b'BTWACT01':raise ValueError('tape magic mismatch')
        previous=-1
        while h:=f.read(legacy.HEADER.size):
            if len(h)!=legacy.HEADER.size:raise ValueError('truncated book header')
            ns,valid,force,hist,pad,n,*vals=legacy.HEADER.unpack(h)
            if ns<=previous or valid not in (0,1) or force not in (0,1,2) or hist not in (0,1) or pad or n>1000 or (not valid and n):raise ValueError('bad book header')
            previous=ns;raw=f.read(n*legacy.UPDATE.size)
            if len(raw)!=n*legacy.UPDATE.size:raise ValueError('truncated updates')
            ah=a.read(legacy.ACTIVITY.size)
            if len(ah)!=legacy.ACTIVITY.size:raise ValueError('truncated activity')
            ar=legacy.ACTIVITY.unpack(ah)
            if ar[0]!=ns:raise ValueError('activity and book clocks differ')
            yield legacy.Tick(ns,bool(valid),force,bool(hist),vals[:4],vals[4:],raw),ar
        if a.read(1):raise ValueError('activity has excess records')
