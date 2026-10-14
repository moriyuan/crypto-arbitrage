"""Offline independent maker/hedge lifecycles with two explicit time limits.

Inputs come from a hash-verified historical cohort. No exchange client, AS
pricing, new data acquisition, parameter optimization or live trading.
"""
from __future__ import annotations
import argparse
from collections import Counter
from dataclasses import asdict
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import time

FROZEN = Path('/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-code')
sys.path.insert(0, str(FROZEN))
from execution_adapter import PriceSchema, book_records, legacy


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')


def load_inputs(summary_path):
    source = json.loads(Path(summary_path).read_text())
    if source.get('symbol') != 'BTWUSDT' or source.get('execution_enabled') is not False:
        raise ValueError('requires the frozen public BTWUSDT offline cohort')
    names = ('tape', 'trades', 'activity', 'entry_snapshots', 'thresholds')
    paths = {}
    for name in names:
        spec = source['inputs'][name]
        if digest(spec['path']) != spec['sha256']:
            raise ValueError(f'input hash changed: {name}')
        paths[name] = spec['path']
    return source, paths


def load_snapshots(path, schema, start_ns, end_ns):
    snapshots = {}
    with Path(path).open() as f:
        for line in f:
            s = json.loads(line)
            ns = int(s['entry_ns'])
            if not start_ns <= ns < end_ns:
                raise ValueError('candidate outside causal evaluation window')
            if ns in snapshots:
                raise ValueError('duplicate candidate timestamp')
            schema.validate_snapshot(s)
            snapshots[ns] = s
    return snapshots


def fixed_quote(snapshot, schema, offset_ticks):
    if isinstance(offset_ticks, bool) or not isinstance(offset_ticks, int) or offset_ticks <= 0:
        raise ValueError('offset must be positive native ticks')
    prices = list(map(int, snapshot['prices']))
    maker = 0 if prices[0] - prices[1] >= prices[2] - prices[3] else 1
    lng = 0 if snapshot['long'] == 'bybit' else 1
    ask, bid = prices[2 * maker:2 * maker + 2]
    distance = offset_ticks * schema.native_tick_units[maker]
    return ask - distance if maker == lng else bid + distance


def replay(args):
    # Deferred import keeps input checks independently testable.
    from lifecycle_engine import LifecyclePolicy, LifecycleShadow
    out = Path(args.output)
    if out.exists():
        raise FileExistsError('refusing to overwrite an existing experiment')
    policy = LifecyclePolicy(unhedged_timeout_ms=args.unhedged_timeout_ms,
                             hedged_hold_ms=args.hedged_hold_ms,
                             counterparty_offset_ticks=args.offset_ticks)
    source, paths = load_inputs(args.source_summary)
    schema = PriceSchema.from_tape(paths['tape'])
    day = dt.datetime.fromisoformat(source['date_utc']).replace(tzinfo=dt.timezone.utc)
    day_start = int(day.timestamp()) * legacy.NS
    start, end = day_start + 43200 * legacy.NS, day_start + 86400 * legacy.NS
    snapshots = load_snapshots(paths['entry_snapshots'], schema, start, end)
    thresholds = legacy.read_thresholds(paths['thresholds'], 'q25')
    out.mkdir(parents=True)
    code_paths = [Path(__file__), Path(__file__).with_name('lifecycle_engine.py'),
                  FROZEN / 'execution_adapter.py', Path(legacy.__file__)]
    code_hashes = {str(p): digest(p) for p in code_paths}
    policy_path = Path(__file__).with_name('policy.py')
    if policy_path.exists():
        code_hashes[str(policy_path)] = digest(policy_path)
    metadata = {'date_utc': source['date_utc'], 'symbol': 'BTWUSDT',
                'execution_enabled': False, 'as_pricing_enabled': False,
                'portfolio_pnl_computed': False, 'policy': asdict(policy),
                'price_schema': schema.to_dict(),
                'source_summary': str(Path(args.source_summary).resolve()),
                'source_summary_sha256': digest(args.source_summary),
                'inputs': {name: source['inputs'][name] for name in paths},
                'code_sha256': code_hashes,
                'evaluation_start_ns': start, 'evaluation_end_ns_exclusive': end,
                'scope': 'Independent opportunity lifecycles, not a continuous account. '
                         'Overlapping opportunities do not share cash or depth. '
                         'Paired closes assume simultaneous matched-size L1 fills '
                         'after latency and capacity checks; no new entry search.'}
    write_json(out / 'run_metadata.json', metadata)
    write_json(out / 'status.json', {'status': 'RUNNING', 'execution_enabled': False})
    started = time.monotonic()
    active, results, events, activated = [], [], [], set()
    decisions = trade_count = 0
    last_ns = 0

    def drain(ns):
        nonlocal active
        remaining = []
        for shadow in active:
            if shadow.can_archive(ns):
                results.append(shadow.result())
                events.extend(shadow.events)
            else:
                remaining.append(shadow)
        active = remaining

    trades = iter(legacy.trade_records(paths['trades']))
    tr = next(trades, None)
    try:
        for tick, ar in book_records(paths['tape'], paths['activity']):
            while tr is not None and tr.ns <= tick.ns:
                for shadow in active:
                    shadow.trade(tr)
                trade_count += 1
                last_ns = tr.ns
                drain(tr.ns)
                tr = next(trades, None)
            gate = legacy.allowed(ar, thresholds)
            if active:
                tick.updates = list(legacy.UPDATE.iter_unpack(tick.updates))
                for shadow in active:
                    shadow.book(tick, gate)
                drain(tick.ns)
            if tick.ns in snapshots:
                s = snapshots[tick.ns]
                if not tick.valid or tick.force or not tick.history or not gate:
                    raise ValueError('historical candidate contradicts current gates')
                if list(map(int, s['prices'])) != tick.price:
                    raise ValueError('snapshot/L1 mismatch')
                shadow = LifecycleShadow(s, schema, policy)
                if shadow.quote != fixed_quote(s, schema, args.offset_ticks):
                    raise ValueError('lifecycle quote does not match fixed native-tick policy')
                active.append(shadow)
                activated.add(tick.ns)
                drain(tick.ns)
            decisions += 1
            last_ns = tick.ns
        while tr is not None:
            for shadow in active:
                shadow.trade(tr)
            trade_count += 1
            last_ns = tr.ns
            drain(tr.ns)
            tr = next(trades, None)
        for shadow in active:
            shadow.finish(last_ns)
            results.append(shadow.result())
            events.extend(shadow.events)
        if activated != set(snapshots) or len(results) != len(snapshots):
            raise ValueError('not every frozen candidate completed a diagnostic')
        for name, expected in code_hashes.items():
            if digest(name) != expected:
                raise ValueError('source changed during replay')
        results.sort(key=lambda r: r['cycle'])
        events.sort(key=lambda r: (r['ns'], r['cycle']))
        for name, rows in [('cycles.jsonl', results), ('events.jsonl', events)]:
            with (out / name).open('x') as f:
                for row in rows:
                    f.write(json.dumps(row, separators=(',', ':'), ensure_ascii=False) + '\n')
        summary = {**metadata, 'status': 'COMPLETED_INDEPENDENT_LIFECYCLE_REPLAY',
                   'candidate_count': len(results), 'source_decisions': decisions,
                   'source_live_trades': trade_count, 'event_count': len(events),
                   'states': dict(Counter(r['state'] for r in results)),
                   'elapsed_seconds': time.monotonic() - started,
                   'funding_status': 'PENDING_INDEPENDENT_INTERVAL_AUDIT',
                   'economic_scope': 'Per-opportunity realized net before funding; '
                                     'unclosed residuals are not final PnL.'}
        write_json(out / 'summary.json', summary)
        write_json(out / 'status.json', {'status': summary['status'],
                                       'candidate_count': len(results), 'execution_enabled': False})
        print(json.dumps({k: summary[k] for k in ('status', 'candidate_count', 'states', 'elapsed_seconds')}))
        return summary
    except BaseException as error:
        # Preserve evidence, even if a subsequent validation fails.
        with (out / 'failure-partial.jsonl').open('w') as f:
            for shadow in active:
                f.write(json.dumps({'cycle_result': shadow.result(), 'events': shadow.events}) + '\n')
            for row in results:
                f.write(json.dumps({'completed_cycle_result': row}) + '\n')
            for event in events:
                f.write(json.dumps({'completed_event': event}) + '\n')
        write_json(out / 'status.json', {'status': 'FAILED', 'execution_enabled': False,
                                       'error': str(error), 'last_ns': last_ns})
        raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-summary', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--unhedged-timeout-ms', type=int, default=1000)
    p.add_argument('--hedged-hold-ms', type=int, default=60000)
    p.add_argument('--offset-ticks', type=int, default=1)
    replay(p.parse_args())


if __name__ == '__main__':
    main()
