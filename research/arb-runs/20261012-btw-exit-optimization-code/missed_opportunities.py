"""Post-stop price/depth diagnostic, never an ex-post-best-price backtest.

Each independent shadow starts from the baseline's post-entry budget and holds
the entire original quantity. Subsequent actual exits and other entries are
ignored; observed depth changes remain causal and consumption is not reset.
"""
from __future__ import annotations
import argparse
import csv
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import struct
import time

HEADER = struct.Struct('<qBBBBI8q')
UPDATE = struct.Struct('<B7xqq')

def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        while b := f.read(1024*1024): h.update(b)
    return h.hexdigest()

def records(path):
    with Path(path).open('rb') as f:
        if f.read(8) != b'BTWTP002': raise ValueError('wrong tape version')
        previous = -1
        while raw := f.read(80):
            if len(raw) != 80: raise ValueError('truncated header')
            ns, valid, force, history, reserved, count, *quotes = HEADER.unpack(raw)
            if ns <= previous or reserved or valid not in (0,1) or force not in (0,1,2):
                raise ValueError('invalid tape header')
            previous = ns
            blob = f.read(count * 24)
            if len(blob) != count * 24: raise ValueError('truncated updates')
            yield ns, bool(valid), force, quotes[:4], quotes[4:], UPDATE.iter_unpack(blob)

def window(start):
    return {'start_ns': start, 'observations': 0, 'invalid_observations': 0,
            'valid_observations': 0, 'fragment_observations': 0, 'full_observations': 0,
            'max_net_bps': None, 'min_net_bps': None,
            'quote_first_hit': None, 'fragment_first_hit': None, 'full_first_hit': None}

class Shadow:
    def __init__(self, cycle, snapshot):
        self.cycle = cycle
        self.entry_ns = int(cycle['entry_ns'])
        self.qty = int(cycle['qty'])
        self.long = 0 if cycle['long_venue'] == 'bybit' else 1
        self.entry_prices = [int(snapshot['entry_prices'][v]) for v in ('bybit','bitget')]
        self.deadline = (int(cycle['entry_timestamp_ms']) + 60000) * 1_000_000
        self.budget = [{int(r['price_units_1e8']): [int(r['displayed_qty']), int(r['remaining_quarter'])]
                        for r in side} for side in snapshot['sides']]
        self.windows = {'after_trigger': window(int(cycle['first_trigger_ns'])),
                        'after_final_exit': window(int(cycle['exit_ns']))}
        self.ended = False
        self.end_ns = None
        self.end_reason = None

    def observe(self, ns, valid, force, prices, quantities, updates):
        if ns >= self.deadline or force:
            self.ended = True
            self.end_ns = min(ns, self.deadline)
            self.end_reason = 'original_60s_limit' if ns >= self.deadline else 'funding_or_dayend_guard'
            return
        active = [w for w in self.windows.values() if ns > w['start_ns']]
        for w in active:
            w['observations'] += 1
            w['valid_observations' if valid else 'invalid_observations'] += 1
        if not valid: return
        for side, price, display in updates:
            b = self.budget[side]
            if display == 0:
                b.pop(price, None)
            else:
                old, remaining = b.get(price, (0,0))
                b[price] = [display, max(0, remaining + display - old)]
        for side in range(4):
            row = self.budget[side].get(prices[side] * 1000)
            if row is None or row[0] != quantities[side]:
                raise ValueError('shadow budget and tape diverged')
        if not active: return
        lng, short = self.long, 1-self.long
        xl, xs = prices[2*lng+1], prices[2*short]
        el, es = self.entry_prices[lng], self.entry_prices[short]
        unit_net_nano = 10000 * (xl-el+es-xs) - 3 * (el+es+xl+xs)
        # Per-coin nano / long entry price ticks equals paired net bp / single leg.
        bps = D(unit_net_nano) / D(el)
        capacity = min(self.budget[2*lng+1][xl*1000][1],
                       self.budget[2*short][xs*1000][1]) // 40 * 10
        fragment = min(self.qty, capacity)
        # Match the live replay's L1 residual-minimum guard before treating a
        # partial exit as executable (both venue minimum notionals are 5 USDT).
        minimum = max(10, ((500000 + min(xl,xs)*10 - 1) // (min(xl,xs)*10))*10)
        if fragment < self.qty and self.qty - fragment < minimum:
            fragment = max(0, self.qty - minimum) // 10 * 10
        legal_fragment = fragment >= 10 and fragment*min(xl,xs) >= 500000
        full = legal_fragment and capacity >= self.qty
        hit = unit_net_nano >= 2 * el
        for w in active:
            w['fragment_observations'] += legal_fragment
            w['full_observations'] += full
            w['max_net_bps'] = bps if w['max_net_bps'] is None else max(w['max_net_bps'], bps)
            w['min_net_bps'] = bps if w['min_net_bps'] is None else min(w['min_net_bps'], bps)
            for category, allowed in [('quote',True),('fragment',legal_fragment),('full',full)]:
                field = category + '_first_hit'
                if hit and allowed and w[field] is None:
                    w[field] = {'ns': ns, 'seconds_after_window_start': D(ns-w['start_ns'])/10**9,
                                'net_bps': bps, 'max_legal_qty': fragment,
                                'worst_net_bps_before_hit': w['min_net_bps']}

    def result(self):
        return {'cycle': self.cycle['cycle'], 'entry_ns': self.entry_ns,
                'original_qty_per_leg': self.qty, 'original_actual_net': self.cycle['net_before_funding'],
                'end_ns': self.end_ns, 'end_reason': self.end_reason,
                'windows': self.windows}

def summarize(results):
    summary = {}
    for name in ('after_trigger','after_final_exit'):
        rows = [r['windows'][name] for r in results]
        summary[name] = {'stopped_cycles': len(rows),
                         'with_future_valid_observations': sum(w['valid_observations'] > 0 for w in rows),
                         'without_future_valid_observations': sum(w['valid_observations'] == 0 for w in rows),
                         'with_any_invalid_decisions': sum(w['invalid_observations'] > 0 for w in rows),
                         **{c+'_hit_cycles': sum(w[c+'_first_hit'] is not None for w in rows)
                            for c in ('quote','fragment','full')}}
    return summary

def run(tape, baseline, output):
    started = time.time()
    output = Path(output)
    if output.exists(): raise ValueError('refusing existing diagnostic output')
    baseline = Path(baseline)
    cycles = [r for r in csv.DictReader((baseline/'cycles.csv').open()) if r['exit_reason']=='stop_loss']
    snapshots = {str(r['cycle_id']):r for r in map(json.loads,(baseline/'entry_budgets.jsonl').read_text().splitlines())}
    pending = [Shadow(c, snapshots[c['cycle']]) for c in cycles]
    pending.sort(key=lambda s:s.entry_ns)
    active, complete, at = [], [], 0
    last_progress = 0
    for ns, valid, force, prices, quantities, updates_iter in records(tape):
        if active:
            updates = list(updates_iter)
            survivors = []
            for shadow in active:
                shadow.observe(ns,valid,force,prices,quantities,updates)
                if shadow.ended: complete.append(shadow.result())
                else: survivors.append(shadow)
            active = survivors
        while at < len(pending) and pending[at].entry_ns <= ns:
            if pending[at].entry_ns != ns: raise ValueError('entry not found in decision tape')
            active.append(pending[at]);at += 1
        if time.time() - last_progress > 20:
            print(json.dumps({'cursor_ns':ns,'activated':at,'completed':len(complete)}),flush=True)
            last_progress = time.time()
    if active or at != len(pending): raise ValueError('incomplete shadow horizon')
    complete.sort(key=lambda r:r['entry_ns'])
    result = {'status':'COMPLETED_DIAGNOSTIC_ONLY','symbol':'BTWUSDT','target_net_bps':2,
              'summary':summarize(complete),'cycles':complete,
              'semantics':'Independent original full-quantity shadow for each baseline stop, initialized with actual post-entry consumable budgets. No subsequent actual exits or other entries. No best-future-price orders or PnL claims.',
              'limitations':['Hindsight diagnostic of observable recovery, not realizable strategy return.',
                            'Full-depth opportunity assumes ideal simultaneous taker execution without latency.',
                            'No observed hit is not proof of absence during invalid/missing observations.',
                            'The original fee-net stop determines this selected sample.'],
              'tape_sha256':sha(tape),'cycles_sha256':sha(baseline/'cycles.csv'),
              'entry_budgets_sha256':sha(baseline/'entry_budgets.jsonl'),
              'script_sha256':sha(Path(__file__)),'elapsed_seconds':time.time()-started,
              'execution_enabled':False}
    with output.open('x') as f:json.dump(result,f,default=str,ensure_ascii=False,indent=2);f.write('\n')
    print(json.dumps({'status':result['status'],'summary':result['summary'],'elapsed_seconds':result['elapsed_seconds']},ensure_ascii=False),flush=True)

if __name__ == '__main__':
    cli=argparse.ArgumentParser()
    cli.add_argument('--tape',type=Path,required=True)
    cli.add_argument('--baseline',type=Path,required=True)
    cli.add_argument('--output',type=Path,required=True)
    a=cli.parse_args();run(a.tape,a.baseline,a.output)
