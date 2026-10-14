"""Lossless decision/depth-change tape from the frozen native BTW replay.

Only book reconstruction is cached. Candidate-specific liquidity consumption,
positions, fees and decisions must be recomputed by each execution engine.
"""
from __future__ import annotations
import argparse
from collections import Counter
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import struct
import sys
import time

OLD = Path('/Users/mori/code/crypto/state/arb-runs/20261012-btw-l1-net-2bps-code')
sys.path.insert(0, str(OLD))
from runner import (BASE, FastBookState, HistoricalFunding,
                    Policy, precise_fresh, sha, write_json)
from btc_cta.cross_arb.engine import fresh
from day_adapter import preflight, iter_batches, startup_entry_ready
from day_inputs import Day, DailyHistory, validate_stream_counts, validate_funding_day

MAGICS = {5:b'BTWTP002',6:b'BTWTP003'}
MAGIC = MAGICS[5]
HEADER = struct.Struct('<qBBBBI8q')
UPDATE = struct.Struct('<B7xqq')
SIDES = [('bybit', 'asks'), ('bybit', 'bids'), ('bitget', 'asks'), ('bitget', 'bids')]

def exact_int(value, name):
    value = D(value)
    if not value.is_finite() or value != value.to_integral_value():
        raise ValueError(f'nonintegral {name}: {value}')
    return int(value)

class ObservedDepth:
    def __init__(self,price_decimals=5):
        if price_decimals not in MAGICS:raise ValueError("unsupported price decimals")
        self.price_decimals=price_decimals
        self.previous = [{} for _ in SIDES]
        self.identities = [None for _ in SIDES]

    def encode(self, books):
        prices, quantities, changes = [], [], []
        for side, (venue, attr) in enumerate(SIDES):
            levels = getattr(books[venue], attr)
            validate_l1_prices(books[venue],venue,self.price_decimals)
            prices.append(exact_int(levels[0][0] * 10**self.price_decimals, 'price units'))
            quantities.append(exact_int(levels[0][1], 'display quantity'))
            if levels is self.identities[side]:
                continue
            current = {exact_int(p * 100000000, 'depth price units 1e-8'): exact_int(q, 'display quantity')
                       for p, q in levels}
            if len(current) != len(levels) or any(p <= 0 or q <= 0 for p, q in current.items()):
                raise ValueError('invalid observed levels')
            old = self.previous[side]
            for p in old.keys() - current.keys():
                changes.append((side, p, 0))
            for p, q in current.items():
                if old.get(p) != q:
                    changes.append((side, p, q))
            self.previous[side], self.identities[side] = current, levels
        return prices, quantities, changes

def validate_l1_prices(book,venue,price_decimals):
    native_decimals=5 if venue=='bybit' else price_decimals
    for levels in (book.asks,book.bids):
        if not levels:raise ValueError('empty native BBO')
        exact_int(levels[0][0]*10**price_decimals,'header price unit')
        exact_int(levels[0][0]*10**native_decimals,'assumed native quote tick')

def grid_row(ns,books,received,segments,policy,price_decimals=5):
    mids=[0,0];mask=0;venues=('bybit','bitget')
    for i,v in enumerate(venues):
        b=books.get(v)
        if b is None or v not in received or not segments.get(v,0):continue
        if not 0<=ns-received[v]<=policy.max_quote_age_ms*1_000_000:continue
        if not -500_000_000<=ns-b.exchange_ts_ms*1_000_000<=policy.max_quote_age_ms*1_000_000:continue
        if not 0<=b.request_ms<=policy.max_request_ms:continue
        if not b.asks or not b.bids or b.asks[0][0]<=b.bids[0][0]:continue
        validate_l1_prices(b,v,price_decimals)
        mids[i]=exact_int((b.asks[0][0]+b.bids[0][0])*10**price_decimals,'twice mid price units');mask|=1<<i
    if all(v in books and v in received for v in venues):
        if (abs(received['bybit']-received['bitget'])>policy.max_quote_skew_ms*1_000_000 or
            abs(books['bybit'].exchange_ts_ms-books['bitget'].exchange_ts_ms)>policy.max_quote_skew_ms):
            mids=[0,0];mask=0
    return (ns,*mids,mask,segments.get('bybit',0),segments.get('bitget',0))

class GridSampler:
    def __init__(self,day,price_decimals=5):self.day=day;self.price_decimals=price_decimals;self.next_ns=day.start_ns;self.rows=[]
    def emit_until(self,cursor,inclusive,books,received,segments,policy):
        while self.next_ns<self.day.end_ns and (self.next_ns<cursor or inclusive and self.next_ns==cursor):
            self.rows.append(grid_row(self.next_ns,books,received,segments,policy,self.price_decimals));self.next_ns+=1_000_000_000
    def save(self,path):
        import numpy as np
        if len(self.rows)!=86400:raise ValueError('incomplete one-second grid')
        columns=['ns','bybit_mid_ticks_x2','bitget_mid_ticks_x2','validmask','segmentA','segmentB']
        data=np.asarray(self.rows,dtype=np.int64)
        np.savez_compressed(path,data=data,columns=np.asarray(columns))
        return {'path':str(path),'sha256':sha(path),'rows':len(self.rows),'columns':columns,
                'price_decimals':self.price_decimals,'price_tick_usdt':str(D(1).scaleb(-self.price_decimals)),'mid_encoding':'(ask_ticks+bid_ticks), divide by two for mid ticks',
                'validity':'per-venue receive and source age <=2s (source permits <=500ms future); paired receive/source skew >500ms invalidates both; unavailable mid=0',
                'segments':'zero on disconnect, increment per native snapshot; invalid grid point breaks rolling windows',
                'causality':'All and only whole native batches with receiver/ordering ns <= grid ns; unchanged book age reevaluated on every second',
                'validmask_counts':{str(i):int((data[:,3]==i).sum()) for i in range(4)}}

def build(args):
    day=Day(args.date)
    price_decimals=args.price_decimals
    magic=MAGICS[price_decimals]
    if not args.grid_only and (not args.history or not args.funding):
        raise ValueError("full tape requires --history and --funding")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    started = time.time()
    counts = Counter()
    cursor = None
    last_progress = 0
    def progress(status, error=None):
        nonlocal last_progress
        last_progress = time.time()
        row = {'status': status, 'heartbeat_epoch': last_progress,
               'elapsed_seconds': last_progress - started, 'cursor_ns': cursor,
               'counts': dict(counts), 'error': error}
        write_json(output / 'tape-status.json', row)
        print(json.dumps(row), flush=True)
    progress('PREFLIGHT')
    try:
        archive = preflight(args.archive,expected_date=day.text,startup_evidence=args.startup_orphan_evidence)
        gate = funding = None
        if not args.grid_only:
            gate=DailyHistory(args.history,day)
            validate_funding_day(args.funding,day)
            funding=HistoricalFunding(args.funding)
        policy = Policy(bybit_fee_bps=D(3), bitget_fee_bps=D(3), price_guard_bps=D(0), max_cycles=10000)
        parsers = {v: FastBookState(v) for v in ('bybit', 'bitget')}
        books, received = {}, {}
        observed = ObservedDepth(price_decimals)
        grid=GridSampler(day,price_decimals)
        segments={v:0 for v in ("bybit","bitget")}
        segment_counts={v:0 for v in segments}
        previous_cursor = -1
        progress('BUILDING')
        partial = output / 'tape.bin.partial'
        with (partial.open('xb') if not args.grid_only else open('/dev/null','wb')) as f:
            if not args.grid_only:f.write(magic)
            for batch in iter_batches(archive):
                cursor = batch.ordering_ns
                day.validate_ns(cursor)
                grid.emit_until(cursor,False,books,received,segments,policy)
                counts['batches'] += 1
                for event in batch.events:
                    counts['frames:' + event.venue] += 1
                    counts['kind:' + event.venue + ':' + event.kind] += 1
                    if event.kind in ('trade', 'historical_trade'):
                        counts['trade_records:' + event.venue + ':' + event.kind] += len(event.native['data'])
                changed = False
                for event in batch.reconstruction_events:
                    if event.kind == 'disconnect':
                        changed = True
                        books.pop(event.venue, None)
                        received.pop(event.venue, None)
                        parsers[event.venue] = FastBookState(event.venue)
                        segments[event.venue]=0
                    elif event.kind == 'book':
                        changed = True
                        snapshot=(event.native.get('type',event.native.get('action'))=='snapshot')
                        if snapshot:
                            segment_counts[event.venue]+=1
                            segments[event.venue]=segment_counts[event.venue]
                        books[event.venue] = parsers[event.venue].apply(event.native, event.local_ns)
                        validate_l1_prices(books[event.venue],event.venue,price_decimals)
                        received[event.venue] = event.local_ns
                grid.emit_until(cursor,True,books,received,segments,policy)
                if changed and not args.grid_only:
                    if cursor <= previous_cursor:
                        raise ValueError('decision clock did not strictly increase')
                    previous_cursor = cursor
                    guard = precise_fresh(books, received, policy, cursor)
                    valid = guard and fresh(books, policy, cursor // 1_000_000)
                    counts['precise_quote_rejected'] += not guard
                    counts['valid'] += bool(valid)
                    force = 1 if funding.decision(cursor, policy.max_holding_s) else 0
                    if day.force_day_end(cursor):
                        force = 2
                    history = gate.evaluate('BTWUSDT', cursor // 1_000_000)['history_filter_passed'] and startup_entry_ready(archive,cursor)
                    counts['history_admitted_decisions']+=bool(history)
                    counts['forced_funding_decisions']+=force==1
                    counts['forced_day_end_decisions']+=force==2
                    if valid:
                        prices, quantities, updates = observed.encode(books)
                    else:
                        prices, quantities, updates = [0] * 4, [0] * 4, []
                    f.write(HEADER.pack(cursor, int(valid), force, int(history), 0,
                                        len(updates), *prices, *quantities))
                    for update in updates:
                        f.write(UPDATE.pack(*update))
                    counts['decisions'] += 1
                    counts['depth_changes'] += len(updates)
                if time.time() - last_progress > 15:
                    progress('BUILDING')
        count_audit=validate_stream_counts(archive,counts,'tape')
        grid.emit_until(day.end_ns,False,books,received,segments,policy)
        grid_metadata=grid.save(output/'mid-grid.npz')
        tape = output / 'tape.bin'
        if not args.grid_only:
            if not counts['decisions']:raise ValueError('no book decisions in archive')
            partial.replace(tape)
        metadata = {'format':magic.decode(), 'header_struct': HEADER.format,
                    'update_struct': UPDATE.format, 'sides': SIDES,
                    'price_tick_usdt':str(D(1).scaleb(-price_decimals)),'price_decimals':price_decimals,
                    'money_decimals':price_decimals+4,'native_tick_units':{'bybit':10**(price_decimals-5),'bitget':1},
                    'native_tick_rule_evidence':'Observed historical BBO raster plus explicit scenario tick assumption; not a verified historical exchange rule snapshot',
                    'display_qty_units': '1 BTW',
                    'update_price_unit_usdt': '0.00000001',
                    'candidate_budget_units': '0.25 BTW',
                    'date':day.text,'symbol':'BTWUSDT','grid_only':args.grid_only,
                    'counts': dict(counts),'source_count_audit':count_audit,
                    'tape_sha256':sha(tape) if not args.grid_only else None,
                    'bytes':tape.stat().st_size if not args.grid_only else 0,
                    'mid_grid':grid_metadata,
                    'builder_sha256': sha(Path(__file__)),
                    'source_code_sha256': {name: sha(OLD/name) for name in ['runner.py','tardis_adapter.py','risk_policy.py']},
                    'day_inputs_sha256':sha(Path(__file__).with_name('day_inputs.py')),
                    'day_adapter_sha256':sha(Path(__file__).with_name('day_adapter.py')),
                    'startup_exception':archive.startup_exception,
                    'source_archive': str(archive.root), 'source_evidence_sha256': archive.evidence_sha256,
                    'history':gate.metadata() if gate else None,'funding_evidence':funding.evidence if funding else None,
                    'execution_enabled': False,
                    'description': 'Every native book/disconnect decision. Freshness and causal funding/history gates unchanged. Sparse top50 display changes observed only on valid paired decisions; each scenario recomputes its own consumption.'}
        write_json(output / 'tape-metadata.json', metadata)
        progress('COMPLETED')
    except BaseException as e:
        progress('FAILED', type(e).__name__ + ': ' + str(e))
        raise

if __name__ == '__main__':
    cli = argparse.ArgumentParser()
    cli.add_argument('--output', type=Path, required=True)
    cli.add_argument('--date',required=True)
    cli.add_argument('--archive',type=Path,required=True)
    cli.add_argument('--history',type=Path)
    cli.add_argument('--funding',type=Path)
    cli.add_argument('--grid-only',action='store_true')
    cli.add_argument('--startup-orphan-evidence',type=Path)
    cli.add_argument('--price-decimals',type=int,choices=(5,6),required=True)
    build(cli.parse_args())
