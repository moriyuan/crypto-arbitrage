"""Independently reconcile completed paper fills to original streamed depth.

Reads only the small orders/latest_state tables. Does not import PaperSession,
runner, exchange clients, signal tables or execution code. Budget arithmetic is
recomputed from native top-50 displayed-size changes and earlier ledger fills.
"""
from __future__ import annotations
import argparse
import bisect
import hashlib
import json
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

from tardis_adapter import preflight, iter_batches

VENUES = ('bybit', 'bitget')
TOL = D('1e-20')


class AuditFailure(ValueError):
    pass


def check(condition, detail):
    if not condition:
        raise AuditFailure(detail)


def serialized(value):
    return json.dumps(value, default=str, sort_keys=True, separators=(',', ':'))


def digest_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        while data:=f.read(1024*1024):h.update(data)
    return h.hexdigest()


class IndependentSide:
    """Ascending Decimal price index, independent of runner's signed index."""
    def __init__(self, bid):
        self.bid=bid;self.prices=[];self.sizes={};self.top=None;self.top_prices=set()

    def apply(self, rows):
        changed=self.top is None;seen=set()
        boundary=self.top[-1][0] if self.top else None
        for raw_price,raw_quantity in rows:
            price,quantity=D(raw_price),D(raw_quantity)
            check(price.is_finite() and quantity.is_finite() and price>0 and quantity>=0,'Invalid native price/quantity')
            check(price not in seen,'Duplicate native price');seen.add(price)
            old=self.sizes.get(price)
            if quantity==0:
                if old is not None:
                    self.prices.pop(bisect.bisect_left(self.prices,price));del self.sizes[price]
                    changed|=price in self.top_prices
            elif old!=quantity:
                if old is None:bisect.insort(self.prices,price)
                self.sizes[price]=quantity
                changed|=(boundary is None or price in self.top_prices or len(self.prices)<=50 or
                          (price>=boundary if self.bid else price<=boundary))
        if changed:
            selected=self.prices[-50:][::-1] if self.bid else self.prices[:50]
            self.top=tuple((p,self.sizes[p]) for p in selected);self.top_prices=set(selected)


class IndependentBook:
    def __init__(self, venue):
        self.venue=venue;self.bid=IndependentSide(True);self.ask=IndependentSide(False)
        self.seq=self.u=None;self.first_update=False
        self.local_ns=self.engine_ms=self.publish_ms=None;self.provenance=None

    def apply(self,event):
        native=event.native;bybit=self.venue=='bybit'
        row=native['data'] if bybit else native['data'][0]
        snapshot=(native['type'] if bybit else native['action'])=='snapshot'
        seq=int(row['seq']);u=int(row['u']) if bybit else None
        engine=int(native['cts'] if bybit else row['ts'])
        if snapshot:self.bid,self.ask=IndependentSide(True),IndependentSide(False)
        else:
            check(self.seq is not None,'Incremental book without snapshot')
            check(engine>=self.engine_ms,'Native engine time regression')
            if bybit:check(seq>self.seq and u>self.u,'Bybit non-increasing sequence')
            else:
                pseq=int(row['pseq'])
                check(seq>pseq and (pseq<=self.seq<=seq if self.first_update else pseq==self.seq and seq>self.seq),'Bitget sequence-chain error')
        self.bid.apply(row['b']);self.ask.apply(row['a'])
        check(self.bid.top and self.ask.top and self.bid.top[0][0]<self.ask.top[0][0],'Empty/crossed native book')
        self.seq,self.u,self.first_update=seq,u,snapshot
        self.local_ns,self.engine_ms,self.publish_ms=event.local_ns,engine,int(native['ts'])
        self.provenance={'file':str(event.path),'line':event.line_number,'local_timestamp':event.local_timestamp,
                         'local_ns':event.local_ns,'engine_timestamp_ms':engine,'publish_timestamp_ms':self.publish_ms,
                         'sequence':seq,'update_id':u}


def independently_fresh(books,now_ns,policy):
    if set(books)!=set(VENUES):return False
    age=int(policy['max_quote_age_ms'])*1_000_000
    skew=int(policy['max_quote_skew_ms'])*1_000_000
    for book in books.values():
        if not 0<=now_ns-book.local_ns<=age:return False
        if not -500_000_000<=now_ns-book.engine_ms*1_000_000<=age:return False
    return (abs(books['bybit'].local_ns-books['bitget'].local_ns)<=skew and
            abs(books['bybit'].engine_ms-books['bitget'].engine_ms)*1_000_000<=skew)


class IndependentBudgets:
    def __init__(self, participation):
        self.participation=participation;self.levels={};self.last_observed={};self.observations=0

    def observe(self, books):
        for venue,book in books.items():
            for side,display in (('buy',book.ask.top),('sell',book.bid.top)):
                key=(venue,side)
                if self.last_observed.get(key) is display:continue
                old=self.levels.get(key,{})
                current={}
                for price,quantity in display:
                    previous_displayed,previous_remaining=old.get(price,(D(0),D(0)))
                    current[price]=(quantity,max(D(0),previous_remaining+(quantity-previous_displayed)*self.participation))
                self.levels[key]=current;self.last_observed[key]=display;self.observations+=1

    def quote_and_consume(self,venue,side,display,quantity,band_bps):
        best=display[0][0];boundary=best*(1+band_bps/10000 if side=='buy' else 1-band_bps/10000)
        band=[(p,q) for p,q in display if p<=boundary] if side=='buy' else [(p,q) for p,q in display if p>=boundary]
        level_state=self.levels[(venue,side)]
        capacity=sum((level_state[p][1] for p,_ in band),D(0))
        check(quantity<=capacity,'Fill exceeds independent remaining depth capacity')
        left=quantity;notional=D(0);consumed=[]
        for price,displayed in band:
            previous_displayed,remaining=level_state[price]
            check(previous_displayed==displayed,'Budget/display mismatch')
            take=min(left,remaining)
            if take:
                check(take>0 and take<=displayed,'Fill exceeds visible level')
                consumed.append({'price':str(price),'displayed_qty':str(displayed),'budget_before':str(remaining),
                                 'fill_qty':str(take),'budget_after':str(remaining-take)})
                level_state[price]=(displayed,remaining-take)
                notional+=take*price;left-=take
            if not left:break
        check(not left,'Insufficient independent depth')
        return {'vwap':notional/quantity,'exact_level_notional':notional,'capacity':capacity,
                'best_price':best,'band_boundary':boundary,'consumed_levels':consumed,
                'fingerprint':hashlib.sha256(serialized(band).encode()).hexdigest()}


def read_final_orders(root):
    status=json.loads((root/'status.json').read_text())
    check(status['status'] in ('COMPLETED','UNRESOLVED','FUNDING_UNVERIFIED'),'Replay not final; audit refuses running ledger')
    c=sqlite3.connect(f'file:{root / "ledger.sqlite"}?mode=ro',uri=True)
    try:
        c.execute('BEGIN')
        orders=[{'ledger_row_id':rowid,**json.loads(value)} for rowid,value in c.execute('SELECT id,value FROM orders ORDER BY id')]
        state=json.loads(c.execute('SELECT value FROM latest_state WHERE id=1').fetchone()[0])
        c.commit()
    finally:c.close()
    check(len(orders)==state['orders'],'Latest checkpoint/order count mismatch')
    return orders,status,state


def audit(root):
    started=time.monotonic();root=Path(root).resolve();orders,status,final_state=read_final_orders(root)
    metadata=json.loads((root/'run_metadata.json').read_text())
    check(metadata['execution_enabled'] is False and metadata['symbol']=='BTWUSDT','Wrong replay target')
    check(metadata['depth_sizing']=={'band_bps':'10','participation':'0.25'},'Unexpected depth policy')
    code=Path(__file__).parent
    check(digest_file(code/'runner.py')==metadata['runner_sha256'],'Runner changed since replay start')
    check(digest_file(code/'tardis_adapter.py')==metadata['adapter_sha256'],'Adapter changed since replay start')
    archive=preflight(metadata['source_archive'])
    check(dict(archive.evidence_sha256)==metadata['source_evidence_sha256'],'Source evidence changed')
    grouped=defaultdict(list);positions={};previous=-1;pair_groups=[]
    for row in orders:
        cursor=int(row['decision_cursor_ns'])
        check(cursor>=previous,'Ledger decision cursor regressed');previous=cursor
        check(row['symbol']=='BTWUSDT' and row['pair']=='BTW/USDT:USDT' and row['status']=='FILLED','Wrong order target/status')
        grouped[cursor].append(row)
    for cursor,rows in grouped.items():
        groups=defaultdict(list)
        for row in rows:groups[(row['cycle_id'],row['intent'])].append(row)
        for (cycle,intent),legs in groups.items():
            check(len(legs)==2 and {x['venue'] for x in legs}==set(VENUES),'Fill is not a two-venue pair')
            check(len({D(x['qty']) for x in legs})==1 and {x['side'] for x in legs}=={'buy','sell'},'Unequal paired leg quantity/direction')
            quantity=D(legs[0]['qty']);check(quantity>0,'Nonpositive fill')
            if intent=='entry':
                check(cycle not in positions,'Duplicate cycle entry')
                positions[cycle]={'initial':quantity,'remaining':quantity,'exit_parts':0,
                                  'sides':{x['venue']:x['position_side'] for x in legs}}
                check(set(positions[cycle]['sides'].values())=={'long','short'},'Entry is not hedged')
            else:
                check(intent=='exit' and cycle in positions,'Exit without entry')
                check(quantity<=positions[cycle]['remaining'],'Exit exceeds remaining pair quantity')
                positions[cycle]['remaining']-=quantity;positions[cycle]['exit_parts']+=1
            for x in legs:
                position_side=positions[cycle]['sides'][x['venue']]
                check(x['position_side']==position_side,'Position side changed')
                expected_side='buy' if (position_side=='long')==(intent=='entry') else 'sell'
                check(x['side']==expected_side,'Wrong opening/closing side')
            pair_groups.append({'decision_cursor_ns':cursor,'cycle_id':cycle,'intent':intent,'qty':str(quantity)})
    last_cursor=max(grouped,default=None);books={};budgets=IndependentBudgets(D('0.25'))
    observations=[];counts=Counter();seen=set();last_progress=time.monotonic()
    if last_cursor is not None:
        for batch in iter_batches(archive):
            now=batch.ordering_ns
            if now>last_cursor:break
            counts['raw_batches']+=1;counts['raw_frames']+=len(batch.events)
            changed=False
            for event in batch.reconstruction_events:
                if event.kind=='disconnect':
                    changed=True;books.pop(event.venue,None);counts['disconnects_before_last_fill']+=1
                    # Budget memories deliberately survive native reconnection.
                elif event.kind=='book':
                    changed=True
                    if event.venue not in books:books[event.venue]=IndependentBook(event.venue)
                    books[event.venue].apply(event)
            fresh=changed and independently_fresh(books,now,metadata['policy'])
            if fresh:
                counts['fresh_budget_decisions']+=1;budgets.observe(books)
            elif changed:counts['rejected_stale_or_unpaired_decisions']+=1
            if now in grouped:
                check(changed and fresh,f'Orders occur without fresh paired book: {now}')
                seen.add(now)
                for row in grouped[now]:
                    venue,side=row['venue'],row['side'];book=books[venue]
                    check(row['precise_quote_rejected'] is False,'Ledger says stale quote was filled')
                    for v in VENUES:
                        check(int(row[v+'_receive_ns'])==books[v].local_ns,'Recorded receive time differs from native source')
                        check(books[v].local_ns<=now and books[v].engine_ms*1_000_000<=now and books[v].publish_ms*1_000_000<=now,'Future native source used for fill')
                    check(int(row['book_exchange_ts_ms'])==book.engine_ms and int(row['book_received_at_ms'])==book.local_ns//1_000_000,'Recorded book clock differs')
                    check(int(row['timestamp_ms'])==now//1_000_000,'Decision ms/ns mismatch')
                    quantity,price=D(row['qty']),D(row['price']);rule=metadata['rules'][venue]
                    check(quantity%D(rule['qty_step'])==0 and quantity>=D(rule['min_qty']),'Order violates quantity scenario rule')
                    display=book.ask.top if side=='buy' else book.bid.top
                    evidence=budgets.quote_and_consume(venue,side,display,quantity,D('10'))
                    check(abs(evidence['vwap']-price)<=TOL,'VWAP differs from independent depth: '+row['order_id'])
                    check(evidence['capacity']==D(row['depth_capacity_qty']),'Capacity differs from remaining budget: '+row['order_id'])
                    check(evidence['fingerprint']==row['depth_side_fingerprint'],'Source depth fingerprint differs: '+row['order_id'])
                    notional=D(row['notional']);check(abs(notional-quantity*price)<=TOL,'Order notional mismatch')
                    check(notional>=D(rule['min_notional']),'Below minimum notional scenario rule')
                    if row['intent']=='entry':
                        check(notional<=D(metadata['policy']['max_notional']),'Entry above 250USDT cap')
                        limit=D(row['limit_price'])
                        check(price<=limit if side=='buy' else price>=limit,'Entry fill violates limit')
                    check(D(row['fee_rate'])==D('0.0003') and abs(D(row['fee'])-notional*D('0.0003'))<=TOL,'Fee mismatch')
                    observations.append({'order_id':row['order_id'],'ledger_row_id':row['ledger_row_id'],
                        'cycle_id':row['cycle_id'],'venue':venue,'intent':row['intent'],'side':side,
                        'decision_cursor_ns':now,'qty':str(quantity),'ledger_vwap':str(price),
                        'independent_vwap':str(evidence['vwap']),'remaining_capacity_before':str(evidence['capacity']),
                        'best_price':str(evidence['best_price']),'band_boundary':str(evidence['band_boundary']),
                        'source_depth_fingerprint':evidence['fingerprint'],'consumed_levels':evidence['consumed_levels'],
                        'source_books':{v:books[v].provenance for v in VENUES},'status':'PASS'})
            if time.monotonic()-last_progress>15:
                print(serialized({'audit_progress_ns':now,'raw_batches':counts['raw_batches'],'orders_verified':len(observations)}),flush=True)
                last_progress=time.monotonic()
    check(seen==set(grouped),'Some order cursors not found in original raw stream')
    check(len(observations)==len(orders),'Not all orders independently verified')
    return {'status':'PASS','created_at_utc':datetime.now(timezone.utc).isoformat(),'symbol':'BTWUSDT',
            'run_status':status['status'],'execution_enabled':False,'orders_verified':len(orders),
            'fill_pair_groups':len(pair_groups),'cycles':len(positions),'partial_exit_cycles':sum(p['exit_parts']>1 for p in positions.values()),
            'unclosed_remaining_qty':{k:str(v['remaining']) for k,v in positions.items() if v['remaining']},
            'source_scan':'Continuous from original UTC day start through last order cursor inclusive; no arbitrary-fragment restart.',
            'budget_rule':'25% per top-50 price-level budget; observed size increase adds 25%, decrease deducts 25% with zero floor; fills consume cheapest remaining eligible budgets; observation only at paired precise-fresh book decision; disconnect does not reset budget memory.',
            'band_bps':'10','participation':'0.25','vwap_numeric_tolerance':str(TOL),
            'fill_assumption_limitation':'Confirms the configured ideal depth-budget fills, not real latency, queue priority or actual market impact.',
            'last_fill_cursor_ns':last_cursor,'scan_counts':dict(counts),'budget_observations':budgets.observations,
            'elapsed_seconds':time.monotonic()-started,'orders_snapshot_sha256':hashlib.sha256(serialized(orders).encode()).hexdigest(),
            'run_metadata_sha256':digest_file(root/'run_metadata.json'),'auditor_sha256':digest_file(__file__),
            'source_evidence_sha256':dict(archive.evidence_sha256),'orders':observations,'pair_groups':pair_groups}


def selftest():
    import random
    names=[]
    # Spending is not replenished by an unchanged observation or a connection reset.
    class Book:
        pass
    class Side:
        pass
    b=Book();b.ask=Side();b.bid=Side();b.ask.top=((D(1),D(100)),);b.bid.top=((D('.9'),D(100)),)
    budget=IndependentBudgets(D('.25'));budget.observe({'bybit':b})
    q=budget.quote_and_consume('bybit','buy',b.ask.top,D(10),D(10));check(q['vwap']==1,'Selftest vwap')
    names.append('level_budget_fill_vwap')
    budget.observe({'bybit':b});check(budget.levels[('bybit','buy')][D(1)][1]==15,'Same quote replenished spent depth')
    names.append('unchanged_quote_does_not_replenish')
    b.ask.top=tuple([(D(1),D(100))]);budget.observe({'bybit':b});check(budget.levels[('bybit','buy')][D(1)][1]==15,'Reconnect replenished spent depth')
    names.append('reconnect_same_display_does_not_replenish')
    b.ask.top=((D(1),D(140)),);budget.observe({'bybit':b});check(budget.levels[('bybit','buy')][D(1)][1]==25,'Increase budget wrong')
    names.append('only_displayed_increase_adds_participation')
    b.ask.top=((D(1),D(20)),);budget.observe({'bybit':b});check(budget.levels[('bybit','buy')][D(1)][1]==0,'Decrease zero-floor wrong')
    names.append('displayed_decrease_subtracts_with_zero_floor')
    for bid in (True,False):
        side=IndependentSide(bid);side.apply([[str(i),'10'] for i in range(1,101)])
        check(len(side.top)==50,'Wrong exposed depth')
        best=side.top[0][0];side.apply([[str(best),'0']]);check(len(side.top)==50 and side.top[0][0]!=best,'Deeper native level was not promoted')
    names.append('both_sides_promote_previously_known_deeper_level')
    randomizer=random.Random(20261001)
    for bid in (True,False):
        side=IndependentSide(bid);oracle={D(i):D(10) for i in range(1,1001)}
        side.apply([[str(p),str(q)] for p,q in oracle.items()])
        for _ in range(1000):
            price=D(randomizer.randrange(1,1100));quantity=D(randomizer.randrange(0,100))
            if quantity:oracle[price]=quantity
            else:oracle.pop(price,None)
            side.apply([[str(price),str(quantity)]])
            expected=tuple((p,oracle[p]) for p in sorted(oracle,reverse=bid)[:50])
            check(side.top==expected,'Cached independent top50 differs from full-sort oracle')
    names.append('2000_deterministic_top50_mutations_match_full_sort_oracle')
    test_budgets=IndependentBudgets(D('.25'))
    b.ask.top=((D('1'),D(40)),(D('1.001'),D(80)),(D('1.002'),D(400)))
    test_budgets.observe({'bybit':b})
    evidence=test_budgets.quote_and_consume('bybit','buy',b.ask.top,D(30),D(10))
    check(evidence['capacity']==30 and evidence['vwap']==D('1.000666666666666666666666667'),'Inclusive 10bp band/VWAP mismatch')
    names.append('inclusive_10bp_band_excludes_outside_liquidity')
    result={'status':'PASS','tests':names,'test_count':len(names),'random_top50_mutations':2000,
            'auditor_sha256':digest_file(__file__),'execution_enabled':False,
            'created_at_utc':datetime.now(timezone.utc).isoformat()}
    print(serialized(result))
    return result


if __name__=='__main__':
    cli=argparse.ArgumentParser();cli.add_argument('--run-dir',type=Path);cli.add_argument('--output',type=Path);cli.add_argument('--selftest',action='store_true')
    args=cli.parse_args()
    if args.selftest:selftest()
    else:
        check(args.run_dir is not None and args.output is not None,'Explicit run/output required')
        check(not args.output.exists(),'Refusing to overwrite prior audit')
        try:result=audit(args.run_dir)
        except Exception as exc:
            result={'status':'FAIL','error':type(exc).__name__+': '+str(exc),'created_at_utc':datetime.now(timezone.utc).isoformat(),
                    'execution_enabled':False,'auditor_sha256':digest_file(__file__)}
            with args.output.open('x') as f:json.dump(result,f,indent=2);f.write('\n')
            raise
        with args.output.open('x') as f:json.dump(result,f,indent=2,ensure_ascii=False,allow_nan=False);f.write('\n')
        print(serialized({k:v for k,v in result.items() if k not in ('orders','pair_groups','source_evidence_sha256')}))
