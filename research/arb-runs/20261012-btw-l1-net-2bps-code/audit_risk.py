"""Independent fee-net TP2/SL2 audit from immutable orders and risk events.

No imports of the runner, policy, or paper implementation. Opens the final ledger
read-only and never reads signals/equity. Arithmetic checks concern recorded risk
evaluations; the separate depth auditor proves native L1 quotes and budgets.
This does not prove that no unlogged trigger opportunity was missed.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import sqlite3
import time

VENUES = ('bybit', 'bitget')
BASIS = 'remaining_long_entry_notional'
PNL_BASIS = {k: 'remaining_pair_net_before_funding' for k in ('take_profit', 'stop_loss')}
RISK_REASONS = frozenset(PNL_BASIS)
RELEVANT_EVENTS = frozenset(('exit_triggered', 'pair_partially_closed', 'pair_closed'))
TOL = D('1e-18')


def number(value):
    result = D(str(value))
    if not result.is_finite():
        raise ValueError('nonfinite numeric evidence')
    return result


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def audit_records(metadata, state, orders, cycles, events):
    failures, triggers = [], []
    counts = Counter()

    def check(ok, label, detail=None):
        counts['checks'] += 1
        if not ok:
            counts['failures'] += 1
            if len(failures) < 100:
                failures.append({'check': label, 'detail': detail})

    def equal(a, b, label):
        check(abs(number(a) - number(b)) <= TOL, label, {'computed': str(a), 'recorded': str(b)})

    for label, record in (('metadata', metadata), ('state', state)):
        check(record.get('risk_basis') == BASIS, label + ':risk_basis')
        check(record.get('risk_pnl_basis') == PNL_BASIS, label + ':risk_pnl_basis')
        policy = record.get('policy', {})
        check('stop_loss_usdt' not in policy, label + ':no_fixed_usdt_stop')
        for key, expected in (('take_profit_bps', 2), ('stop_loss_bps', 2),
                              ('bybit_fee_bps', 3), ('bitget_fee_bps', 3)):
            try:
                equal(expected, policy[key], label + ':' + key)
            except (KeyError, ValueError, ArithmeticError) as exc:
                check(False, label + ':invalid_' + key, type(exc).__name__)
    check(state.get('policy') == metadata.get('policy'), 'state_policy_matches_metadata')
    check(state.get('cycle') is None, 'no_unclosed_state_cycle')
    check(metadata.get('symbol') == 'BTWUSDT' and metadata.get('execution_enabled') is False,
          'expected_public_paper_scope')
    order_groups, event_groups = defaultdict(list), defaultdict(list)
    for order in orders:
        order_groups[order['cycle_id']].append(order)
    for index, event in enumerate(events):
        if event.get('kind') not in RELEVANT_EVENTS:
            continue
        cid = event.get('cycle_id') or event.get('result', {}).get('cycle_id')
        check(cid is not None, 'event_has_cycle_identity')
        if cid is not None:
            event_groups[cid].append((index, event))
    by_cycle = {c['cycle_id']: c for c in cycles}
    check(len(by_cycle) == len(cycles), 'unique_closed_cycles')
    check(set(order_groups) == set(by_cycle), 'all_order_cycles_closed')
    check(set(event_groups) <= set(by_cycle), 'events_have_known_cycles')
    check(len({o['order_id'] for o in orders}) == len(orders), 'unique_orders')
    trigger_counts = Counter({reason: 0 for reason in sorted(RISK_REASONS)})

    for cid, cycle in by_cycle.items():
        try:
            rows = order_groups[cid]
            entries = [r for r in rows if r['intent'] == 'entry']
            check(len(entries) == 2 and {r['venue'] for r in entries} == set(VENUES), 'paired_entries:' + cid)
            if len(entries) != 2 or {r['venue'] for r in entries} != set(VENUES):
                continue
            entry = {r['venue']: r for r in entries}
            long_entries = [r for r in entries if r['position_side'] == 'long' and r['side'] == 'buy']
            check(len(long_entries) == 1, 'one_long_entry:' + cid)
            if len(long_entries) != 1:
                continue
            long = long_entries[0]['venue']
            short = next(v for v in VENUES if v != long)
            check(entry[short]['position_side'] == 'short' and entry[short]['side'] == 'sell', 'one_short_entry:' + cid)
            q0 = number(entry[long]['qty'])
            equal(q0, entry[short]['qty'], 'equal_initial_qty:' + cid)
            check(q0 > 0, 'positive_initial_qty:' + cid)
            start = int(entry[long]['decision_cursor_ns'])
            check(start == int(entry[short]['decision_cursor_ns']), 'paired_entry_time:' + cid)
            prices = {v: number(entry[v]['price']) for v in VENUES}
            fee_rates = {v: number(metadata['policy'][v + '_fee_bps']) / 10000 for v in VENUES}
            parts = defaultdict(list)
            for row in rows:
                q, price, venue = number(row['qty']), number(row['price']), row['venue']
                check(q > 0 and price > 0, 'positive_order:' + row['order_id'])
                equal(fee_rates[venue], row['fee_rate'], 'order_fee_rate:' + row['order_id'])
                equal(q * price * fee_rates[venue], row['fee'], 'order_fee:' + row['order_id'])
                if row['intent'] == 'exit':
                    ns = int(row['decision_cursor_ns'])
                    check(ns > start, 'exit_after_entry_decision:' + cid)
                    check(row['side'] == ('sell' if venue == long else 'buy'), 'exit_side:' + row['order_id'])
                    parts[ns].append(row)
                else:
                    check(row['intent'] == 'entry', 'known_order_intent:' + row['order_id'])
            closed_qty = D(0)
            remaining_by_part = {}
            for ns, part in sorted(parts.items()):
                check(len(part) == 2 and {r['venue'] for r in part} == set(VENUES), 'paired_exit:' + cid)
                if len(part) != 2:
                    continue
                equal(part[0]['qty'], part[1]['qty'], 'paired_exit_qty:' + cid)
                closed_qty += number(part[0]['qty'])
                remaining_by_part[ns] = q0 - closed_qty
                check(closed_qty <= q0, 'no_overclose:' + cid)
            equal(closed_qty, q0, 'closed_quantity:' + cid)
            check(len(parts) == int(cycle['exit_parts']), 'exit_parts:' + cid)
            indexed = event_groups[cid]
            risk_events = [(i, e) for i, e in indexed if e['kind'] == 'exit_triggered']
            final_reason = cycle['exit_reason']
            check(len(risk_events) == (1 if final_reason in RISK_REASONS else 0), 'exactly_one_initial_risk_trigger:' + cid)
            partial_events = [(i, e) for i, e in indexed if e['kind'] == 'pair_partially_closed']
            closing_events = [(i, e) for i, e in indexed if e['kind'] == 'pair_closed']
            check(len(closing_events) == 1, 'one_close_event:' + cid)
            check(len(partial_events) == max(0, len(parts) - 1), 'one_event_per_partial_exit:' + cid)
            seen_partial = set()
            for index, event in partial_events:
                ns = int(event['decision_cursor_ns'])
                check(ns in parts and ns != max(parts), 'partial_event_matches_nonfinal_fill:' + cid)
                check(ns not in seen_partial, 'unique_partial_event:' + cid)
                seen_partial.add(ns)
                if ns in parts:
                    equal(event['closed_qty'], parts[ns][0]['qty'], 'partial_closed_qty:' + cid)
                    equal(event['remaining_qty'], remaining_by_part[ns], 'partial_remaining_qty:' + cid)
                check(event['reason'] == final_reason, 'sticky_partial_reason:' + cid)
            for index, event in closing_events:
                check(int(event['decision_cursor_ns']) == max(parts), 'close_event_matches_last_fill:' + cid)
                check(event['result']['exit_reason'] == final_reason, 'sticky_final_reason:' + cid)

            for index, event in risk_events:
                ns = int(event['decision_cursor_ns'])
                ev = event['evaluation']
                reason = event['reason']
                check(reason in RISK_REASONS and reason == final_reason, 'trigger_reason_matches_cycle:' + cid)
                check(start < ns <= min(parts), 'risk_trigger_precedes_first_exit:' + cid)
                check(all(index < other_index for other_index, _ in partial_events + closing_events),
                      'trigger_event_precedes_exit_events:' + cid)
                check(int(ev['timestamp_ms']) == ns // 1_000_000, 'evaluation_clock:' + cid)
                before = sum((number(part[0]['qty']) for t, part in parts.items() if t < ns), D(0))
                q = q0 - before
                equal(q, ev['position_qty'], 'trigger_remaining_qty:' + cid)
                executable = number(ev['executable_qty'])
                check(0 < executable <= q and executable % 10 == 0, 'executable_fragment_qty:' + cid)
                basis = q * prices[long]
                tp, sl = basis * D(2) / 10000, -basis * D(2) / 10000
                check(ev['risk_basis'] == BASIS, 'evaluation_risk_basis:' + cid)
                for computed, recorded, label in ((basis, ev['risk_basis_notional'], 'basis_notional'),
                    (2, ev['take_profit_bps'], 'tp_bps'), (2, ev['stop_loss_bps'], 'sl_bps'),
                    (tp, ev['take_profit_threshold_usdt'], 'tp_threshold'),
                    (sl, ev['stop_loss_threshold_usdt'], 'sl_threshold')):
                    equal(computed, recorded, label + ':' + cid)
                check(set(ev['prices']) == set(VENUES), 'both_evaluation_quotes:' + cid)
                gross, fees, net = D(0), D(0), D(0)
                for venue in VENUES:
                    exit_price = number(ev['prices'][venue])
                    check(exit_price > 0, 'positive_risk_quote:' + cid)
                    leg_gross = q * (exit_price - prices[venue]) * (1 if venue == long else -1)
                    leg_fee = q * (prices[venue] + exit_price) * fee_rates[venue]
                    gross += leg_gross
                    fees += leg_fee
                    net += leg_gross
                    net -= leg_fee
                for computed, recorded, label in ((gross, ev['estimated_position_gross_pnl'], 'gross'),
                    (fees, ev['estimated_position_fees'], 'fees'), (net, ev['estimated_position_pnl'], 'net'),
                    (net * executable / q, ev['executable_fragment_net_pnl'], 'fragment_net')):
                    equal(computed, recorded, 'risk_' + label + ':' + cid)
                expected_reason = 'stop_loss' if net <= sl else 'take_profit' if net >= tp else None
                check(reason == expected_reason, 'net_inclusive_trigger_condition:' + cid)
                check(ev['mode'] == ('full_budget_executable_pnl' if executable == q else 'partial_depth_position_pnl_proxy'),
                      'evaluation_mode:' + cid)
                if ns in parts:
                    check(number(parts[ns][0]['qty']) <= executable, 'trigger_fill_within_fragment:' + cid)
                    for row in parts[ns]:
                        equal(ev['prices'][row['venue']], row['price'], 'trigger_quote_equals_same_cursor_fill:' + row['order_id'])
                trigger_counts[reason] += 1
                triggers.append({'cycle_id': cid, 'decision_cursor_ns': ns, 'reason': reason, 'long_venue': long,
                    'remaining_qty': str(q), 'basis_notional': str(basis), 'executable_qty': str(executable),
                    'take_profit_threshold_usdt': str(tp), 'stop_loss_threshold_usdt': str(sl),
                    'recomputed_gross_pnl': str(gross), 'recomputed_fees': str(fees), 'recomputed_net_pnl': str(net),
                    'same_cursor_fill_verified': ns in parts, 'exit_parts': len(parts)})
        except (KeyError, ValueError, TypeError, ArithmeticError, StopIteration) as exc:
            check(False, 'malformed_cycle_evidence:' + cid, type(exc).__name__ + ': ' + str(exc))

    return {'status': 'PASS' if not failures else 'FAIL', 'risk_basis': BASIS, 'risk_pnl_basis': dict(PNL_BASIS),
            'checks': dict(counts), 'failures': failures, 'trigger_counts': dict(trigger_counts), 'triggers': triggers,
            'input_counts': {'orders': len(orders), 'cycles': len(cycles), 'relevant_events': len(events)},
            'verification_scope': 'All logged TP/SL triggers and every closed cycle reason; independent order-derived remaining notional, paired gross/fee/net arithmetic, inclusive fee-net conditions before funding, same-cursor fills, partial-exit quantities and sticky reasons. Does not search unlogged opportunities or reconstruct native books.'}


def main(path):
    path = Path(path).resolve()
    started = time.monotonic()
    status = json.loads((path / 'status.json').read_text())
    if status['status'] != 'COMPLETED':
        print(json.dumps({'status': 'NOT_RUN_NONCOMPLETED_REPLAY', 'run_status': status['status']}))
        return 3
    target = path / 'risk-audit.json'
    if target.exists():
        raise RuntimeError('refuse_to_overwrite_existing_risk_audit')
    metadata = json.loads((path / 'run_metadata.json').read_text())
    with sqlite3.connect('file:' + str(path / 'ledger.sqlite') + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        state = json.loads(db.execute('SELECT value FROM latest_state WHERE id=1').fetchone()[0])
        orders = [json.loads(r[0]) for r in db.execute('SELECT value FROM orders ORDER BY id')]
        cycles = [json.loads(r[0]) for r in db.execute('SELECT value FROM cycles ORDER BY id')]
        events = []
        for row in db.execute('SELECT value FROM events ORDER BY id'):
            event = json.loads(row[0])
            if event.get('kind') in RELEVANT_EVENTS:
                events.append(event)
        result = audit_records(metadata, state, orders, cycles, events)
    code = Path(__file__).resolve().parent
    executions = {str(code / name): metadata[key] for name, key in
                  (('runner.py', 'runner_sha256'), ('paper_snapshot.py', 'risk_snapshot_sha256'),
                   ('risk_policy.py', 'risk_policy_sha256'), ('tardis_adapter.py', 'adapter_sha256'))}
    hashes = {**metadata['source_code_sha256'], **executions}
    for source, expected in hashes.items():
        try:
            actual = digest(source)
        except OSError as exc:
            actual = type(exc).__name__
        result['checks']['checks'] += 1
        if actual != expected:
            result['status'] = 'FAIL'
            result['checks']['failures'] = result['checks'].get('failures', 0) + 1
            result['failures'].append({'check': 'frozen_source_hash', 'detail': source})
    result.update(run_status=status['status'], read_only_audit=True,
                  created_utc=datetime.now(timezone.utc).isoformat(), elapsed_seconds=time.monotonic() - started,
                  run_metadata_sha256=digest(path / 'run_metadata.json'),
                  source_code_sha256=metadata['source_code_sha256'], execution_code_sha256=executions,
                  audit_script=str(Path(__file__).resolve()), audit_script_sha256=digest(__file__))
    # Write only the audit artifact; the replay and its inputs remain untouched.
    with target.open('x') as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    print(json.dumps({k: result[k] for k in ('status', 'run_status', 'trigger_counts', 'checks', 'failures', 'elapsed_seconds')}))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('run', type=Path)
    args = cli.parse_args()
    raise SystemExit(main(args.run))
