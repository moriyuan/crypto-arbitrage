"""Independent fixed-offset maker/hedge opportunities with real residual ledgers.

Public offline research only. Paired exits assume simultaneous equal-quantity
fills at both currently visible opposing L1 prices; no portfolio aggregation.
Frozen input readers/precision are reused; old entry-only terminal logic is not.
"""
from collections import Counter
from dataclasses import dataclass,asdict
from decimal import Decimal
import heapq
import sys

sys.path.insert(0,'/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-code')
from execution_adapter import PriceSchema,book_records,legacy

NS=legacy.NS;LATENCY=legacy.LATENCY;TTL=legacy.TTL;NOTIFICATION_GRACE=legacy.NOTIFICATION_GRACE
VENUES=('bybit','bitget')

@dataclass(frozen=True)
class LifecyclePolicy:
    unhedged_timeout_ms:int=1000
    hedged_hold_ms:int=60000
    counterparty_offset_ticks:int=1
    paired_take_profit_bps:int=5
    paired_entry_deterioration_bps:int=20
    paired_hard_loss_bps:int=100
    def __post_init__(self):
        for field in ('unhedged_timeout_ms','hedged_hold_ms','counterparty_offset_ticks'):
            v=getattr(self,field)
            if type(v) is not int or v<=0:raise ValueError(field+' must be positive integer')
        for field in ('paired_take_profit_bps','paired_entry_deterioration_bps','paired_hard_loss_bps'):
            v=getattr(self,field)
            if type(v) is not int or v<=0:raise ValueError(field+' must be positive integer')


class LifecycleShadow:
    def __init__(self,snapshot,price_schema,policy=None,scenario_id=None):
        self.policy=policy or LifecyclePolicy();self.schema=price_schema;self.schema.validate_snapshot(snapshot)
        if snapshot.get('after_entry_consumption') is not False or not snapshot.get('pre_entry_consumption'):
            raise ValueError('pre-entry unconsumed budget snapshot required')
        self.cycle=int(snapshot['cycle_id']);self.signal_ns=int(snapshot['entry_ns']);self.last_ns=self.signal_ns;self.scenario_id=scenario_id or f'fixed{self.policy.counterparty_offset_ticks}_dual_timeout'
        self.reference=list(map(int,snapshot['prices']));self.target=int(snapshot['qty']);self.lng=0 if snapshot['long']=='bybit' else 1
        if self.target<=0 or self.target%10:raise ValueError('positive common-lot candidate required')
        self.maker=0 if self.reference[0]-self.reference[1]>=self.reference[2]-self.reference[3] else 1;self.hedge=1-self.maker
        self.side='buy' if self.maker==self.lng else 'sell';self.sign=1 if self.side=='buy' else -1
        ask,bid=self.reference[2*self.maker:2*self.maker+2];distance=self.policy.counterparty_offset_ticks*self.schema.native_tick_units[self.maker]
        self.quote=ask-distance if self.side=='buy' else bid+distance
        self.budget=[{int(x['price_units_1e8']):[int(x['displayed_qty']),int(x['remaining_quarter'])] for x in side} for side in snapshot['sides']]
        self.events=[];self.counters=Counter();self.state='PENDING';self.reason='';self.order_state='PENDING';self.closing=False
        self.arrival_target=self.signal_ns+LATENCY;self.arrival_ns=None;self.ttl_ns=None;self.cancel_due=None;self.cancel_ack_ns=None;self.cancel_reason=''
        self.maker_qty=0;self.hedged_qty=0;self.unhedged_closed_qty=0;self.paired_closed_qty=0
        self.first_fill_ns=None;self.first_hedge_ns=None;self.paired_deadline_ns=None
        self.paired_exit_trigger_ns=None;self.paired_exit_ready_ns=None;self.paired_exit_reason=''
        self.funding_guard_seen_ns=None;self.day_end_seen_ns=None;self.archive_finished=False
        self.lots={};self.pairs={};self._heap=[];self._timer_seq=0;self._fill_seq=0;self._pair_seq=0;self._close_seq=0
        self.venue_positions=[0,0];self.venue_gross=[0,0];self.venue_fees=[0,0]
        self.paired_entry_fees=0;self.paired_close_fees=0;self.paired_gross=[0,0];self.paired_baseline_net=0;self.paired_long_notional=0
        self.hedge_entry_notional=0;self.max_unhedged_qty=0;self.last_risk_mark=None;self.exit_reasons=Counter()
        if ask-bid<=self.schema.native_tick_units[self.maker]:self.reason='no_legal_inside_tick'
        elif not bid<self.quote<ask:self.reason='offset_not_strictly_inside'
        elif not self.schema.min_notional_units<=self.quote*self.target<=self.schema.max_notional_units:self.reason='maker_order_notional_out_of_bounds'
        if self.reason:self.state='REJECTED';self.order_state='REJECTED'
        self.event('quote_request',self.signal_ns,maker_venue=self.maker,side=self.side,quote_price_units=self.quote,target_qty=self.target,
                   arrival_target_ns=self.arrival_target,policy=asdict(self.policy),reason=self.reason)
        if self.reason:self.event('quote_rejected',self.signal_ns,reason=self.reason)

    @property
    def unhedged_qty(self):return sum(l['qty'] for l in self.lots.values())
    @property
    def paired_qty(self):return sum(p['qty'] for p in self.pairs.values())
    def amount(self,n,places=None):return format(Decimal(n)/(10**(self.schema.money_decimals if places is None else places)),'f')
    def event(self,kind,ns,**fields):self.events.append({'cycle':self.cycle,'scenario_id':self.scenario_id,'type':kind,'ns':int(ns),**fields})
    def schedule(self,ns,kind,identity=None):
        self._timer_seq+=1;heapq.heappush(self._heap,(ns,self._timer_seq,kind,identity))

    def cancel(self,ns,reason):
        if self.order_state=='REJECTED' or self.cancel_due is not None:return
        self.cancel_due=ns+LATENCY;self.cancel_reason=reason
        self.event('cancel_request',ns,reason=reason,effective_ns=self.cancel_due)
        self.schedule(self.cancel_due,'cancel_ack')

    def request_unhedged(self,lot,ns,reason):
        if not lot['qty'] or lot['close_trigger_ns'] is not None:return
        lot['close_trigger_ns']=ns;lot['close_ready_ns']=ns+LATENCY;lot['close_reason']=reason
        self.event('unhedged_exit_request',ns,lot_id=lot['lot_id'],maker_fill_id=lot['fill_id'],qty=lot['qty'],reason=reason,
                   maker_receive_ns=lot['fill_ns'],unhedged_deadline_ns=lot['deadline_ns'],deadline_ns=lot['deadline_ns'],request_ns=ns,earliest_execution_ns=lot['close_ready_ns'])

    def request_paired(self,ns,reason,mark=None):
        if not self.paired_qty or self.paired_exit_trigger_ns is not None:return
        self.paired_exit_trigger_ns=ns;self.paired_exit_ready_ns=ns+LATENCY;self.paired_exit_reason=reason;self.closing=True
        self.cancel(ns,reason)
        self.event('paired_exit_request',ns,reason=reason,qty=self.paired_qty,first_hedge_ns=self.first_hedge_ns,
                   paired_deadline_ns=self.paired_deadline_ns,deadline_ns=self.paired_deadline_ns,request_ns=ns,earliest_execution_ns=self.paired_exit_ready_ns,mark=mark)
        for lot in self.lots.values():self.request_unhedged(lot,ns,'paired_exit_stage')

    def request_all(self,ns,reason):
        self.closing=True;self.cancel(ns,reason)
        self.request_paired(ns,reason)
        for lot in self.lots.values():self.request_unhedged(lot,ns,reason)

    def advance(self,ns):
        if ns<self.last_ns:raise ValueError('lifecycle clock moved backwards')
        self.last_ns=ns
        if self.state=='REJECTED':return
        while self._heap and self._heap[0][0]<=ns:
            deadline,_,kind,key=heapq.heappop(self._heap)
            if kind=='ttl':self.cancel(deadline,'ttl')
            elif kind=='cancel_ack':
                self.cancel_ack_ns=deadline;self.order_state='CANCELLED'
                self.event('cancel_ack',deadline,reason=self.cancel_reason,unfilled_qty=self.target-self.maker_qty)
            elif kind=='unhedged_expiry':
                lot=self.lots[key]
                if lot['qty'] and lot['close_trigger_ns'] is None:
                    self.closing=True;self.cancel(deadline,'unhedged_timeout');self.request_unhedged(lot,deadline,'unhedged_timeout')
            elif kind=='paired_hold':self.request_paired(deadline,'holding_timeout')
        self.refresh_state()

    def refresh_state(self):
        if self.state=='REJECTED' or self.archive_finished:return
        if self.can_archive(self.last_ns):self.state='DONE';self.reason='closed' if self.maker_qty else 'unfilled_cancelled'
        elif not self.unhedged_qty and not self.paired_qty and self.cancel_ack_ns is not None:self.state='FLAT_PENDING_NOTIFICATIONS'
        elif self.closing:self.state='EXITING'
        elif self.paired_qty:self.state='HEDGED' if not self.unhedged_qty else 'PARTIALLY_HEDGED'
        else:self.state=self.order_state

    def can_archive(self,ns):
        if self.state=='REJECTED':return True
        return not self.unhedged_qty and not self.paired_qty and self.cancel_ack_ns is not None and ns>=self.cancel_ack_ns+NOTIFICATION_GRACE

    def record_fill(self,ns,venue,side,intent,qty,px,gross=0,**details):
        self._fill_seq+=1;fid=self._fill_seq;fee=3*px*qty
        self.venue_positions[venue]+=qty*(1 if side=='buy' else -1);self.venue_fees[venue]+=fee;self.venue_gross[venue]+=gross
        self.event('fill',ns,fill_id=fid,intent=intent,venue=venue,side=side,qty=qty,price_units=px,fee_units=fee,
                   gross_realized_units=gross,venue_position_after=self.venue_positions[venue],**details)
        if intent=='maker_open':self.events[-1]['lot_id']=fid
        return fid,fee

    def trade(self,tr):
        if not 0<=tr.ns-tr.source_ns<=NOTIFICATION_GRACE:raise ValueError('uncertifiable trade source receipt lag')
        self.advance(tr.ns)
        if self.state=='REJECTED' or self.arrival_ns is None or tr.venue!=self.maker:return
        if self.cancel_due is not None and tr.source_ns>=self.cancel_due:return
        if tr.source_ns<self.arrival_ns:self.counters['source_before_arrival']+=1;return
        if tr.flags:self.counters['rpi_or_block_excluded']+=1;return
        correct=tr.aggressor==(1 if self.side=='buy' else 0)
        through=tr.price8<self.quote*self.schema.depth_key_scale if self.side=='buy' else tr.price8>self.quote*self.schema.depth_key_scale
        if not correct or not through:return
        quantum=10 if self.maker==0 else 1;qty=min(self.target-self.maker_qty,tr.qty8//(4*10**8*quantum)*quantum)
        if qty<=0:self.counters['participation_below_native_increment']+=1;return
        deadline=tr.ns+self.policy.unhedged_timeout_ms*10**6
        fid,_=self.record_fill(tr.ns,self.maker,self.side,'maker_open',qty,self.quote,source_ns=tr.source_ns,
            source_sequence=tr.sequence,row_ordinal=tr.ordinal,trade_price_1e8=tr.price8,trade_qty_1e8=tr.qty8,
            aggressor=tr.aggressor,unhedged_deadline_ns=deadline,hedge_eligible_ns=tr.ns+LATENCY)
        lot={'lot_id':fid,'fill_id':fid,'qty':qty,'fill_ns':tr.ns,'entry_px':self.quote,'deadline_ns':deadline,
             'close_trigger_ns':None,'close_ready_ns':None,'close_reason':''}
        self.lots[fid]=lot;self.maker_qty+=qty
        if self.first_fill_ns is None:self.first_fill_ns=tr.ns
        self.max_unhedged_qty=max(self.max_unhedged_qty,self.unhedged_qty)
        self.schedule(deadline,'unhedged_expiry',fid);self.cancel(tr.ns,'first_fill')
        if self.closing:self.request_unhedged(lot,tr.ns,'late_fill_after_exit_stage')
        self.refresh_state();self.assert_inventory()

    def level(self,t,side):
        px=t.price[side];level=self.budget[side].get(px*self.schema.depth_key_scale)
        if level is None or level[0]!=t.display[side]:raise ValueError('L1 and shadow depth budget disagree')
        return px,level

    def book(self,t,gate):
        self.advance(t.ns)
        if self.state=='REJECTED':return
        if t.valid:
            for side,p,q in t.updates:
                if not q:self.budget[side].pop(p,None)
                else:
                    old,left=self.budget[side].get(p,(0,0));self.budget[side][p]=[q,max(0,left+q-old)]
        if not gate:self.cancel(t.ns,'inactive')
        if t.force:
            if t.force==1 and self.funding_guard_seen_ns is None:self.funding_guard_seen_ns=t.ns
            if t.force==2 and self.day_end_seen_ns is None:self.day_end_seen_ns=t.ns
            self.request_all(t.ns,'funding_guard' if t.force==1 else 'day_end')
        if self.order_state=='PENDING' and self.cancel_ack_ns is None and t.ns>=self.arrival_target:
            if not t.valid:
                self.state='REJECTED';self.order_state='REJECTED';self.reason='unknown_book_at_arrival';self.event('quote_rejected',t.ns,reason=self.reason);return
            crossing=self.quote>=t.price[2*self.maker] if self.side=='buy' else self.quote<=t.price[2*self.maker+1]
            if crossing:
                self.state='REJECTED';self.order_state='REJECTED';self.reason='post_only_would_cross';self.event('quote_rejected',t.ns,reason=self.reason);return
            self.order_state='LIVE';self.arrival_ns=t.ns;self.ttl_ns=t.ns+TTL;self.schedule(self.ttl_ns,'ttl')
            self.event('quote_arrival',t.ns,quote_price_units=self.quote,own_ask_price_units=t.price[2*self.maker],own_bid_price_units=t.price[2*self.maker+1],ttl_ns=self.ttl_ns)
        if t.valid:
            self.close_unhedged(t);self.close_paired(t)
            if not self.closing:self.open_hedge(t)
            if self.paired_qty and self.paired_exit_trigger_ns is None:self.check_paired_risk(t)
        else:
            if self.closing and (self.unhedged_qty or self.paired_qty):self.counters['exit_blocked_invalid_book']+=1
        self.refresh_state();self.assert_inventory()

    def open_hedge(self,t):
        lots=[l for l in self.lots.values() if l['qty'] and l['close_trigger_ns'] is None and l['fill_ns']+LATENCY<=t.ns<l['deadline_ns']]
        ready=sum(l['qty'] for l in lots)
        if not ready:return
        side=2*self.hedge+(0 if self.hedge==self.lng else 1);px,level=self.level(t,side)
        qty=min(ready,level[1]//4)//10*10;qty=min(qty,max(0,self.schema.max_notional_units-self.hedge_entry_notional)//px//10*10)
        if qty<=0 or qty*px<self.schema.min_notional_units:self.counters['hedge_blocked_depth_lot_or_minimum']+=1;return
        allocations=[];left=qty
        for lot in lots:
            used=min(left,lot['qty']);left-=used;lot['qty']-=used
            allocations.append({'lot_id':lot['lot_id'],'maker_fill_id':lot['fill_id'],'qty':used,'maker_receive_ns':lot['fill_ns'],
                'unhedged_deadline_ns':lot['deadline_ns'],'latency_ns':t.ns-lot['fill_ns']})
            if not left:break
        fid,fee=self.record_fill(t.ns,self.hedge,'buy' if self.hedge==self.lng else 'sell','hedge_open',qty,px,allocations=allocations)
        self.hedged_qty+=qty;self.hedge_entry_notional+=px*qty;level[1]-=4*qty
        self.paired_entry_fees+=fee+3*self.quote*qty
        if self.first_hedge_ns is None:
            self.first_hedge_ns=t.ns;self.paired_deadline_ns=t.ns+self.policy.hedged_hold_ms*10**6;self.schedule(self.paired_deadline_ns,'paired_hold')
        cm=t.price[2*self.maker+(1 if self.side=='buy' else 0)];ch=t.price[2*self.hedge+(0 if self.side=='buy' else 1)]
        baseline=(self.sign*(cm-self.quote)-self.sign*(ch-px))*10000-3*(self.quote+px+cm+ch)
        long_price=self.quote if self.maker==self.lng else px
        for a in allocations:
            self._pair_seq+=1;identity=self._pair_seq
            self.pairs[identity]={'paired_group_id':identity,'qty':a['qty'],'maker_lot_id':a['lot_id'],'maker_fill_id':a['maker_fill_id'],
                'hedge_fill_id':fid,'maker_px':self.quote,'hedge_px':px,'formed_ns':t.ns,'deadline_ns':self.paired_deadline_ns}
            self.event('pair_formed',t.ns,paired_group_id=identity,qty=a['qty'],maker_fill_id=a['maker_fill_id'],hedge_fill_id=fid,
                pair_formed_ns=t.ns,first_hedge_ns=self.first_hedge_ns,paired_deadline_ns=self.paired_deadline_ns,
                initial_reverse_l1_net_units=baseline*a['qty'],long_entry_notional_price_units=long_price*a['qty'])
        self.paired_baseline_net+=baseline*qty;self.paired_long_notional+=long_price*qty

    def close_unhedged(self,t):
        lots=[l for l in self.lots.values() if l['qty'] and l['close_ready_ns'] is not None and l['close_ready_ns']<=t.ns]
        requested=sum(l['qty'] for l in lots)
        if not requested:return
        side=2*self.maker+(1 if self.side=='buy' else 0);px,level=self.level(t,side);quantum=10 if self.maker==0 else 1
        qty=min(requested,level[1]//4)//quantum*quantum
        # Include known younger lots: closing an older ready lot must not leave
        # the aggregate unhedged position below the venue's current minimum.
        qty=self.protect_exit_remainder(qty,self.unhedged_qty,[px],quantum,'unhedged_close')
        if qty<=0:self.counters['unhedged_close_blocked_depth_or_lot']+=1;return
        if qty*px<self.schema.min_notional_units:self.counters['unhedged_close_blocked_minimum']+=1;return
        left=qty;allocations=[];gross=0
        for lot in lots:
            used=min(left,lot['qty']);left-=used;lot['qty']-=used;g=self.sign*(px-lot['entry_px'])*used*10000;gross+=g
            allocations.append({'lot_id':lot['lot_id'],'maker_fill_id':lot['fill_id'],'qty':used,'entry_price_units':lot['entry_px'],
                'trigger_ns':lot['close_trigger_ns'],'earliest_execution_ns':lot['close_ready_ns'],'reason':lot['close_reason'],'gross_realized_units':g})
            self.exit_reasons[lot['close_reason']]+=used
            if not left:break
        self.record_fill(t.ns,self.maker,'sell' if self.side=='buy' else 'buy','unhedged_close',qty,px,gross,
            trigger_ns=min(a['trigger_ns'] for a in allocations),allocations=allocations)
        level[1]-=4*qty;self.unhedged_closed_qty+=qty

    def pair_exit_sides(self):return [2*v+(1 if v==self.lng else 0) for v in (0,1)]

    def protect_exit_remainder(self,qty,total,prices,quantum,prefix):
        """Do not manufacture an uncloseable remainder at current L1 prices.

        The quantity may be reduced, never increased beyond visible capacity or
        close-ready inventory. Existing sub-minimum maker fills remain explicit.
        """
        if qty<=0 or qty>=total:return qty
        minimum=max(quantum,*(((self.schema.min_notional_units+p-1)//p+quantum-1)//quantum*quantum for p in prices))
        if total-qty>=minimum:return qty
        reduced=max(0,(total-minimum)//quantum*quantum)
        if reduced<minimum:
            self.counters[prefix+'_blocked_to_avoid_new_dust']+=1
            return 0
        self.counters[prefix+'_reduced_to_preserve_legal_remainder']+=1
        return reduced

    def close_paired(self,t):
        if not self.paired_qty or self.paired_exit_ready_ns is None or t.ns<self.paired_exit_ready_ns:return
        sides=self.pair_exit_sides();levels=[self.level(t,s) for s in sides]
        qty=min(self.paired_qty,*(lev[1]//4 for _,lev in levels))//10*10
        qty=self.protect_exit_remainder(qty,self.paired_qty,[px for px,_ in levels],10,'paired_close')
        if qty<=0:self.counters['paired_close_blocked_depth_or_lot']+=1;return
        if any(qty*px<self.schema.min_notional_units for px,_ in levels):self.counters['paired_close_blocked_minimum']+=1;return
        parts=[];left=qty
        for pair in self.pairs.values():
            if not pair['qty']:continue
            used=min(left,pair['qty']);left-=used;parts.append((pair,used))
            if not left:break
        self._close_seq+=1;group=self._close_seq
        for venue,(px,lev) in enumerate(levels):
            allocations=[];gross=0
            for pair,used in parts:
                entry=pair['maker_px'] if venue==self.maker else pair['hedge_px'];g=(1 if venue==self.lng else -1)*(px-entry)*used*10000;gross+=g
                allocations.append({'paired_group_id':pair['paired_group_id'],'qty':used,'entry_price_units':entry,
                    'entry_fill_id':pair['maker_fill_id'] if venue==self.maker else pair['hedge_fill_id'],'gross_realized_units':g})
            _,fee=self.record_fill(t.ns,venue,'sell' if venue==self.lng else 'buy','paired_close',qty,px,gross,
                close_group_id=group,trigger_ns=self.paired_exit_trigger_ns,earliest_execution_ns=self.paired_exit_ready_ns,
                reason=self.paired_exit_reason,allocations=allocations,execution_assumption='simultaneous matched visible L1 on both venues')
            self.paired_close_fees+=fee;self.paired_gross[venue]+=gross;lev[1]-=4*qty
        for pair,used in parts:pair['qty']-=used
        self.paired_closed_qty+=qty;self.exit_reasons[self.paired_exit_reason]+=qty

    def check_paired_risk(self,t):
        levels=[self.level(t,s) for s in self.pair_exit_sides()];qty=self.paired_qty
        if any(lev[1]//4<qty or qty*px<self.schema.min_notional_units for px,lev in levels):
            self.counters['paired_risk_mark_not_fully_executable']+=1;return
        gross=0
        for pair in self.pairs.values():
            gross+=pair['qty']*10000*(self.sign*(levels[self.maker][0]-pair['maker_px'])-self.sign*(levels[self.hedge][0]-pair['hedge_px']))
        future_fees=3*qty*sum(px for px,_ in levels)
        net=sum(self.paired_gross)+gross-self.paired_entry_fees-self.paired_close_fees-future_fees
        self.counters['paired_risk_marks']+=1
        mark={'paired_qty':qty,'net_before_funding_units':net,'initial_reverse_l1_baseline_units':self.paired_baseline_net,
              'initial_long_entry_notional_price_units':self.paired_long_notional,'estimated_close_fee_units':future_fees,
              'remaining_gross_units':gross,'realized_paired_gross_units':sum(self.paired_gross),'paid_paired_fees_units':self.paired_entry_fees+self.paired_close_fees,
              'close_prices_units':[x[0] for x in levels],'fully_executable_at_visible_L1':True}
        self.last_risk_mark=mark;reason=None
        if net<=-self.policy.paired_hard_loss_bps*self.paired_long_notional:reason='hard_stop_loss'
        elif net<=self.paired_baseline_net-self.policy.paired_entry_deterioration_bps*self.paired_long_notional:reason='stop_loss'
        elif net>=self.policy.paired_take_profit_bps*self.paired_long_notional:reason='take_profit'
        if reason:self.request_paired(t.ns,reason,mark)

    def assert_inventory(self):
        assert self.maker_qty==self.unhedged_qty+self.paired_qty+self.unhedged_closed_qty+self.paired_closed_qty
        assert self.hedged_qty==self.paired_qty+self.paired_closed_qty
        assert self.venue_positions[self.maker]==self.sign*(self.unhedged_qty+self.paired_qty)
        assert self.venue_positions[self.hedge]==-self.sign*self.paired_qty
        assert self.paired_qty%10==0

    def finish(self,last_ns):
        self.advance(last_ns)
        if self.can_archive(last_ns):self.refresh_state();return
        self.archive_finished=True;self.state='UNRESOLVED'
        self.reason='archive_ended_with_inventory' if self.unhedged_qty or self.paired_qty else 'archive_ends_before_cancel_notification_grace'
        self.event('archive_ended',last_ns,reason=self.reason,unhedged_qty=self.unhedged_qty,paired_qty=self.paired_qty)
        self.assert_inventory()

    def result(self):
        gross=sum(self.venue_gross);fees=sum(self.venue_fees)
        return {'cycle':self.cycle,'scenario_id':self.scenario_id,'signal_ns':self.signal_ns,'state':self.state,'reason':self.reason,
          'execution_enabled':False,'portfolio_aggregation_valid':False,'as_model_used':False,'policy':asdict(self.policy),'price_schema':self.schema.to_dict(),
          'maker_venue':VENUES[self.maker],'maker_side':self.side,'long_venue':VENUES[self.lng],'quote_price_units':self.quote,'target_qty':self.target,
          'arrival_ns':self.arrival_ns,'cancel_ack_ns':self.cancel_ack_ns,'cancel_reason':self.cancel_reason,'first_fill_ns':self.first_fill_ns,
          'first_hedge_ns':self.first_hedge_ns,'paired_deadline_ns':self.paired_deadline_ns,'paired_exit_trigger_ns':self.paired_exit_trigger_ns,'paired_exit_reason':self.paired_exit_reason,
          'maker_open_qty':self.maker_qty,'hedge_open_qty':self.hedged_qty,'unhedged_closed_qty':self.unhedged_closed_qty,'paired_closed_qty':self.paired_closed_qty,
          'maker_qty':self.maker_qty,'unhedged_exit_qty':self.unhedged_closed_qty,
          'unhedged_open_qty':self.unhedged_qty,'paired_open_qty':self.paired_qty,'max_unhedged_qty':self.max_unhedged_qty,
          'venue_positions':dict(zip(VENUES,self.venue_positions)),
          'venue_gross_realized':{v:self.amount(self.venue_gross[i]) for i,v in enumerate(VENUES)},
          'venue_fees':{v:self.amount(self.venue_fees[i]) for i,v in enumerate(VENUES)},
          'venue_net_before_funding':{v:self.amount(self.venue_gross[i]-self.venue_fees[i]) for i,v in enumerate(VENUES)},
          'gross_realized':self.amount(gross),'fees':self.amount(fees),'net_before_funding':self.amount(gross-fees),
          'pnl_complete_before_funding':self.can_archive(self.last_ns),'funding_status':'NOT_COMPUTED_REQUIRES_EXTERNAL_SETTLEMENT_AUDIT',
          'funding_guard_seen_ns':self.funding_guard_seen_ns,'day_end_seen_ns':self.day_end_seen_ns,
          'residual_unhedged_lots':[dict(l) for l in self.lots.values() if l['qty']],
          'residual_pairs':[dict(p) for p in self.pairs.values() if p['qty']],
          'paired_risk_baseline_units':self.paired_baseline_net,'paired_initial_long_notional_price_units':self.paired_long_notional,
          'paired_entry_fees':self.amount(self.paired_entry_fees),'paired_close_fees':self.amount(self.paired_close_fees),
          'last_paired_risk_mark':self.last_risk_mark,'exit_reasons_qty':dict(self.exit_reasons),'counters':dict(self.counters),'event_count':len(self.events),
          'limitations':['Independent opportunity, not continuous shared accounts.','Paired exits assume simultaneous matched fills at visible opposing L1.','Residual positions are not marked or forcibly filled; net_before_funding includes paid fees and realized gross only.','Price risk is checked only when all remaining paired quantity can close at current L1 budget.']}
