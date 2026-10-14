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
from runner import (BASE, END_MS, FastBookState, FrozenHistory, HistoricalFunding,
                    Policy, precise_fresh, sha, write_json)
from btc_cta.cross_arb.engine import fresh
from tardis_adapter import preflight, iter_batches

MAGIC = b'BTWTP002'
HEADER = struct.Struct('<qBBBBI8q')
UPDATE = struct.Struct('<B7xqq')
SIDES = [('bybit', 'asks'), ('bybit', 'bids'), ('bitget', 'asks'), ('bitget', 'bids')]

def exact_int(value, name):
    value = D(value)
    if not value.is_finite() or value != value.to_integral_value():
        raise ValueError(f'nonintegral {name}: {value}')
    return int(value)

class ObservedDepth:
    def __init__(self):
        self.previous = [{} for _ in SIDES]
        self.identities = [None for _ in SIDES]

    def encode(self, books):
        prices, quantities, changes = [], [], []
        for side, (venue, attr) in enumerate(SIDES):
            levels = getattr(books[venue], attr)
            prices.append(exact_int(levels[0][0] * 100000, 'price ticks'))
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

def build(output):
    output = Path(output)
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
        archive = preflight(BASE / '20261009-btw-tardis-20261001')
        gate = FrozenHistory(BASE / '20261009-btw-oct1-history')
        funding = HistoricalFunding(BASE / '20261009-btw-oct1-funding')
        policy = Policy(bybit_fee_bps=D(3), bitget_fee_bps=D(3), price_guard_bps=D(0), max_cycles=10000)
        parsers = {v: FastBookState(v) for v in ('bybit', 'bitget')}
        books, received = {}, {}
        observed = ObservedDepth()
        previous_cursor = -1
        progress('BUILDING')
        partial = output / 'tape.bin.partial'
        with partial.open('xb') as f:
            f.write(MAGIC)
            for batch in iter_batches(archive):
                cursor = batch.ordering_ns
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
                    elif event.kind == 'book':
                        changed = True
                        books[event.venue] = parsers[event.venue].apply(event.native, event.local_ns)
                        received[event.venue] = event.local_ns
                if changed:
                    if cursor <= previous_cursor:
                        raise ValueError('decision clock did not strictly increase')
                    previous_cursor = cursor
                    guard = precise_fresh(books, received, policy, cursor)
                    valid = guard and fresh(books, policy, cursor // 1_000_000)
                    counts['precise_quote_rejected'] += not guard
                    counts['valid'] += bool(valid)
                    force = 1 if funding.decision(cursor, policy.max_holding_s) else 0
                    if cursor >= (END_MS - 120000) * 1_000_000:
                        force = 2
                    history = gate.evaluate('BTWUSDT', cursor // 1_000_000)['history_filter_passed']
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
        if counts['batches'] != 3462182 or counts['decisions'] != 3160784:
            raise ValueError('native day counts differ from audited baseline')
        if counts['precise_quote_rejected'] != 64891:
            raise ValueError('freshness rejection count differs from audited baseline')
        tape = output / 'tape.bin'
        partial.replace(tape)
        metadata = {'format': MAGIC.decode(), 'header_struct': HEADER.format,
                    'update_struct': UPDATE.format, 'sides': SIDES,
                    'price_tick_usdt': '0.00001', 'display_qty_units': '1 BTW',
                    'update_price_unit_usdt': '0.00000001',
                    'candidate_budget_units': '0.25 BTW',
                    'counts': dict(counts), 'tape_sha256': sha(tape), 'bytes': tape.stat().st_size,
                    'builder_sha256': sha(Path(__file__)),
                    'source_code_sha256': {name: sha(OLD/name) for name in ['runner.py','tardis_adapter.py','risk_policy.py']},
                    'source_archive': str(archive.root), 'source_evidence_sha256': archive.evidence_sha256,
                    'history': gate.metadata(), 'funding_evidence': funding.evidence,
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
    build(cli.parse_args().output)
