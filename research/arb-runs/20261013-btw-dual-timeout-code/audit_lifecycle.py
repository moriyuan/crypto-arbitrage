"""Independent lifecycle accounting, timing, native-data and funding audit.

No production modules are imported. All money is integer price units * 1e-4;
source maker executions start funding exposure, not their later notifications.
"""
from collections import Counter, defaultdict
from decimal import Decimal as D
from pathlib import Path
import argparse
import csv
import hashlib
import json
import struct
import time

NS = 10**9
LATENCY = 10**8
HEADER = struct.Struct('<qBBBBI8q')
UPDATE = struct.Struct('<B7xqq')
TRADE = struct.Struct('<qqBB6xqqqII8x')
ACTIVITY = struct.Struct('<qIIqqqqQ')
VENUES = ('bybit', 'bitget')


def digest(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def read_lines(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def check(value, *details):
    if not value:
        raise AssertionError(details)


def qty_sum(events, intent):
    return sum(e['qty'] for e in events if e['type'] == 'fill' and e['intent'] == intent)


def funding_intersections(open_ns, end_ns, venue, times):
    """Equality is conservatively counted as settlement exposure."""
    return [ts for ts in times[venue] if open_ns <= ts <= end_ns]


def remaining_is_tradeable(total_qty, close_qty, prices, min_notional, quantity_step):
    """No close may create a new untradeable remainder at current prices."""
    remaining = total_qty - close_qty
    return remaining == 0 or (remaining > 0 and remaining % quantity_step == 0
                             and all(remaining * price >= min_notional for price in prices))


def audit(directory):
    begun = time.monotonic()
    root = Path(directory)
    meta = read_json(root / 'summary.json')
    check(meta['execution_enabled'] is False and meta['as_pricing_enabled'] is False, 'public offline only')
    check(meta['portfolio_pnl_computed'] is False, 'independent opportunities')
    pd = meta['price_schema']['price_decimals']
    check(pd in (5, 6), 'supported price units')
    money, scale = 10**(pd + 4), 10**(8 - pd)
    policy = meta['policy']
    ut = policy['unhedged_timeout_ms'] * 10**6
    ht = policy['hedged_hold_ms'] * 10**6
    for name, spec in meta['inputs'].items():
        check(digest(spec['path']) == spec['sha256'], 'input hash', name)
    check(digest(meta['source_summary']) == meta['source_summary_sha256'], 'source summary hash')
    for path, expected in meta['code_sha256'].items():
        check(digest(path) == expected, 'production source hash', path)
    rows = read_lines(root / 'cycles.jsonl')
    events = read_lines(root / 'events.jsonl')
    snapshots = {s['cycle_id']: s for s in read_lines(meta['inputs']['entry_snapshots']['path'])}
    check(len(rows) == meta['candidate_count'] == len(snapshots), 'candidate count')
    check(len(events) == meta['event_count'], 'event count')
    check(set(r['cycle'] for r in rows) == set(snapshots), 'candidate identities')
    check(len({r['cycle'] for r in rows}) == len(rows), 'unique opportunity')
    bycycle = defaultdict(list)
    for event in events:
        bycycle[event['cycle']].append(event)
    cohort = Path(meta['inputs']['entry_snapshots']['path']).parent
    funding_pointer = read_json(cohort.parent / 'funding-avoidance-audit.json')
    funding_path = Path(funding_pointer['events_path'])
    check(digest(funding_path) == funding_pointer['events_sha256'], 'funding events hash')
    funding = read_json(funding_path)
    check(funding['target_day_utc'] == meta['date_utc'], 'funding date')
    funding_times = {}
    for venue in VENUES:
        fv = funding['venues'][venue]
        check(fv['status'] == 'BRACKETED_HISTORY', 'funding bracket', venue)
        funding_times[venue] = [e['timestamp_ms'] * 10**6 for e in fv['selected_events']]
    source_expected = defaultdict(list)
    scan_events = defaultdict(list)
    starts = defaultdict(list)
    cycle_by_id = {r['cycle']: r for r in rows}
    audit_states = {}
    checks = Counter()
    closed_intervals, residual_intervals = [], []
    cycle_economics = []
    for row in rows:
        cid = row['cycle']
        ev = bycycle[cid]
        s = snapshots[cid]
        p = s['prices']
        maker = 0 if p[0] - p[1] >= p[2] - p[3] else 1
        hedge = 1 - maker
        lng = VENUES.index(s['long'])
        sign = 1 if maker == lng else -1
        side = 'buy' if sign == 1 else 'sell'
        quote = p[2 * maker] - policy['counterparty_offset_ticks'] * meta['price_schema']['native_tick_units'][maker] if sign == 1 else p[2 * maker + 1] + policy['counterparty_offset_ticks'] * meta['price_schema']['native_tick_units'][maker]
        check(row['signal_ns'] == s['entry_ns'] and meta['evaluation_start_ns'] <= s['entry_ns'] < meta['evaluation_end_ns_exclusive'], 'causal entry', cid)
        check(row['maker_venue'] == VENUES[maker] and row['maker_side'] == side and row['long_venue'] == VENUES[lng], 'maker selection', cid)
        check(row['quote_price_units'] == quote and row['target_qty'] == s['qty'], 'fixed quote', cid)
        check(row['as_model_used'] is False and row['portfolio_aggregation_valid'] is False, 'scope', cid)
        check(row['policy'] == policy, 'policy', cid)
        check(len(ev) == row['event_count'] and ev[0]['type'] == 'quote_request', 'cycle event count', cid)
        check(ev[0]['ns'] == row['signal_ns'] and ev[0]['arrival_target_ns'] == row['signal_ns'] + LATENCY, 'quote request', cid)
        check(ev == sorted(ev, key=lambda x: x['ns']), 'monotonic events', cid)
        opening = {}
        lots = {}
        pairs = {}
        hedge_allocations = {}
        hedge_pair_qty = Counter()
        paired_closes = {}
        positions = [0, 0]
        gross_total = [0, 0]
        fees_total = [0, 0]
        first_hedge = paired_deadline = None
        pair_request = None
        cancel = None
        maker_qty = hedge_qty = single_closed = paired_closed = 0
        pair_baseline = pair_long_notional = 0
        paired_entry_fee = paired_close_fee = 0
        paired_gross = [0, 0]
        next_fill_id = 1
        for e in ev:
            kind, ns = e['type'], e['ns']
            if kind == 'quote_arrival':
                check(ns == row['arrival_ns'] and ns >= row['signal_ns'] + LATENCY, 'arrival latency', cid)
                check(e['ttl_ns'] == ns + 2 * NS, 'ttl', cid)
                scan_events[ns].append((cid, e))
            elif kind == 'cancel_request':
                check(cancel is None and e['effective_ns'] == ns + LATENCY, 'cancel latency', cid)
                cancel = e
            elif kind == 'cancel_ack':
                check(cancel is not None and ns == cancel['effective_ns'] == row['cancel_ack_ns'], 'cancel acknowledgement', cid)
            elif kind == 'unhedged_exit_request':
                lot = lots[e['lot_id']]
                check(lot['unhedged_qty'] > 0 and lot['request'] is None, 'single request remaining', cid)
                check(e['qty'] == lot['unhedged_qty'] and e['unhedged_deadline_ns'] == lot['deadline'], 'single request evidence', cid)
                check(e['maker_receive_ns'] == lot['receive_ns'] and e['earliest_execution_ns'] == ns + LATENCY, 'single request causal', cid)
                if e['reason'] == 'unhedged_timeout':
                    check(ns == lot['deadline'], 'single fixed timeout', cid)
                lot['request'] = e
            elif kind == 'paired_exit_request':
                check(pair_request is None and paired_deadline is not None, 'pair request exists', cid)
                check(e['first_hedge_ns'] == first_hedge and e['paired_deadline_ns'] == paired_deadline and e['earliest_execution_ns'] == ns + LATENCY, 'pair immutable timeout/latency', cid)
                check(e['qty'] == sum(x['qty'] for x in pairs.values()), 'pair requested qty', cid)
                if e['reason'] == 'holding_timeout':
                    check(ns == paired_deadline, 'paired fixed timeout', cid)
                if e.get('mark') is not None:
                    mark = e['mark']
                    check(mark['initial_reverse_l1_baseline_units'] == pair_baseline, 'risk baseline', cid)
                    check(mark['initial_long_entry_notional_price_units'] == pair_long_notional, 'risk denominator', cid)
                    cg = sum(q['qty'] * 10000 * (sign * (mark['close_prices_units'][maker] - q['maker_px']) - sign * (mark['close_prices_units'][hedge] - q['hedge_px'])) for q in pairs.values())
                    future_fees = 3 * sum(q['qty'] for q in pairs.values()) * sum(mark['close_prices_units'])
                    net = sum(paired_gross) + cg - paired_entry_fee - paired_close_fee - future_fees
                    check(mark['net_before_funding_units'] == net and mark['remaining_gross_units'] == cg and mark['estimated_close_fee_units'] == future_fees, 'risk mark recomputation', cid)
                    reasons = ('hard_stop_loss' if net <= -policy['paired_hard_loss_bps'] * pair_long_notional else 'stop_loss' if net <= pair_baseline - policy['paired_entry_deterioration_bps'] * pair_long_notional else 'take_profit' if net >= policy['paired_take_profit_bps'] * pair_long_notional else None)
                    check(e['reason'] == reasons, 'risk reason priority', cid)
                    scan_events[ns].append((cid, e))
                    checks['risk_marks'] += 1
                pair_request = e
            elif kind == 'pair_formed':
                h = opening[e['hedge_fill_id']]
                m = opening[e['maker_fill_id']]
                check(e['paired_group_id'] not in pairs and e['qty'] > 0, 'pair identity', cid)
                check(ns == h['receive_ns'] == e['pair_formed_ns'], 'pair formed when hedge known', cid)
                check(e['first_hedge_ns'] == first_hedge and e['paired_deadline_ns'] == paired_deadline, 'partial pair never extends hold', cid)
                expected_qty = hedge_allocations[e['hedge_fill_id']][e['maker_fill_id']]
                check(e['qty'] == expected_qty, 'pair source allocation', cid)
                pairs[e['paired_group_id']] = {'qty': e['qty'], 'maker_fill': e['maker_fill_id'], 'hedge_fill': e['hedge_fill_id'], 'maker_px': m['px'], 'hedge_px': h['px'], 'initial_qty': e['qty']}
                hedge_pair_qty[e['hedge_fill_id']] += e['qty']
                pair_baseline += e['initial_reverse_l1_net_units']
                lp = m['px'] if maker == lng else h['px']
                check(e['long_entry_notional_price_units'] == lp * e['qty'], 'pair long denominator', cid)
                pair_long_notional += lp * e['qty']
                scan_events[ns].append((cid, e))
            elif kind == 'fill':
                fid, qty, px, venue = e['fill_id'], e['qty'], e['price_units'], e['venue']
                intent = e['intent']
                check(fid == next_fill_id, 'sequential fill IDs', cid, fid)
                next_fill_id += 1
                check(type(qty) is int and qty > 0 and type(px) is int and px > 0, 'exact quantity and price', cid)
                check(e['fee_units'] == 3 * px * qty, 'fee 3bp', cid, fid)
                fees_total[venue] += e['fee_units']
                position_sign = 1 if e['side'] == 'buy' else -1
                positions[venue] += position_sign * qty
                check(e['venue_position_after'] == positions[venue], 'position after fill', cid, fid)
                calc_gross = 0
                if intent == 'maker_open':
                    check(venue == maker and e['side'] == side and px == quote, 'maker fill', cid)
                    check(e['lot_id'] == fid and e['unhedged_deadline_ns'] == ns + ut and e['hedge_eligible_ns'] == ns + LATENCY, 'maker lot timer', cid)
                    check(row['arrival_ns'] <= e['source_ns'] < row['cancel_ack_ns'] and 0 <= ns - e['source_ns'] <= NS, 'source valid across cancel', cid)
                    quantum = 10 if maker == 0 else 1
                    check(qty == min(row['target_qty'] - maker_qty, e['trade_qty_1e8'] // (4 * 10**8 * quantum) * quantum), 'strict exact participation', cid)
                    maker_qty += qty
                    lots[fid] = {'unhedged_qty': qty, 'receive_ns': ns, 'deadline': ns + ut, 'request': None}
                    opening[fid] = {'venue': venue, 'side': e['side'], 'sign': position_sign, 'px': px, 'qty': qty, 'receive_ns': ns, 'exposure_start_ns': e['source_ns']}
                    source_expected[(ns, venue, e['source_sequence'], e['row_ordinal'])].append((row, e))
                elif intent == 'hedge_open':
                    check(venue == hedge and position_sign == -sign and qty % 10 == 0, 'hedge side/common lot', cid)
                    check(qty * px >= 5 * 10**pd, 'hedge minimum', cid)
                    check(pair_request is None, 'no hedge after paired exit request', cid)
                    check(sum(a['qty'] for a in e['allocations']) == qty, 'hedge allocations', cid)
                    hedge_allocations[fid] = {}
                    for a in e['allocations']:
                        lot = lots[a['lot_id']]
                        check(a['lot_id'] == a['maker_fill_id'] and 0 < a['qty'] <= lot['unhedged_qty'], 'hedge lot allocation', cid)
                        check(lot['request'] is None and lot['receive_ns'] + LATENCY <= ns < lot['deadline'], 'hedge causal eligibility', cid)
                        check(a['latency_ns'] == ns - lot['receive_ns'], 'hedge reported latency', cid)
                        lot['unhedged_qty'] -= a['qty']
                        hedge_allocations[fid][a['maker_fill_id']] = a['qty']
                    opening[fid] = {'venue': venue, 'side': e['side'], 'sign': position_sign, 'px': px, 'qty': qty, 'receive_ns': ns, 'exposure_start_ns': ns}
                    hedge_qty += qty
                    paired_entry_fee += 3 * qty * (quote + px)
                    if first_hedge is None:
                        first_hedge, paired_deadline = ns, ns + ht
                    scan_events[ns].append((cid, e))
                elif intent == 'unhedged_close':
                    check(venue == maker and position_sign == -sign and qty % (10 if maker == 0 else 1) == 0, 'single reversing close', cid)
                    check(qty * px >= 5 * 10**pd and sum(a['qty'] for a in e['allocations']) == qty, 'single minimum/allocation', cid)
                    check(remaining_is_tradeable(sum(l['unhedged_qty'] for l in lots.values()), qty, [px], 5*10**pd, 10 if maker == 0 else 1), 'single exit must not manufacture dust, including known young lots', cid, fid)
                    checks['no_new_dust_single_exit'] += 1
                    for a in e['allocations']:
                        lot = lots[a['lot_id']]
                        op = opening[a['maker_fill_id']]
                        req = lot['request']
                        check(req is not None and ns >= req['earliest_execution_ns'] == a['earliest_execution_ns'], 'single future latency', cid)
                        check(a['trigger_ns'] == req['ns'] and a['reason'] == req['reason'], 'single request identity', cid)
                        check(0 < a['qty'] <= lot['unhedged_qty'] and a['entry_price_units'] == op['px'], 'single close quantity', cid)
                        g = op['sign'] * (px - op['px']) * a['qty'] * 10000
                        check(a['gross_realized_units'] == g, 'single gross', cid)
                        calc_gross += g
                        lot['unhedged_qty'] -= a['qty']
                        op['qty'] -= a['qty']
                        closed_intervals.append({'cycle': cid, 'fill_id': a['maker_fill_id'], 'venue': VENUES[venue], 'qty': a['qty'], 'entry_ns': op['exposure_start_ns'], 'exit_ns': ns})
                    single_closed += qty
                    scan_events[ns].append((cid, e))
                elif intent == 'paired_close':
                    check(pair_request is not None and ns >= pair_request['earliest_execution_ns'] == e['earliest_execution_ns'], 'paired future latency', cid)
                    check(e['trigger_ns'] == pair_request['ns'] and e['reason'] == pair_request['reason'], 'paired request identity', cid)
                    check(position_sign == (-1 if venue == lng else 1) and qty % 10 == 0 and qty * px >= 5 * 10**pd, 'pair close side/lot/minimum', cid)
                    check(sum(a['qty'] for a in e['allocations']) == qty, 'pair close allocations', cid)
                    check(remaining_is_tradeable(sum(p['qty'] for p in pairs.values()), qty, [px], 5*10**pd, 10), 'paired exit must not manufacture dust at either venue', cid, fid)
                    checks['no_new_dust_paired_exit_leg'] += 1
                    group = paired_closes.setdefault(e['close_group_id'], [])
                    check(all(old['venue'] != venue for old in group), 'one close leg per venue', cid)
                    group.append(e)
                    for a in e['allocations']:
                        pair = pairs[a['paired_group_id']]
                        op = opening[a['entry_fill_id']]
                        check(a['entry_fill_id'] == pair['maker_fill' if venue == maker else 'hedge_fill'], 'pair entry identity', cid)
                        check(0 < a['qty'] <= pair['qty'] and a['entry_price_units'] == op['px'], 'pair remaining allocation', cid)
                        g = op['sign'] * (px - op['px']) * a['qty'] * 10000
                        check(g == a['gross_realized_units'], 'pair gross', cid)
                        calc_gross += g
                        op['qty'] -= a['qty']
                        check(op['qty'] >= 0, 'opening lot not overclosed', cid)
                        closed_intervals.append({'cycle': cid, 'fill_id': a['entry_fill_id'], 'venue': VENUES[venue], 'qty': a['qty'], 'entry_ns': op['exposure_start_ns'], 'exit_ns': ns})
                    if len(group) == 2:
                        x, y = group
                        check(x['qty'] == y['qty'] and x['ns'] == y['ns'], 'same qty simultaneous paired close', cid)
                        check([(a['paired_group_id'], a['qty']) for a in x['allocations']] == [(a['paired_group_id'], a['qty']) for a in y['allocations']], 'same paired lot allocation', cid)
                        for a in e['allocations']:
                            pairs[a['paired_group_id']]['qty'] -= a['qty']
                        paired_closed += qty
                    paired_close_fee += e['fee_units']
                    paired_gross[venue] += calc_gross
                    scan_events[ns].append((cid, e))
                else:
                    raise AssertionError(('unknown fill intent', intent))
                check(e['gross_realized_units'] == calc_gross, 'fill gross', cid, fid)
                gross_total[venue] += calc_gross
                checks[intent + '_fills'] += 1
                checks['fees_3bp'] += 1
            elif kind not in ('quote_request', 'quote_rejected', 'archive_ended'):
                raise AssertionError(('unknown event', kind))
        check(all(len(g) == 2 for g in paired_closes.values()), 'complete paired close groups', cid)
        check(all(hedge_pair_qty[fid] == sum(a.values()) for fid, a in hedge_allocations.items()), 'all hedged portions paired', cid)
        unhedged_left = sum(l['unhedged_qty'] for l in lots.values())
        paired_left = sum(p['qty'] for p in pairs.values())
        check(maker_qty == unhedged_left + paired_left + single_closed + paired_closed, 'maker inventory conservation', cid)
        check(hedge_qty == paired_left + paired_closed, 'hedge inventory conservation', cid)
        check(positions[maker] == sign * (unhedged_left + paired_left) and positions[hedge] == -sign * paired_left, 'venue residual conservation', cid)
        for name, value in {'maker_open_qty': maker_qty, 'hedge_open_qty': hedge_qty, 'unhedged_closed_qty': single_closed, 'paired_closed_qty': paired_closed, 'unhedged_open_qty': unhedged_left, 'paired_open_qty': paired_left, 'first_hedge_ns': first_hedge, 'paired_deadline_ns': paired_deadline, 'paired_risk_baseline_units': pair_baseline, 'paired_initial_long_notional_price_units': pair_long_notional}.items():
            check(row[name] == value, 'reported lifecycle total', cid, name)
        check(row['venue_positions'] == dict(zip(VENUES, positions)), 'reported positions', cid)
        for i, venue in enumerate(VENUES):
            check(D(row['venue_gross_realized'][venue]) * money == gross_total[i], 'venue gross', cid, venue)
            check(D(row['venue_fees'][venue]) * money == fees_total[i], 'venue fees', cid, venue)
            check(D(row['venue_net_before_funding'][venue]) * money == gross_total[i] - fees_total[i], 'venue net', cid, venue)
        check(D(row['gross_realized']) * money == sum(gross_total) and D(row['fees']) * money == sum(fees_total), 'total accounting', cid)
        check(D(row['net_before_funding']) * money == sum(gross_total) - sum(fees_total), 'net accounting', cid)
        check(D(row['paired_entry_fees']) * money == paired_entry_fee and D(row['paired_close_fees']) * money == paired_close_fee, 'pair fee allocation', cid)
        if row['state'] in ('DONE', 'REJECTED'):
            check(positions == [0, 0], 'terminal flat per venue', cid)
        check(not row['pnl_complete_before_funding'] or positions == [0, 0], 'complete pnl requires flatness', cid)
        check(sum(r['qty'] for r in row['residual_unhedged_lots']) == unhedged_left and sum(r['qty'] for r in row['residual_pairs']) == paired_left, 'residual rows', cid)
        for fid, op in opening.items():
            if op['qty']:
                residual_intervals.append({'cycle': cid, 'fill_id': fid, 'venue': VENUES[op['venue']], 'qty': op['qty'], 'entry_ns': op['exposure_start_ns'], 'exit_ns': None, 'interval_kind': 'residual_until_last_source_timestamp'})
        cycle_economics.append({'cycle': cid, 'state': row['state'], 'gross_realized': row['gross_realized'], 'fees_paid': row['fees'], 'realized_gross_less_all_paid_fees': row['net_before_funding'], 'unhedged_residual_qty': unhedged_left, 'paired_residual_qty': paired_left, 'unclosed_opening_fees': str(D(sum(3 * op['px'] * op['qty'] for op in opening.values())) / money), 'pnl_complete_before_funding': row['pnl_complete_before_funding']})
        starts[row['signal_ns']].append(cid)
        audit_states[cid] = {'row': row, 'maker': maker, 'hedge': hedge, 'long': lng, 'sign': sign, 'quote': quote, 'last_ns': max(e['ns'] for e in ev)}
        checks['opportunities'] += 1
    # Scan ordinary source trades once; historical Bitget snapshots never enter this file.
    seen_sources = set()
    trade_count = 0
    trade_first = trade_last = None
    max_lag = 0
    with open(meta['inputs']['trades']['path'], 'rb') as stream:
        check(stream.read(8) == b'BTWTRD01', 'trade magic')
        while raw := stream.read(64):
            check(len(raw) == 64, 'complete trade record')
            ns, source, venue, aggressor, price, qty, seq, ordinal, flags = TRADE.unpack(raw)
            if trade_first is None:
                trade_first = ns
            check(trade_last is None or ns >= trade_last, 'trade receive order')
            trade_last = ns
            check(0 <= ns - source <= NS, 'bounded trade delay')
            max_lag = max(max_lag, ns - source)
            trade_count += 1
            key = (ns, venue, seq, ordinal)
            for row, event in source_expected.get(key, []):
                check(source == event['source_ns'] and price == event['trade_price_1e8'] and qty == event['trade_qty_1e8'], 'native source tuple', key)
                check(flags == 0 and aggressor == (1 if row['maker_side'] == 'buy' else 0), 'ordinary aggressor', key)
                check(price < row['quote_price_units'] * scale if row['maker_side'] == 'buy' else price > row['quote_price_units'] * scale, 'strict through', key)
                seen_sources.add((row['cycle'], event['fill_id']))
    check(len(seen_sources) == checks['maker_open_fills'], 'all maker fills traceable')
    check(trade_count == meta['source_live_trades'], 'all live trades counted')
    # A different representation from production: consumed debt against visible quantity.
    # Include the original cohort's existing debt and validate pre-entry snapshots before
    # that cohort consumes its current entry. Every opportunity then owns independent debt.
    orders_at = defaultdict(list)
    with (cohort / 'orders.csv').open() as stream:
        for order in csv.DictReader(stream):
            orders_at[int(order['ns'])].append(order)
    thresholds = {}
    with open(meta['inputs']['thresholds']['path']) as stream:
        for r in csv.DictReader(stream):
            if r['name'] == 'q25':
                thresholds = {k: int(v) for k, v in r.items() if k != 'name'}
    check(bool(thresholds), 'q25 threshold present')
    display_book = [{} for _ in range(4)]
    debt = [{} for _ in range(4)]
    active = {}
    tape_count = 0
    tape_first = tape_last = None
    source_checks = Counter()
    with open(meta['inputs']['tape']['path'], 'rb') as tape, open(meta['inputs']['activity']['path'], 'rb') as activity:
        check(tape.read(8) == (b'BTWTP002' if pd == 5 else b'BTWTP003'), 'book precision magic')
        check(activity.read(8) == b'BTWACT01', 'activity magic')
        while raw := tape.read(80):
            ns, valid, force, hist, pad, count, *values = HEADER.unpack(raw)
            if tape_first is None:
                tape_first = ns
            check(tape_last is None or ns > tape_last, 'tape strict monotonic')
            tape_last = ns
            tape_count += 1
            updates = tape.read(count * 24)
            check(len(updates) == count * 24, 'complete depth updates')
            ar = ACTIVITY.unpack(activity.read(56))
            check(ar[0] == ns, 'exact activity timestamp alignment')
            px, shown = values[:4], values[4:]
            if valid:
                for side, price, qty in UPDATE.iter_unpack(updates):
                    if not qty:
                        display_book[side].pop(price, None)
                        debt[side].pop(price, None)
                    else:
                        display_book[side][price] = qty
                        debt[side][price] = min(debt[side].get(price, 0), qty)
                    for st in active.values():
                        book = st['book'][side]
                        if not qty:
                            book.pop(price, None)
                        else:
                            book[price] = [qty, min(book.get(price, (0, 0))[1], qty)]
            for cid in starts.get(ns, []):
                s = snapshots[cid]
                check(valid and hist and not force and px == s['prices'], 'cohort price and guard', cid)
                gate = ar[7] == 3 and ar[1] >= thresholds['countA'] and ar[2] >= thresholds['countB'] and ar[3] >= thresholds['turnoverAunits'] and ar[4] >= thresholds['turnoverBunits'] and 0 <= ar[5] <= thresholds['maxAgeNs'] and 0 <= ar[6] <= thresholds['maxAgeNs']
                check(gate, 'exact causal entry activity', cid)
                for side, levels in enumerate(s['sides']):
                    expected = {p: (q, q - debt[side].get(p, 0)) for p, q in display_book[side].items()}
                    got = {l['price_units_1e8']: (l['displayed_qty'], l['remaining_quarter']) for l in levels}
                    check(got == expected, 'pre-entry budget full provenance', cid, side)
                active[cid] = {'book': [{l['price_units_1e8']: [l['displayed_qty'], l['displayed_qty'] - l['remaining_quarter']] for l in levels} for levels in s['sides']], 'hedge_notional': 0}
                source_checks['entry_guard_and_snapshot'] += 1
            for cid, e in scan_events.get(ns, []):
                st = active[cid]
                info = audit_states[cid]
                check(valid, 'actual quote/fill uses valid fresh paired book', cid, e['type'])
                if e['type'] == 'quote_arrival':
                    v = info['maker']
                    check(e['own_ask_price_units'] == px[2*v] and e['own_bid_price_units'] == px[2*v+1], 'arrival opposing quote', cid)
                    check(info['quote'] < px[2*v] if info['sign'] == 1 else info['quote'] > px[2*v+1], 'postonly arrival', cid)
                    source_checks['arrivals'] += 1
                elif e['type'] == 'pair_formed':
                    m, h, sign = info['maker'], info['hedge'], info['sign']
                    fills = {f['fill_id']: f for f in bycycle[cid] if f['type'] == 'fill'}
                    mp, hp = fills[e['maker_fill_id']]['price_units'], fills[e['hedge_fill_id']]['price_units']
                    cm, ch = px[2*m+(1 if sign == 1 else 0)], px[2*h+(0 if sign == 1 else 1)]
                    baseline = e['qty'] * (10000 * (sign*(cm-mp) - sign*(ch-hp)) - 3*(mp+hp+cm+ch))
                    check(e['initial_reverse_l1_net_units'] == baseline, 'pair initial baseline at actual hedge book', cid)
                    source_checks['pair_baselines'] += 1
                elif e['type'] == 'paired_exit_request':
                    mark = e['mark']
                    sides = [2*v+(1 if v == info['long'] else 0) for v in (0,1)]
                    check(mark['close_prices_units'] == [px[s] for s in sides], 'risk prices at current L1', cid)
                    for side in sides:
                        shownqty, used = st['book'][side][px[side]*scale]
                        check(shownqty == shown[side] and shownqty-used >= 4*mark['paired_qty'], 'risk mark full exit budget', cid)
                        check(mark['paired_qty']*px[side] >= 5*10**pd, 'risk mark minimum', cid)
                    source_checks['executable_risk_marks'] += 1
                else:
                    venue = e['venue']
                    side = 2*venue+(0 if e['side'] == 'buy' else 1)
                    check(e['price_units'] == px[side], 'opposing L1 fill price', cid, e['fill_id'])
                    shownqty, used = st['book'][side][px[side]*scale]
                    check(shownqty == shown[side] and 4*e['qty'] <= shownqty-used, 'actual fill within residual depth budget', cid, e['fill_id'])
                    st['book'][side][px[side]*scale][1] += 4*e['qty']
                    if e['intent'] == 'hedge_open':
                        st['hedge_notional'] += e['qty']*e['price_units']
                        check(st['hedge_notional'] <= 250*10**pd, 'hedge maximum notional', cid)
                        check(not force, 'hedge never opens in funding/day guard', cid)
                    source_checks[e['intent']+'_depth_fills'] += 1
            for order in orders_at.get(ns, []):
                side = 2*VENUES.index(order['venue'])+(0 if order['side'] == 'buy' else 1)
                q = int(D(order['qty']))
                price = int(D(order['price'])*10**pd)
                key = price*scale
                check(valid and px[side] == price and 4*q <= display_book[side][key]-debt[side].get(key,0), 'source cohort original budget', ns)
                debt[side][key] = debt[side].get(key,0)+4*q
                source_checks['source_cohort_order_depth'] += 1
            active = {cid: st for cid, st in active.items() if audit_states[cid]['last_ns'] > ns}
        check(not activity.read(1), 'no extra activity records')
    check(tape_count == meta['source_decisions'], 'all decisions scanned')
    check(source_checks['entry_guard_and_snapshot'] == len(rows), 'all entries seen')
    for intent in ('hedge_open','unhedged_close','paired_close'):
        check(source_checks[intent+'_depth_fills'] == checks[intent+'_fills'], 'all taker fills found', intent)
    check(source_checks['arrivals'] == sum(e['type']=='quote_arrival' for e in events), 'all arrivals found')
    # Residual holdings are followed only to the last actually observed source
    # timestamp. The next settlement remains an unresolved future obligation;
    # absence of a post-midnight observation must not be called a seen crossing.
    archive_last_ns = max(tape_last, trade_last)
    for interval in residual_intervals:
        interval['exit_ns'] = archive_last_ns
    crossings = []
    for interval in closed_intervals + residual_intervals:
        times = funding_intersections(interval['entry_ns'], interval['exit_ns'], interval['venue'], funding_times)
        if times:
            crossings.append({**interval, 'settlement_ns': times})
    closed_crossings = [i for i in crossings if 'interval_kind' not in i]
    residual_crossings = [i for i in crossings if 'interval_kind' in i]
    crossing_cycles = {i['cycle'] for i in crossings}
    closed_crossing_cycles = {i['cycle'] for i in closed_crossings}
    for row in cycle_economics:
        row['funding_status'] = ('UNRESOLVED_SETTLEMENT_MARK_PRICE_MISSING' if row['cycle'] in crossing_cycles else 'VERIFIED_NO_SETTLEMENT_IN_OBSERVED_INTERVALS')
        row['closed_parts_funding_status'] = ('UNRESOLVED_SETTLEMENT_MARK_PRICE_MISSING' if row['cycle'] in closed_crossing_cycles else 'VERIFIED_NO_SETTLEMENT')
    funding_audit = {'status': 'VERIFIED_NO_SETTLEMENT' if not crossings else 'UNRESOLVED_SETTLEMENT_MARK_PRICE_MISSING',
        'execution_enabled': False, 'source_events_path': str(funding_path), 'source_events_sha256': digest(funding_path),
        'old_avoidance_pointer_path': str(cohort.parent/'funding-avoidance-audit.json'),
        'old_avoidance_conclusion_reused': False, 'closed_partial_leg_intervals': len(closed_intervals),
        'residual_leg_intervals': len(residual_intervals), 'crossings': crossings,
        'closed_partial_leg_crossings': closed_crossings,
        'residual_leg_crossings': residual_crossings,
        'archive_last_source_ns': archive_last_ns,
        'basis': 'Maker source execution timestamp (not receive time), hedge execution ns, each allocated partial close ns; inclusive endpoints. Residuals extend to last observed source timestamp, not an unobserved next-midnight event.',
        'exact_funding_amount': '0' if not crossings else None,
        'settlement_mark_price': 'UNAVAILABLE_IN_PUBLIC_FUNDING_HISTORY',
        'summary_sha256': digest(root/'summary.json')}
    result = {'status': 'PASS', 'date_utc': meta['date_utc'], 'method': 'No production imports. Independent integer FIFO inventory/fee/gross ledger, exact source trade matching, independent consumed-depth debt and original cohort snapshot reconstruction, new funding intervals.',
        'execution_enabled': False, 'portfolio_pnl_computed': False, 'accounting_checks': dict(checks),
        'source_checks': dict(source_checks), 'funding_status': funding_audit['status'],
        'input_time_ranges': {'tape_first_ns': tape_first, 'tape_last_ns': tape_last, 'live_trade_first_ns': trade_first, 'live_trade_last_ns': trade_last, 'max_source_receive_lag_ns': max_lag, 'decisions': tape_count, 'live_trades': trade_count},
        'candidate_states': dict(Counter(r['state'] for r in rows)), 'cycles': cycle_economics,
        'input_hashes': meta['inputs'], 'code_sha256': digest(__file__), 'summary_sha256': digest(root/'summary.json'),
        'events_sha256': digest(root/'events.jsonl'), 'cycles_sha256': digest(root/'cycles.jsonl'),
        'elapsed_seconds': time.monotonic()-begun,
        'limits': ['This validates hypothetical independent opportunities, not account-level returns.', 'Maker queue position is unobserved; strict-through/25% participation is an assumption.', 'Paired closes are simultaneous matched-size L1 assumptions.', 'Unclosed positions lack mark-to-market and may have unresolved funding; gross realized minus all paid fees is not final PnL.']}
    return result, funding_audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replay-dir', required=True)
    args = parser.parse_args()
    report, funding = audit(args.replay_dir)
    root = Path(args.replay_dir)
    for name, data in [('independent-audit.json', report), ('funding-interval-audit.json', funding)]:
        with (root/name).open('x') as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write('\n')
    print(json.dumps({k:report[k] for k in ('status','date_utc','accounting_checks','source_checks','funding_status','candidate_states','elapsed_seconds')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
