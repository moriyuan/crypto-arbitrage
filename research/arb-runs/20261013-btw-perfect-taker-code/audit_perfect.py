"""Independent accounting, funding and optional decision-tape audit of ideal taker CSVs.

No production imports, exchange API, credentials, or counterfactual replay output writes.
"""
import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import csv
import mmap
import struct

VENUES = ('bybit', 'bitget')
FEE = D('0.0003')
HEADER = struct.Struct('<qBBBBI8q')
ACTIVITY = struct.Struct('<qIIqqqqQ')


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def csv_rows(path):
    with Path(path).open() as f:
        return list(csv.DictReader(f))


def require(ok, message):
    if not ok:
        raise AssertionError(message)


class Checks:
    def __init__(self):
        self.n = 0

    def __call__(self, ok, message):
        self.n += 1
        require(ok, message)


def funding_sources(spec):
    events = read(spec['files']['funding_events']['path'])
    schedule = csv_rows(spec['files']['funding_csv']['path'])
    settled, announced = {}, {}
    for venue in VENUES:
        bundle = events['venues'][venue]
        require(bundle['status'] == 'BRACKETED_HISTORY', 'actual funding history not bracketed')
        settled[venue] = [int(e['timestamp_ms']) * 10**6 for e in bundle['selected_events']]
        rows = [(int(r['known_at_ns']), int(r['next_funding_ns'])) for r in schedule if r['venue'] == venue]
        require(all(a[0] < b[0] for a, b in zip(rows, rows[1:])), 'funding known-time ordering')
        announced[venue] = ([r[0] for r in rows], [r[1] for r in rows])
    return settled, announced


def funding_guard(ns, hold_ms, announced):
    for venue in VENUES:
        known, nexts = announced[venue]
        i = bisect_right(known, ns) - 1
        if i < 0 or nexts[i] - ns <= (hold_ms + 60000) * 10**6:
            return True
    return False


def crossings(start, end, timestamps):
    return [t for t in timestamps if start <= t <= end]


def audit_scenario(s, directory, spec, settled, announced, policy):
    check = Checks()
    orders = csv_rows(directory / 'orders.csv')
    cycles = csv_rows(directory / 'cycles.csv')
    equity = csv_rows(directory / 'equity.csv')
    for field in ('entry_bps', 'tp_bps', 'sl_bps', 'hard_bps', 'hold_ms'):
        check(int(s[field]) == int(policy[field]), 'policy ' + field)
    cash = {v: D(1000) for v in VENUES}
    grouped = defaultdict(list)
    snapshots = {}
    paid = D(0)
    realized = D(0)
    previous_ns = -1
    excess_orders = below_min = excess_total = excess_max = 0
    for order in orders:
        ns, cid, qty = int(order['ns']), int(order['cycle']), int(order['qty'])
        venue, intent = order['venue'], order['intent']
        price, fee = D(order['price']), D(order['fee'])
        check(ns >= previous_ns and int(order['timestamp_ms']) == ns // 10**6, 'exact nanosecond order time')
        previous_ns = ns
        check(venue in VENUES and intent in ('entry', 'exit'), 'order identity')
        check(qty > 0 and qty % 10 == 0 and price > 0, 'positive common quantity')
        check(fee == price * qty * FEE, 'exact fill fee 3bp')
        paid += fee
        if intent == 'entry':
            check(not any(x['venue'] == venue for x in grouped[cid]), 'one opening leg per venue')
            check(price * qty <= 250 and price * qty >= 5, 'entry notional bounds')
            check(price * qty + fee <= cash[venue], 'cash including entry fee budget')
            check(int(order['excess_depth_qty']) == 0 and int(order['exit_below_min']) == 0, 'entry assumption flags')
            check(qty <= int(order['l1_display_qty']), 'entry visible full L1 depth budget')
            cash[venue] -= fee
        else:
            entry = next(x for x in grouped[cid] if x['venue'] == venue and x['intent'] == 'entry')
            check(order['side'] != entry['side'] and int(entry['qty']) == qty, 'same quantity reverse closing side')
            gross = (price - D(entry['price'])) * qty * (1 if entry['side'] == 'buy' else -1)
            cash[venue] += gross - fee
            realized += gross
            excess = max(0, qty - int(order['l1_display_qty']))
            below = int(price * qty < 5)
            check(int(order['excess_depth_qty']) == excess and int(order['exit_below_min']) == below, 'explicit ideal exit assumption flags')
            excess_orders += excess > 0
            below_min += below
            excess_total += excess
            excess_max = max(excess_max, excess)
        grouped[cid].append(order)
        check(D(order['cash']) == cash[venue], 'per-fill independent cash ledger')
        if len(grouped[cid]) in (2, 4):
            snapshots[(cid, intent)] = dict(cash)
    check(list(grouped) == list(range(1, len(grouped) + 1)), 'consecutive opportunity ids')
    previous_exit = None
    closed_net = D(0)
    wins = losses = flat = 0
    reasons = Counter()
    actual_crossings = []
    initial_breaches = 0
    for cycle in cycles:
        cid = int(cycle['cycle'])
        records = grouped[cid]
        check(len(records) == 4 and [x['intent'] for x in records] == ['entry', 'entry', 'exit', 'exit'], 'four full synchronous orders')
        entry_ns, exit_ns, qty = int(cycle['entry_ns']), int(cycle['exit_ns']), int(cycle['qty'])
        check([x['venue'] for x in records] == list(VENUES) * 2, 'both venues opening and closing')
        check(all(int(x['ns']) == entry_ns for x in records[:2]) and all(int(x['ns']) == exit_ns for x in records[2:]), 'zero inter-leg delay')
        check(all(int(x['qty']) == qty for x in records), 'all four quantities identical')
        check(spec['entry_start_ns'] <= entry_ns < spec['end_ns_exclusive'] - 120 * 10**9, 'causal noon and tail entry window')
        check(entry_ns <= exit_ns < spec['end_ns_exclusive'], 'valid holding interval')
        check(not funding_guard(entry_ns, int(s['hold_ms']), announced), 'dynamic causal entry funding guard')
        if previous_exit is not None:
            check(entry_ns >= previous_exit + 30 * 10**9, 'single portfolio and cooldown')
        previous_exit = exit_ns
        check(int(cycle['holding_ns']) == exit_ns - entry_ns and int(cycle['holding_ms']) == (exit_ns - entry_ns) // 10**6, 'exact holding duration')
        check(int(cycle['entry_timestamp_ms']) == entry_ns // 10**6 and int(cycle['exit_timestamp_ms']) == exit_ns // 10**6, 'cycle timestamp serialization')
        lng, short = cycle['long_venue'], cycle['short_venue']
        check({lng, short} == set(VENUES), 'opposite cycle venue roles')
        entries = {o['venue']: o for o in records[:2]}
        exits = {o['venue']: o for o in records[2:]}
        check(entries[lng]['side'] == 'buy' and entries[short]['side'] == 'sell', 'long/short sides')
        check(D(cycle['long_entry_price']) == D(entries[lng]['price']) and D(cycle['short_entry_price']) == D(entries[short]['price']), 'cycle entry prices')
        check(D(cycle['long_exit_price']) == D(exits[lng]['price']) and D(cycle['short_exit_price']) == D(exits[short]['price']), 'cycle exit prices')
        leg_net = {}
        gross = D(0)
        fees = sum(D(o['fee']) for o in records)
        for venue in VENUES:
            leg_gross = (D(exits[venue]['price']) - D(entries[venue]['price'])) * qty * (1 if venue == lng else -1)
            gross += leg_gross
            leg_net[venue] = leg_gross - D(entries[venue]['fee']) - D(exits[venue]['fee'])
            for timestamp in crossings(entry_ns, exit_ns, settled[venue]):
                actual_crossings.append({'cycle': cid, 'venue': venue, 'settlement_ns': timestamp, 'entry_ns': entry_ns, 'exit_ns': exit_ns})
        net = gross - fees
        check(D(cycle['gross_pnl']) == gross and D(cycle['total_fees']) == fees and D(cycle['net_before_funding']) == net, 'cycle accounting')
        check(all(D(cycle[v + '_net_before_funding']) == leg_net[v] for v in VENUES), 'per venue cycle net')
        check(int(cycle['exit_parts']) == 1 and all(o['exit_reason'] == cycle['exit_reason'] for o in records[2:]), 'single full exit reason')
        check(int(cycle['exit_depth_assumed']) == int(any(int(o['excess_depth_qty']) > 0 for o in records[2:])), 'cycle exit depth flag')
        check(int(cycle['exit_min_notional_assumed']) == int(any(int(o['exit_below_min']) for o in records[2:])), 'cycle exit minimum flag')
        e_long, e_short = D(cycle['long_entry_price']), D(cycle['short_entry_price'])
        reverse_long = D(cycle['entry_' + lng + '_bid'])
        reverse_short = D(cycle['entry_' + short + '_ask'])
        initial = qty * (reverse_long - e_long + e_short - reverse_short - FEE * (e_long + e_short + reverse_long + reverse_short))
        check(D(cycle['initial_quote_net_proxy']) == initial, 'entry reverse-L1 baseline includes four fees')
        breach = initial <= -qty * e_long * D(s['hard_bps']) / 10000
        initial_breaches += breach
        check(int(cycle['initial_hard_budget_breach']) == int(breach), 'entry hard risk flag')
        check(D(cycle['trigger_remaining_net_proxy']) == net, 'exit liquidation net')
        check(entry_ns <= int(cycle['first_trigger_ns']) <= exit_ns, 'causal trigger')
        reason = cycle['exit_reason']
        if reason in ('hard_stop_loss', 'stop_loss', 'take_profit'):
            check(int(cycle['first_trigger_ns']) == exit_ns, 'zero risk-to-fill latency at valid book')
            expected = ('hard_stop_loss' if net <= -qty * e_long * D(s['hard_bps']) / 10000 else
                        'stop_loss' if net <= initial - qty * e_long * D(s['sl_bps']) / 10000 else
                        'take_profit' if net >= qty * e_long * D(s['tp_bps']) / 10000 else '')
            check(reason == expected, 'net-fee risk threshold and priority')
        elif reason == 'holding_timeout':
            check(int(cycle['first_trigger_ns']) - entry_ns >= int(s['hold_ms']) * 10**6, 'holding timeout boundary')
        elif reason == 'funding_guard':
            check(funding_guard(int(cycle['first_trigger_ns']), int(s['hold_ms']), announced), 'causal funding exit request')
        elif reason == 'replay_day_end':
            check(int(cycle['first_trigger_ns']) >= spec['end_ns_exclusive'] - 120 * 10**9, 'tail exit request')
        else:
            check(False, 'unknown exit reason')
        closed_net += net
        wins += net > 0
        losses += net < 0
        flat += net == 0
        reasons[reason] += 1
        post = snapshots[(cid, 'exit')]
        if sum(post.values()) - 2000 <= -10:
            check(cid == len(grouped), 'no entries after daily cash loss cap')
    open_groups = [(cid, rs) for cid, rs in grouped.items() if len(rs) != 4]
    check(len(open_groups) <= 1, 'one simultaneous portfolio')
    unresolved_qty = 0
    if open_groups:
        cid, rs = open_groups[0]
        check(cid == len(grouped) and len(rs) == 2, 'unresolved pair has two opening orders')
        check(rs[0]['qty'] == rs[1]['qty'] and rs[0]['ns'] == rs[1]['ns'], 'unresolved pair synchronous equal quantity')
        unresolved_qty = int(rs[0]['qty'])
        for venue in VENUES:
            for timestamp in crossings(int(rs[0]['ns']), spec['end_ns_exclusive'] - 1, settled[venue]):
                actual_crossings.append({'cycle': cid, 'venue': venue, 'settlement_ns': timestamp, 'residual': True})
    check(s['unresolved_qty'] == unresolved_qty, 'aggregate residual quantity')
    check(s['orders'] == len(orders) and s['cycles'] == len(cycles), 'aggregate row counts')
    check(s['wins'] == wins and s['losses'] == losses and s['flat_cycles'] == flat, 'aggregate win/loss counts')
    check(s['exit_reasons'] == dict(reasons), 'aggregate exit reasons')
    for v in VENUES:
        check(D(s['cash'][v]) == cash[v], 'final venue cash')
    check(D(s['gross_pnl']) == realized and D(s['fees']) == paid, 'aggregate gross and all fees')
    check(D(s['net_before_funding']) == sum(cash.values()) - 2000 == realized - paid, 'cash net includes every paid entry fee')
    check(D(s['closed_net_before_funding']) == closed_net, 'closed subset net')
    check(s['exit_depth_assumed_orders'] == excess_orders and s['exit_below_min_orders'] == below_min, 'aggregate assumed exits')
    check(s['total_exit_excess_depth_qty'] == excess_total and s['max_exit_excess_depth_qty'] == excess_max, 'aggregate assumed extra quantity')
    check(s['initial_hard_budget_breaches'] == initial_breaches, 'aggregate hard breach count')
    check(len(equity) == len(grouped) + len(cycles) + 1, 'equity events count')
    cycle_lookup = {int(c['cycle']): c for c in cycles}
    for row in equity:
        event, cid = row['event'], int(row['cycle'])
        balances = cash if event == 'end' else snapshots[(cid, event)]
        check(all(D(row[v + '_cash']) == balances[v] for v in VENUES) and D(row['total_cash']) == sum(balances.values()), 'equity independent cash')
        if event == 'exit':
            check(D(row['liquidation_equity']) == sum(balances.values()) and int(row['unresolved_qty']) == 0, 'flat equity')
        elif event == 'entry' and cid in cycle_lookup:
            cycle = cycle_lookup[cid]
            entry_fees = sum(D(o['fee']) for o in grouped[cid][:2])
            expected = sum(balances.values()) + D(cycle['initial_quote_net_proxy']) + entry_fees
            check(D(row['liquidation_equity']) == expected, 'entry liquidation equity')
    funding_status = ('UNVERIFIED_OPEN_POSITION' if unresolved_qty else
                      'SETTLEMENT_CROSSED_MARK_PRICE_UNAVAILABLE' if actual_crossings else 'VERIFIED_NO_SETTLEMENT')
    return {'status': 'PASS', 'accounting_pass': True, 'accounting_checks': check.n,
            'funding_status': funding_status, 'funding_crossings': len(actual_crossings),
            'funding_crossing_intervals': actual_crossings, 'unresolved_qty': unresolved_qty,
            'cycles': len(cycles), 'orders': len(orders), 'net_before_funding': str(realized - paid),
            'cash': {v: str(cash[v]) for v in VENUES}, 'fees': str(paid),
            'source_sha256': {name: sha(directory / name) for name in ('orders.csv', 'cycles.csv', 'equity.csv')}}


def decimal_units(value, decimals):
    scaled = D(value) * 10**decimals
    require(scaled == scaled.to_integral_value(), 'non-exact decimal serialization')
    return int(scaled)


class DeepScenario:
    """A separately implemented policy oracle matched against the emitted fills.

    Integer prices are native tape units; money has four additional decimals,
    so a 3bp fee is exactly price * quantity * 3. No production code is imported.
    """
    def __init__(self, scenario, directory, spec, decimals):
        self.s = scenario
        self.id = scenario['name']
        self.price_decimals = decimals
        self.money_decimals = decimals + 4
        self.initial = 1000 * 10**self.money_decimals
        self.cash = [self.initial, self.initial]
        self.entry_start = spec['entry_start_ns']
        self.end = spec['end_ns_exclusive']
        self.entry_bps = int(scenario['entry_bps'])
        self.tp, self.sl, self.hard = [int(scenario[k]) for k in ('tp_bps', 'sl_bps', 'hard_bps')]
        self.hold = int(scenario['hold_ms']) * 10**6
        self.entry_rows, self.exit_rows = {}, {}
        self.closed = {}
        self.cycles = csv_rows(directory / 'cycles.csv')
        self.order_rows = csv_rows(directory / 'orders.csv')
        for order in self.order_rows:
            table = self.entry_rows if order['intent'] == 'entry' else self.exit_rows
            table.setdefault(int(order['ns']), []).append(order)
        for cycle in self.cycles:
            self.closed[int(cycle['cycle'])] = cycle
        self.position = None
        self.next_id = 0
        self.cooldown_until = 0
        self.loss_halted = False
        self.day_ended = False
        self.peak = 2 * self.initial
        self.drawdown = 0
        self.marks = self.incomplete_marks = 0
        self.entries_seen = self.exits_seen = self.order_checks = self.decisions = 0
        self.paid = self.gross = 0
        self.quotes = []

    def check(self, ok, text):
        if not ok:
            raise AssertionError(self.id + ': ' + text)

    def liquidation_net(self, prices):
        p = self.position
        long, q, opening = p['long'], p['qty'], p['opening']
        long_exit, short_exit = prices[2 * long + 1], prices[2 * (1 - long)]
        return q * (10000 * (long_exit - opening[long] + opening[1 - long] - short_exit)
                    - 3 * (long_exit + opening[long] + opening[1 - long] + short_exit))

    def risk(self, prices):
        p = self.position
        net = self.liquidation_net(prices)
        basis = p['opening'][p['long']] * p['qty']
        if net <= -basis * self.hard:
            return 'hard_stop_loss'
        if net <= p['baseline'] - basis * self.sl:
            return 'stop_loss'
        if net >= basis * self.tp:
            return 'take_profit'
        return ''

    def mark(self, prices, valid):
        if self.position is not None and not valid:
            self.incomplete_marks += 1
            return
        value = sum(self.cash)
        if self.position:
            value += self.liquidation_net(prices) + sum(self.position['entry_fees'])
        self.peak = max(self.peak, value)
        self.drawdown = max(self.drawdown, self.peak - value)
        self.marks += 1

    def best_offer(self, prices, displays):
        best = None
        for long in (0, 1):
            ask, bid = prices[2 * long], prices[2 * (1 - long) + 1]
            q = min(displays[2 * long], displays[2 * (1 - long) + 1])
            for venue in (0, 1):
                px = prices[2 * venue if venue == long else 2 * venue + 1]
                q = min(q, (250 * 10**self.price_decimals) // px, self.cash[venue] // (px * 10003))
            q -= q % 10
            if q <= 0 or q * min(ask, bid) < 5 * 10**self.price_decimals:
                continue
            if 20000 * (bid - ask) < self.entry_bps * (bid + ask):
                continue
            if best is None or (bid - ask) * (best[2] + best[3]) > (best[3] - best[2]) * (bid + ask):
                best = (long, q, ask, bid)
        return best

    def match_orders(self, ns, prices, displays, intent):
        p = self.position
        rows = (self.entry_rows if intent == 'entry' else self.exit_rows).get(ns, [])
        self.check(len(rows) == 2, f'missing or extra {intent} at earliest eligible decision {ns}')
        for venue, row in enumerate(rows):
            buy = (venue == p['long']) == (intent == 'entry')
            side = 2 * venue + (0 if buy else 1)
            px, qty = prices[side], p['qty']
            self.check(row['venue'] == VENUES[venue] and row['side'] == ('buy' if buy else 'sell'), 'L1 order side')
            self.check(int(row['cycle']) == p['id'] and int(row['qty']) == qty, 'exact full portfolio quantity')
            self.check(decimal_units(row['price'], self.price_decimals) == px, 'opposing visible L1 price')
            self.check(int(row['l1_display_qty']) == displays[side], 'original display quantity')
            self.check(decimal_units(row['fee'], self.money_decimals) == qty * px * 3, 'independent integer fee')
            if intent == 'entry':
                fee = qty * px * 3
                p['entry_fees'][venue] = fee
                self.cash[venue] -= fee
            else:
                fee = qty * px * 3
                gross = qty * (px - p['opening'][venue]) * 10000 * (1 if venue == p['long'] else -1)
                self.cash[venue] += gross - fee
                self.gross += gross
                self.check(row['exit_reason'] == p['pending'], 'first valid exit priority')
                self.check(int(row['excess_depth_qty']) == max(0, qty - displays[side]), 'exit quantity beyond visible depth explicitly recorded')
                self.check(int(row['exit_below_min']) == int(qty * px < 5 * 10**self.price_decimals), 'exit minimum waiver explicitly recorded')
            self.paid += fee
            self.check(decimal_units(row['cash'], self.money_decimals) == self.cash[venue], 'source-price fill cash')
            self.order_checks += 1

    def open(self, ns, prices, displays, offer):
        long, quantity, ask, bid = offer
        self.next_id += 1
        opening = [0, 0]
        opening[long], opening[1 - long] = ask, bid
        self.position = {'id': self.next_id, 'ns': ns, 'long': long, 'qty': quantity,
                         'opening': opening, 'entry_fees': [0, 0], 'pending': '', 'trigger': None,
                         'entry_prices': list(prices)}
        self.position['baseline'] = self.liquidation_net(prices)
        self.match_orders(ns, prices, displays, 'entry')
        self.entries_seen += 1
        self.mark(prices, True)

    def close(self, ns, prices, displays):
        p = self.position
        cycle = self.closed.get(p['id'])
        self.check(cycle is not None, 'expected closed cycle missing')
        self.check(int(cycle['exit_ns']) == ns, 'first permitted exit decision and zero execution delay')
        self.check(int(cycle['first_trigger_ns']) == p['trigger'], 'exact first trigger including invalid-book latching')
        self.check(cycle['exit_reason'] == p['pending'], 'exit reason priority')
        self.check(decimal_units(cycle['initial_quote_net_proxy'], self.money_decimals) == p['baseline'], 'native entry liquidation baseline')
        self.match_orders(ns, prices, displays, 'exit')
        self.quotes.append({'id': self.id, 'cycle': p['id'], 'entry_ns': p['ns'], 'exit_ns': ns,
                            'qty': p['qty'], 'long_venue': VENUES[p['long']],
                            'price_decimals': self.price_decimals, 'entry_prices': p['entry_prices'],
                            'exit_prices': list(prices)})
        self.position = None
        self.exits_seen += 1
        self.cooldown_until = ns + 30 * 10**9
        if sum(self.cash) - 2 * self.initial <= -10 * 10**self.money_decimals:
            self.loss_halted = True
        self.mark(prices, True)

    def tick(self, ns, valid, force, history, prices, displays, active, funding_next):
        self.decisions += 1
        funding = any(f - ns <= self.hold + 60 * 10**9 for f in funding_next)
        if force == 2:
            self.day_ended = True
        had_position = self.position is not None
        exited = entered = False
        if had_position:
            p = self.position
            reason = None
            if force == 2 and p['pending'] != 'replay_day_end':
                reason = 'replay_day_end'
            elif funding and p['pending'] not in ('replay_day_end', 'funding_guard'):
                reason = 'funding_guard'
            elif not p['pending'] and ns - p['ns'] >= self.hold:
                reason = 'holding_timeout'
            if reason:
                p['pending'], p['trigger'] = reason, ns
            if valid:
                if not p['pending']:
                    reason = self.risk(prices)
                    if reason:
                        p['pending'], p['trigger'] = reason, ns
                if p['pending']:
                    self.close(ns, prices, displays)
                    exited = True
        if (not had_position and not self.day_ended and not funding and not self.loss_halted
                and ns >= self.cooldown_until and ns >= self.entry_start and active and valid and history):
            offer = self.best_offer(prices, displays)
            if offer:
                self.open(ns, prices, displays, offer)
                entered = True
                reason = self.risk(prices)
                if reason:
                    self.position['pending'], self.position['trigger'] = reason, ns
                    self.close(ns, prices, displays)
                    exited = True
        self.check((ns in self.entry_rows) == entered, f'unexpected entry at {ns}')
        self.check((ns in self.exit_rows) == exited, f'unexpected exit at {ns}')
        self.mark(prices, valid)

    def finish(self):
        self.check(self.entries_seen == len(self.entry_rows) and self.exits_seen == len(self.exit_rows), 'all recorded decisions covered')
        self.check(self.order_checks == len(self.order_rows), 'all source L1 orders covered')
        self.check(self.decisions == self.s['ticks'], 'all-day source decision count')
        self.check(self.drawdown == decimal_units(self.s['complete_mark_max_drawdown_usdt'], self.money_decimals), 'every valid decision liquidation drawdown')
        self.check(self.marks == self.s['complete_liquidation_equity_marks'] and self.incomplete_marks == self.s['incomplete_liquidation_equity_marks'], 'liquidation mark completeness')
        self.check(self.paid == decimal_units(self.s['fees'], self.money_decimals), 'all source fees')
        self.check(self.gross == decimal_units(self.s['gross_pnl'], self.money_decimals), 'all source gross')
        for v in (0, 1):
            self.check(self.cash[v] == decimal_units(self.s['cash'][VENUES[v]], self.money_decimals), 'final independent venue cash')
        self.check((self.position['qty'] if self.position else 0) == self.s['unresolved_qty'], 'final remaining quantity')
        return {'status': 'PASS', 'source_decisions': self.decisions, 'source_L1_orders_matched': self.order_checks,
                'earliest_entries_verified': self.entries_seen, 'earliest_exits_verified': self.exits_seen,
                'complete_liquidation_equity_marks': self.marks, 'incomplete_liquidation_equity_marks': self.incomplete_marks,
                'complete_mark_max_drawdown_usdt': str(D(self.drawdown) / 10**self.money_decimals),
                'final_cash': {VENUES[v]: str(D(self.cash[v]) / 10**self.money_decimals) for v in (0, 1)},
                'funding_guard': 'causal latest announcement, unknown/expired/remaining <= policy hold + 60 seconds',
                'entry_sizing': 'full current L1 min of venues, 250USDT/cash including fee, common10/min5',
                'entry_timing': 'noon-known q25, 20 completed day history, cooldown and daily loss cap; all eligible source decisions re-evaluated',
                'exit_timing': 'first valid decision after highest-priority force/causal funding/hold/risk trigger, zero added latency',
                'risk_priority': ['hard_stop_loss', 'entry_baseline_deterioration_stop', 'net_take_profit'],
                'input_price_decimals': self.price_decimals, 'money_decimals': self.money_decimals}


def audit_deep(root, spec, aggregate, replay_dir, announced, light_result, ids):
    require(light_result['status'] == 'PASS', 'deep audit requires independent light checks pass')
    files = spec['files']
    for name in ('tape', 'activity', 'thresholds'):
        require(sha(files[name]['path']) == files[name]['sha256'], 'deep source hash ' + name)
    selected = [s for s in aggregate['scenarios'] if s['name'] in ids]
    require({s['name'] for s in selected} == set(ids), 'requested deep ids not present')
    q25 = next(row for row in csv_rows(files['thresholds']['path']) if row['name'] == 'q25')
    threshold = [int(q25[k]) for k in ('countA', 'countB', 'turnoverAunits', 'turnoverBunits')]
    age = int(q25['maxAgeNs'])
    states = [DeepScenario(s, replay_dir / s['name'], spec, aggregate['price_decimals']) for s in selected]
    observations = sorted((known, venue, next_value) for venue, (times, nexts) in announced.items()
                          for known, next_value in zip(times, nexts))
    cursor, next_funding = 0, [0, 0]
    count, previous_ns, first_ns, last_ns = 0, -1, None, None
    tape_path, activity_path = Path(files['tape']['path']), Path(files['activity']['path'])
    with tape_path.open('rb') as tf, activity_path.open('rb') as af:
        with mmap.mmap(tf.fileno(), 0, access=mmap.ACCESS_READ) as tape, mmap.mmap(af.fileno(), 0, access=mmap.ACCESS_READ) as act:
            require(tape[:8].decode() == aggregate['tape_format'] and act[:8] == b'BTWACT01', 'native tape and activity magic')
            offset, aoffset = 8, 8
            while offset < len(tape):
                h = HEADER.unpack_from(tape, offset)
                ns, valid, force, history, pad, updates = h[:6]
                require(pad == 0 and ns > previous_ns and valid in (0, 1) and force in (0, 1, 2) and history in (0, 1), 'native exact decision schema')
                previous_ns = ns
                a = ACTIVITY.unpack_from(act, aoffset)
                require(a[0] == ns, 'exact ns activity alignment')
                active = (a[7] == 3 and all(a[i + 1] >= threshold[i] for i in range(4))
                          and 0 <= a[5] <= age and 0 <= a[6] <= age)
                while cursor < len(observations) and observations[cursor][0] <= ns:
                    _, venue, nxt = observations[cursor]
                    next_funding[VENUES.index(venue)] = nxt
                    cursor += 1
                prices, displays = h[6:10], h[10:14]
                if valid:
                    require(all(p > 0 for p in prices) and all(q >= 0 for q in displays), 'valid source prices and depth')
                    require(prices[1] <= prices[0] and prices[3] <= prices[2], 'uncrossed per-venue source books')
                if first_ns is None:
                    first_ns = ns
                last_ns = ns
                for state in states:
                    state.tick(ns, valid, force, history, prices, displays, active, next_funding)
                count += 1
                offset += HEADER.size + updates * 24
                aoffset += ACTIVITY.size
            require(offset == len(tape) and aoffset == len(act), 'complete exact tape/activity record lengths')
    require(count == aggregate['decisions'] and first_ns == aggregate['first_ns'] and last_ns == aggregate['last_ns'], 'full native day covered')
    details = {state.id: state.finish() for state in states}
    quotes_path = replay_dir / 'perfect-deep-quotes.json'
    quotes_path.write_text(json.dumps({'status': 'PASS', 'day': light_result['day'],
        'aggregate_sha256': sha(replay_dir / 'aggregate.json'), 'tape_sha256': files['tape']['sha256'],
        'audited_ids': list(ids), 'side_order': ['bybit_ask', 'bybit_bid', 'bitget_ask', 'bitget_bid'],
        'price_units': 'integer units of 10**(-price_decimals) USDT',
        'cycles': [q for state in states for q in state.quotes]}, indent=2) + '\n')
    return {'status': 'PASS', 'execution_enabled': False, 'day': light_result['day'], 'audited_ids': list(ids),
            'aggregate_sha256': sha(replay_dir / 'aggregate.json'),
            'launch_sha256': light_result['launch_sha256'], 'input_manifest_sha256': light_result['input_manifest_sha256'],
            'light_audit_sha256': sha(replay_dir / 'perfect-audit.json'), 'auditor_sha256': sha(__file__),
            'source_entry_exit_quotes_path': str(quotes_path), 'source_entry_exit_quotes_sha256': sha(quotes_path),
            'source_sha256': {k: files[k]['sha256'] for k in ('tape', 'activity', 'thresholds', 'funding_csv')},
            'scenarios': details, 'native_decisions_scanned': count,
            'scope': 'Independent policy oracle against every native decision, not only existing entry cohort. All entries and earliest exits, source L1 prices/depth, exact activity, causal dynamic funding, sizing, fees/cash and every valid mark verified.',
            'limitations': ['Native tape/header correctness is bound to retained prior raw-data audits and input-acceptance; this pass does not reparse compressed exchange frames.',
                           'Replay intentionally assumes simultaneous zero-latency full L1 exits, even beyond displayed depth or below exchange minimum. This is not proof of live executability.']}


def run(root, day, replay_dir, deep=False, ids=None):
    root, replay_dir = Path(root).resolve(), Path(replay_dir).resolve()
    manifest = read(root / 'input-manifest.json')
    spec = manifest['days'][day]
    aggregate_path = replay_dir / 'aggregate.json'
    aggregate = read(aggregate_path)
    require(aggregate['execution_enabled'] is False and aggregate['fee_rate'] == '0.0003', 'offline fee policy')
    require(aggregate['entry_start_ns'] == spec['entry_start_ns'], 'entry start contract')
    require(aggregate['price_decimals'] == (5 if aggregate['tape_format'] == 'BTWTP002' else 6), '002/003 price units')
    require(aggregate['money_decimals'] == aggregate['price_decimals'] + 4, 'exact money units')
    for label in ('funding_csv', 'funding_events', 'funding_acceptance'):
        require(sha(spec['files'][label]['path']) == spec['files'][label]['sha256'], 'funding input hash')
    require(read(spec['files']['funding_acceptance']['path'])['status'] == 'PUBLIC_FUNDING_HISTORY_AND_CAUSAL_SCHEDULE_VERIFIED', 'public funding verification')
    settled, announced = funding_sources(spec)
    policies = {r['id']: r for r in csv_rows(root / 'policies.csv')}
    require(len({s['name'] for s in aggregate['scenarios']}) == len(aggregate['scenarios']), 'unique scenarios')
    results = {}
    selected_ids = ids or [s['name'] for s in aggregate['scenarios']]
    for scenario in aggregate['scenarios']:
        if deep and scenario['name'] not in selected_ids:
            continue
        identifier = scenario['name']
        try:
            results[identifier] = audit_scenario(scenario, replay_dir / identifier, spec, settled, announced, policies[identifier])
        except Exception as error:
            results[identifier] = {'status': 'FAIL', 'accounting_pass': False, 'funding_status': 'UNVERIFIED_AUDIT_FAILURE', 'error': str(error)}
    result = {'status': 'PASS' if all(s['status'] == 'PASS' for s in results.values()) else 'FAIL',
              'execution_enabled': False, 'day': day, 'scenarios': results,
              'aggregate_sha256': sha(aggregate_path), 'auditor_sha256': sha(__file__),
              'launch_sha256': sha(replay_dir.with_suffix('.launch.json')),
              'input_manifest_sha256': sha(root / 'input-manifest.json'),
              'funding_events_sha256': spec['files']['funding_events']['sha256'],
              'funding_csv_sha256': spec['files']['funding_csv']['sha256'],
              'scope': 'Independent Decimal CSV ledger, fee, cash, synchronous same-quantity legs, risk arithmetic and actual settlement intervals; source-L1 and first-trigger completeness are a separate deep audit.'}
    if deep:
        result = audit_deep(root, spec, aggregate, replay_dir, announced, result, selected_ids)
        name = 'perfect-deep-audit.json'
    else:
        name = 'perfect-audit.json'
    (replay_dir / name).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'status': result['status'], 'day': day, 'scenarios': len(results), 'path': str(replay_dir / name), 'failures': {k: v.get('error') for k, v in results.items() if v['status'] != 'PASS'}}))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--day', required=True)
    parser.add_argument('--replay-dir', required=True)
    parser.add_argument('--deep', action='store_true')
    parser.add_argument('--ids', help='Comma-separated scenario ids for the deep audit only')
    args = parser.parse_args()
    if args.ids and not args.deep:
        parser.error('--ids is only valid with --deep; light audit always covers all scenarios')
    run(args.root, args.day, args.replay_dir, args.deep, args.ids.split(',') if args.ids else None)
