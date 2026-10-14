"""Causal live-trade activity features; public archive only, never exchange orders.

Rolling window is (decision - window, decision], in native receive time. A source
connection gap clears only that venue's rolling state; a full new observation
window is required after its first subsequent received frame. Historical Bitget
trade snapshots are excluded. Trade source time is retained separately and never
used to reorder locally received evidence.
"""
from __future__ import annotations
import argparse
from collections import Counter, deque
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import mmap
from pathlib import Path
import struct
import sys
import time

BASE = Path('/Users/mori/code/crypto/state/arb-runs')
ADAPTER_ROOT = BASE / '20261012-btw-exit-optimization-code'
sys.path.insert(0, str(ADAPTER_ROOT))
from day_adapter import preflight, iter_batches, parse_local_ns, startup_activity_ready
from day_inputs import Day, validate_stream_counts

SCALE = 100_000_000
NS = 1_000_000_000
VENUES = ('bybit', 'bitget')
TRADE_MAGIC = b'BTWTRD01'
FEATURE_MAGIC = b'BTWACT01'
ACT_MAGIC = b'BTWAES01'
TRADE = struct.Struct('<qqBB6xqqqII8x')
FEATURE = struct.Struct('<qIIqqqqQ')
ACT_EVENT = struct.Struct('<qBB2xIq')
BOOK_HEADER = struct.Struct('<qBBBBI8q')
BOOK_UPDATE = struct.Struct('<B7xqq')


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        while b := f.read(1024 * 1024): h.update(b)
    return h.hexdigest()


def write_json(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    tmp.replace(path)


def require(condition, message):
    if not condition: raise ValueError(message)


def units_1e8(value):
    d = Decimal(value)
    require(d.is_finite() and d > 0, 'nonpositive/nonfinite price or quantity')
    v = d * SCALE
    require(v == v.to_integral_value(), 'price or quantity requires precision finer than 1e-8')
    require(v < 2**63, 'fixed point overflow')
    return int(v)


def notional_1e8(price, quantity):
    product = price * quantity
    require(product % SCALE == 0, 'notional requires precision finer than 1e-8 USDT')
    answer = product // SCALE
    require(0 < answer < 2**63, 'invalid notional')
    return answer


def is_live_trade(venue, native):
    return venue == 'bybit' or native.get('action') == 'update'


@dataclass(frozen=True)
class Feature:
    ns: int
    count_a: int
    count_b: int
    turnover_a: int
    turnover_b: int
    age_a_ns: int
    age_b_ns: int
    warm_mask: int

    def as_tuple(self):
        return (self.ns, self.count_a, self.count_b, self.turnover_a, self.turnover_b,
                self.age_a_ns, self.age_b_ns, self.warm_mask)


@dataclass(frozen=True)
class GateConfig:
    min_count: int = 0
    min_turnover_1e8: int = 0
    max_trade_age_ns: int = 5 * NS

    def __post_init__(self):
        require(self.min_count >= 0 and self.min_turnover_1e8 >= 0 and self.max_trade_age_ns >= 0,
                'negative gate parameter')


def allow_entry(feature: Feature, config: GateConfig) -> bool:
    """Both venues must pass; an exit must never be blocked by this entry gate."""
    return (feature.warm_mask == 3 and
            min(feature.count_a, feature.count_b) >= config.min_count and
            min(feature.turnover_a, feature.turnover_b) >= config.min_turnover_1e8 and
            0 <= feature.age_a_ns <= config.max_trade_age_ns and
            0 <= feature.age_b_ns <= config.max_trade_age_ns)


class RollingActivity:
    def __init__(self, window_ns=60 * NS):
        require(window_ns > 0, 'nonpositive window')
        self.window_ns = window_ns
        self.rows = [deque(), deque()]
        self.count = [0, 0]
        self.turnover = [0, 0]
        self.last_trade = [None, None]
        self.segment_start = [None, None]
        self.cursor = -1

    def _clock(self, ns):
        require(ns >= self.cursor, 'activity clock regressed / future feature')
        self.cursor = ns

    def resume(self, venue, ns):
        self._clock(ns)
        if self.segment_start[venue] is None: self.segment_start[venue] = ns

    def disconnect(self, venue, ns):
        self._clock(ns)
        self.segment_start[venue] = None
        self.rows[venue].clear()
        self.count[venue] = self.turnover[venue] = 0
        self.last_trade[venue] = None

    def trade(self, venue, ns, count, turnover):
        self._clock(ns)
        require(self.segment_start[venue] is not None, 'trade before observed stream recovery')
        require(count > 0 and turnover > 0, 'invalid live trade group')
        self.rows[venue].append((ns, count, turnover))
        self.count[venue] += count
        self.turnover[venue] += turnover
        self.last_trade[venue] = ns

    def feature(self, ns):
        self._clock(ns)
        mask = 0
        ages = []
        for venue in range(2):
            q = self.rows[venue]
            while q and q[0][0] <= ns - self.window_ns:
                _, count, notional = q.popleft()
                self.count[venue] -= count
                self.turnover[venue] -= notional
            start = self.segment_start[venue]
            if start is not None and ns - start >= self.window_ns: mask |= 1 << venue
            ages.append(-1 if self.last_trade[venue] is None else ns - self.last_trade[venue])
        return Feature(ns, *self.count, *self.turnover, *ages, mask)


def iter_activity(path):
    with Path(path).open('rb') as f:
        require(f.read(8) == ACT_MAGIC, 'invalid activity event magic')
        while raw := f.read(ACT_EVENT.size):
            require(len(raw) == ACT_EVENT.size, 'truncated activity event')
            yield ACT_EVENT.unpack(raw)


def iter_decisions(path):
    with Path(path).open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
        require(data[:8] in (b'BTWTP002',b'BTWTP003'), 'invalid book tape magic')
        at, previous = 8, -1
        while at < len(data):
            require(at + BOOK_HEADER.size <= len(data), 'truncated book header')
            row = BOOK_HEADER.unpack_from(data, at)
            ns, updates = row[0], row[5]
            require(ns > previous, 'book decision clock not strict')
            previous = ns
            at += BOOK_HEADER.size + updates * BOOK_UPDATE.size
            require(at <= len(data), 'truncated book depth changes')
            yield ns
        require(at == len(data), 'book tape trailing bytes')


def extract(archive, output, progress):
    counts = Counter()
    seen_ids = [set(), set()]
    connected = [False, False]
    disconnects = []
    recoveries = []
    first_ns = last_ns = None
    previous_trade_ns = -1
    day_start = parse_local_ns(archive.date + 'T00:00:00.0000000Z')
    day_end = day_start + 86400 * NS
    extrema = {'min_price_1e8': None, 'max_price_1e8': None, 'min_qty_1e8': None, 'max_qty_1e8': None,
               'max_trade_receive_lag_ns': 0}
    paths = [output/'live-trades.bin.partial', output/'activity-events.bin.partial']
    with paths[0].open('xb') as trades, paths[1].open('xb') as activity:
        trades.write(TRADE_MAGIC)
        activity.write(ACT_MAGIC)
        for batch in iter_batches(archive):
            ns = batch.ordering_ns
            if first_ns is None: first_ns = ns
            last_ns = ns
            counts['native_batches'] += 1
            accepted = {(e.venue, e.source_sequence) for e in batch.reconstruction_events}
            groups = {}
            for event in batch.events:
                venue = VENUES.index(event.venue)
                counts['frames:' + event.venue] += 1
                counts['kind:' + event.venue + ':' + event.kind] += 1
                if event.kind not in ('trade', 'historical_trade'): continue
                native = event.native
                rows = native['data']
                require(is_live_trade(event.venue, native) == (event.kind == 'trade'), 'live/history classification mismatch')
                if event.kind == 'historical_trade':
                    counts['historical_records:' + event.venue] += len(rows)
                    continue
                require(event.local_ns == ns, 'live trade missing exact receiver timestamp')
                require(ns >= previous_trade_ns, 'trade tape receive clock regressed')
                previous_trade_ns = ns
                group_notional = 0
                for ordinal, trade in enumerate(rows):
                    require(event.venue != 'bybit' or trade.get('s') == archive.symbol, 'trade symbol mismatch')
                    side = trade['S'].lower()
                    require(side in ('buy', 'sell'), 'invalid aggressor side')
                    source_ms = int(trade['T'])
                    require(str(source_ms) == str(trade['T']), 'nonintegral source timestamp')
                    source_ns = source_ms * 1_000_000
                    require(day_start <= source_ns < day_end, 'live trade source timestamp outside UTC day')
                    require(source_ns <= ns, 'future-source live trade cannot be used')
                    identity = str(trade['i'])
                    require(identity and identity not in seen_ids[venue], 'duplicate live trade identity')
                    seen_ids[venue].add(identity)
                    price, quantity = units_1e8(trade['p']), units_1e8(trade['v'])
                    notional = notional_1e8(price, quantity)
                    rpi = trade.get('RPI', False) is True or trade.get('isRPI') == 'yes'
                    block = trade.get('BT', False) is True
                    flags = int(rpi) | (int(block) << 1)
                    trades.write(TRADE.pack(ns, source_ns, venue, int(side == 'sell'), price, quantity,
                                            event.source_sequence, ordinal, flags))
                    group_notional += notional
                    counts['live_records:' + event.venue] += 1
                    counts['turnover_1e8:' + event.venue] += notional
                    counts['flag_rpi:' + event.venue] += rpi
                    counts['flag_block:' + event.venue] += block
                    counts['qty_not_multiple_10:' + event.venue] += quantity % (10 * SCALE) != 0
                    counts['fractional_coin_qty:' + event.venue] += quantity % SCALE != 0
                    for key, value, fn in (('min_price_1e8',price,min),('max_price_1e8',price,max),
                                           ('min_qty_1e8',quantity,min),('max_qty_1e8',quantity,max)):
                        extrema[key] = value if extrema[key] is None else fn(extrema[key],value)
                    extrema['max_trade_receive_lag_ns'] = max(extrema['max_trade_receive_lag_ns'], ns-source_ns)
                groups[(event.venue,event.source_sequence)] = (len(rows),group_notional)
                if (event.venue,event.source_sequence) not in accepted:
                    counts['same_cursor_pre_disconnect_live_records:' + event.venue] += len(rows)
            # This is the same disconnect-first, same-cursor suppression contract
            # as the native decision tape, not a guessed timestamp for gaps.
            for event in batch.reconstruction_events:
                if not startup_activity_ready(archive,event.venue,ns):continue
                venue = VENUES.index(event.venue)
                if event.kind == 'disconnect':
                    connected[venue] = False
                    activity.write(ACT_EVENT.pack(ns,venue,1,0,0))
                    disconnects.append({'venue':event.venue,'ordering_ns':ns,'local_ns':event.local_ns,
                                        'ordering_basis':event.ordering_basis,'source_sequence':event.source_sequence})
                    continue
                if not connected[venue]:
                    connected[venue] = True
                    activity.write(ACT_EVENT.pack(ns,venue,0,0,0))
                    recoveries.append({'venue':event.venue,'receive_ns':ns,'kind':event.kind,
                                       'source_sequence':event.source_sequence})
                if event.kind == 'trade':
                    count, notional = groups[(event.venue,event.source_sequence)]
                    activity.write(ACT_EVENT.pack(ns,venue,2,count,notional))
                    counts['activity_live_records:' + event.venue] += count
            if counts['native_batches'] % 50000 == 0: progress('EXTRACTING', dict(counts), ns)
    count_audit=validate_stream_counts(archive,counts,'activity')
    for path in paths: path.replace(path.with_suffix(''))
    return {'counts':dict(counts),'source_count_audit':count_audit,'extrema':extrema,'disconnects':disconnects,'recoveries':recoveries,
            'first_receive_ns':first_ns,'last_receive_ns':last_ns,'day_start_ns':day_start}


def features(output, tape, window_ns, day_start, progress, expected_decisions):
    import numpy as np
    source = iter_activity(output/'activity-events.bin')
    pending = next(source,None)
    activity = RollingActivity(window_ns)
    def evaluate(ns):
        nonlocal pending
        while pending is not None and pending[0] <= ns:
            event_ns, venue, kind, count, notional = pending
            if kind == 0: activity.resume(venue,event_ns)
            elif kind == 1: activity.disconnect(venue,event_ns)
            elif kind == 2: activity.trade(venue,event_ns,count,notional)
            else: raise ValueError('invalid activity event kind')
            pending = next(source,None)
        return activity.feature(ns)
    seconds = np.empty((86400,8),dtype=np.int64)
    second_i, decisions = 0, 0
    counts = Counter()
    partial = output/'features-decisions.bin.partial'
    with partial.open('xb') as out:
        out.write(FEATURE_MAGIC)
        for ns in iter_decisions(tape):
            require(day_start<=ns<day_start+86400*NS, "decision outside requested date")
            while second_i < len(seconds) and day_start+second_i*NS < ns:
                seconds[second_i] = evaluate(day_start+second_i*NS).as_tuple()
                second_i += 1
            f = evaluate(ns)
            out.write(FEATURE.pack(*f.as_tuple()))
            if second_i < len(seconds) and day_start+second_i*NS == ns:
                seconds[second_i] = f.as_tuple()
                second_i += 1
            decisions += 1
            counts['decision_warm_mask:' + str(f.warm_mask)] += 1
            counts['both_last_trade_age_le_5s'] += (0<=f.age_a_ns<=5*NS and 0<=f.age_b_ns<=5*NS)
            if decisions % 250000 == 0: progress('FEATURES',dict(counts,decisions=decisions),ns)
        while second_i < len(seconds):
            seconds[second_i] = evaluate(day_start+second_i*NS).as_tuple()
            second_i += 1
    require(decisions == expected_decisions and decisions>0, 'decision feature count mismatch')
    require(pending is None or pending[0] < day_start+86400*NS, 'activity records outside day')
    partial.replace(output/'features-decisions.bin')
    np.savez_compressed(output/'features-uniform-seconds.npz', features=seconds,
                        fields=np.array(['ns','count_bybit','count_bitget','turnover_bybit_1e8',
                                         'turnover_bitget_1e8','last_trade_age_bybit_ns',
                                         'last_trade_age_bitget_ns','warm_valid_mask']))
    return {'decisions':decisions,'uniform_seconds':len(seconds),'counts':dict(counts)}


def build(args):
    day=Day(args.date)
    output = Path(args.output)
    output.mkdir(parents=True,exist_ok=False)
    started = time.time()
    def progress(state, counts=None, cursor=None, error=None):
        row = {'state':state,'heartbeat_epoch':time.time(),'elapsed_seconds':time.time()-started,
               'counts':counts or {},'cursor_ns':cursor,'error':error,'execution_enabled':False}
        write_json(output/'status.json',row)
        print(json.dumps(row),flush=True)
    progress('PREFLIGHT')
    try:
        archive = preflight(Path(args.archive),expected_date=day.text,startup_evidence=args.startup_orphan_evidence)
        metadata_path=Path(args.tape).parent/'tape-metadata.json'
        tape_metadata=json.loads(metadata_path.read_text())
        require(tape_metadata['format'] in ('BTWTP002','BTWTP003') and not tape_metadata.get('grid_only',False),'not a full BTWTP002 tape')
        require(tape_metadata.get('date',archive.date)==day.text,'book tape date mismatch')
        require(tape_metadata['source_evidence_sha256']==archive.evidence_sha256,'book tape archive evidence mismatch')
        require(tape_metadata['tape_sha256']==sha(args.tape),'book tape SHA mismatch')
        raw = extract(archive,output,progress)
        require(raw['counts']['native_batches']==tape_metadata['counts']['batches'],'book and activity native batch counts differ')
        feature = features(output,Path(args.tape),args.window_seconds*NS,raw['day_start_ns'],progress,tape_metadata['counts']['decisions'])
        metadata = {'status':'COMPLETED','symbol':archive.symbol,'date':archive.date,'execution_enabled':False,
                    'source_archive':str(archive.root),'source_evidence_sha256':archive.evidence_sha256,
                    'script_sha256':sha(__file__),'day_adapter_sha256':sha(Path(__file__).with_name('day_adapter.py')),'startup_exception':archive.startup_exception,'day_inputs_sha256':sha(Path(__file__).with_name('day_inputs.py')),'tape_metadata_sha256':sha(metadata_path),'adapter_sha256':sha(ADAPTER_ROOT/'tardis_adapter.py'),
                    'book_tape':str(Path(args.tape).resolve()),'book_tape_sha256':sha(args.tape),
                    'window_ns':args.window_seconds*NS,'window':'(t - window, t] native receive time',
                    'warmup':'Full window after first observed received frame, initially and after every disconnect; disconnected windows are unknown, never inactive.',
                    'entry_only_gate':True,'trade_record_struct':TRADE.format,'trade_record_bytes':TRADE.size,
                    'trade_fields':['receive_ns','source_ns','venue_0_bybit_1_bitget','aggressor_0_buy_1_sell',
                                    'price_1e8','qty_1e8','source_sequence','row_ordinal','flags_rpi_bit0_block_bit1'],
                    'feature_record_struct':FEATURE.format,'feature_record_bytes':FEATURE.size,
                    'feature_fields':['ns','count_bybit','count_bitget','turnover_bybit_1e8',
                                      'turnover_bitget_1e8','last_trade_age_bybit_ns','last_trade_age_bitget_ns','warm_valid_mask'],
                    'feature_magic':FEATURE_MAGIC.decode(),'trade_magic':TRADE_MAGIC.decode(),
                    'activity_event_struct':ACT_EVENT.format,'activity_event_magic':ACT_MAGIC.decode(),
                    'activity_event_kinds':{'0':'first_received_or_recovery','1':'disconnect','2':'live_trade_group'},
                    'integer_scale':100000000,'source_time_checks':'Every live source timestamp is inside UTC day and <= native receive timestamp; source-time regressions retained without reordering.',
                    'trade_identity_checks':'Each live ID unique per venue; exact duplicate fails closed. Historical Bitget snapshot records are counted but excluded.',
                    'qty_rounding':'None: native quantities retained exactly to 1e-8; no contract-lot rounding.',
                    'raw':raw,'feature_summary':feature,'elapsed_seconds':time.time()-started}
        metadata['artifacts'] = {p.name:{'bytes':p.stat().st_size,'sha256':sha(p)} for p in output.iterdir()
                                 if p.name.endswith(('.bin','.npz'))}
        write_json(output/'metadata.json',metadata)
        progress('COMPLETED',dict(raw['counts'],decisions=feature['decisions']),raw['last_receive_ns'])
    except BaseException as exc:
        progress('FAILED',error=f'{type(exc).__name__}: {exc}')
        raise


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--date',required=True)
    p.add_argument('--archive',required=True)
    p.add_argument('--tape',required=True)
    p.add_argument('--output',required=True)
    p.add_argument('--window-seconds',type=int,default=60)
    p.add_argument('--startup-orphan-evidence',type=Path)
    a=p.parse_args()
    require(a.window_seconds>0,'nonpositive window')
    build(a)

if __name__=='__main__': main()
