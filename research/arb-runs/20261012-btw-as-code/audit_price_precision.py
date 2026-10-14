"""Reconstruct native BBO and record exact observed price grids, never infer legal ticks.

Offline and read-only on source archives. A complete independently accepted raw
archive is required. Receipt order and disconnect boundaries are preserved.
"""
import argparse
from collections import Counter
from decimal import Decimal
from functools import lru_cache
import gzip
import hashlib
import heapq
import json
from math import gcd
from pathlib import Path

SCALE = 100_000_000

def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()

@lru_cache(maxsize=100_000)
def units(text):
    value = Decimal(text)
    assert value.is_finite() and value > 0, text
    scaled = value * SCALE
    assert scaled == scaled.to_integral_value(), ('finer_than_1e8', text)
    return int(scaled)

def places(value):
    result = 8
    while result and value % 10 == 0:
        result -= 1
        value //= 10
    return result

class Side:
    def __init__(self, bid):
        self.sign = -1 if bid else 1
        self.values = set()
        self.heap = []

    def update(self, rows):
        for price, quantity in rows:
            p = units(price)
            q = Decimal(quantity)
            assert q.is_finite() and q >= 0
            if q == 0:
                self.values.discard(p)
            elif p not in self.values:
                self.values.add(p)
                heapq.heappush(self.heap, p * self.sign)
        if len(self.heap) > max(1000, 4 * len(self.values)):
            self.heap = [p * self.sign for p in self.values]
            heapq.heapify(self.heap)

    def best(self):
        while self.heap and self.heap[0] * self.sign not in self.values:
            heapq.heappop(self.heap)
        return self.heap[0] * self.sign if self.heap else None

def scan(root, venue):
    counts = Counter()
    precision = Counter()
    bbo_grid = change_grid = level_grid = 0
    max_places = level_max_places = 0
    min_change = None
    previous = None
    valid = False
    bids, asks = Side(True), Side(False)
    examples = []
    finer1e5 = []
    for path in sorted((root / venue).glob('offset*.jsonl.gz')):
        counts['files'] += 1
        with gzip.open(path, 'rt') as f:
            for number, raw in enumerate(f, 1):
                counts['raw_lines'] += 1
                line = raw.rstrip('\r\n')
                stamp, sep, body = line.partition(' ')
                if not line or not sep or not body:
                    counts['disconnect_markers'] += 1
                    valid = False
                    previous = None
                    bids, asks = Side(True), Side(False)
                    continue
                obj = json.loads(body)
                if venue == 'bybit':
                    topic = obj.get('topic')
                    if topic != 'orderbook.50.BTWUSDT':
                        continue
                    data = obj['data']
                    assert data['s'] == 'BTWUSDT'
                    kind = obj['type']
                else:
                    arg = obj.get('arg', {})
                    if arg.get('topic') != 'books' or 'event' in obj:
                        continue
                    assert arg['symbol'] == 'BTWUSDT'
                    assert len(obj['data']) == 1
                    data = obj['data'][0]
                    kind = obj['action']
                counts['book_' + kind] += 1
                for side in ('b', 'a'):
                    for text, quantity in data[side]:
                        p = units(text)
                        counts['raw_price_level_updates'] += 1
                        level_grid = gcd(level_grid, p)
                        level_max_places = max(level_max_places, places(p))
                if kind == 'snapshot':
                    bids, asks = Side(True), Side(False)
                    valid = True
                    previous = None
                elif not valid:
                    counts['uninitialized_book_updates'] += 1
                    continue
                bids.update(data['b'])
                asks.update(data['a'])
                bid, ask = bids.best(), asks.best()
                assert bid and ask and bid < ask, (path, number, bid, ask)
                counts['reconstructed_bbo_events'] += 1
                event_places = max(places(bid), places(ask))
                precision[str(event_places)] += 1
                if event_places > max_places or not examples:
                    max_places = max(max_places, event_places)
                    examples.append({'file': str(path.relative_to(root)), 'line': number,
                                     'receipt_timestamp': stamp, 'bid_units_1e8': bid,
                                     'ask_units_1e8': ask, 'decimal_places': event_places})
                if (bid % 1000 or ask % 1000) and len(finer1e5) < 3:
                    finer1e5.append({'file': str(path.relative_to(root)), 'line': number,
                                    'receipt_timestamp': stamp, 'bid_units_1e8': bid,
                                    'ask_units_1e8': ask})
                counts['bbo_not_exactly_representable_1e6'] += bool(bid % 100 or ask % 100)
                bbo_grid = gcd(bbo_grid, gcd(bid, ask))
                if previous:
                    for old, new in zip(previous, (bid, ask)):
                        change = abs(new - old)
                        if change:
                            change_grid = gcd(change_grid, change)
                            min_change = change if min_change is None else min(change, min_change)
                previous = bid, ask
    return {'venue': venue, 'counts': dict(counts),
            'bbo_event_max_decimal_places_histogram': dict(precision),
            'max_bbo_decimal_places': max_places,
            'max_all_level_decimal_places': level_max_places,
            'all_bbo_exactly_representable_at_1e6': not counts['bbo_not_exactly_representable_1e6'],
            'observed_bbo_price_gcd_units_1e8': bbo_grid,
            'observed_all_level_price_gcd_units_1e8': level_grid,
            'observed_nonzero_same_side_bbo_change_gcd_units_1e8': change_grid,
            'minimum_observed_nonzero_same_side_bbo_change_units_1e8': min_change,
            'precision_examples': examples, 'finer_than_1e5_examples': finer1e5}

def main(root):
    root = root.resolve()
    acceptance = root.parent / 'raw-acceptance.json'
    prior = json.loads(acceptance.read_text())
    assert prior['status'] == 'PASS_NATIVE_PREFLIGHT' and prior['archive'] == str(root)
    original = json.loads((root / 'audit.json').read_text())
    venues = [scan(root, venue) for venue in ('bybit', 'bitget-futures')]
    for row in venues:
        expected = next(v for v in original['venues'] if v['venue'] == row['venue'])['counts']
        assert row['counts']['files'] == 144
        assert row['counts']['raw_lines'] == expected['lines']
        for name in ('book_snapshot', 'book_delta', 'book_update'):
            assert row['counts'].get(name, 0) == expected.get(name, 0), (row['venue'], name)
        assert row['counts'].get('uninitialized_book_updates', 0) == 0
    output = {'status': 'PASS' if all(v['all_bbo_exactly_representable_at_1e6'] for v in venues) else 'FAIL',
              'date': prior['date'], 'symbol': 'BTWUSDT', 'execution_enabled': False,
              'definition': 'Every valid BBO after native L2 update in receipt order; disconnect clears book and previous-price comparison.',
              'interpretation': 'Observed decimal precision and numerical grids only. These are not proof of official historical legal tick size.',
              'source_hashes': {str(p): sha(p) for p in (Path(__file__), root / 'manifest.json', root / 'audit.json', acceptance)},
              'venues': venues}
    destination = root.parent / 'precision-audit.json'
    with destination.open('x') as f:
        json.dump(output, f, indent=2, allow_nan=False)
        f.write('\n')
    print(json.dumps({'date': prior['date'], 'status': output['status'], 'path': str(destination),
                      'venues': [{k: v[k] for k in ('venue', 'max_bbo_decimal_places', 'observed_bbo_price_gcd_units_1e8', 'all_bbo_exactly_representable_at_1e6')} for v in venues]}), flush=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path)
    args = parser.parse_args()
    main(args.archive)
