"""Independent synthetic contracts for the zero-latency perfect L1 taker.

No historical run, network, credentials, exchange API or production risk helper.
Only the binary reader format is shared; expected economics use Decimal below.
"""
import csv
from decimal import Decimal as D
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
BASE = 1790856000000000000  # 2026-10-01 12:00:00 UTC; a synthetic receive clock.
MS = 1_000_000
SEC = 1_000_000_000
ACT = struct.Struct('<qIIqqqqQ')
THRESHOLDS = ('name,countA,countB,turnoverAunits,turnoverBunits,maxAgeNs\n'
              'q10,1,1,1,1,1000000000\n'
              'q25,3,4,300,400,1000000000\n')
POLICY_HEADER = 'id,entry_bps,tp_bps,sl_bps,hard_bps,hold_ms\n'
DEFAULT_POLICY = ('case', 21, 5, 20, 100, 60_000)
FUNDING_HEADER = 'venue,known_at_ns,next_funding_ns\n'


def funding_rows(rows=None):
    if rows is None:
        rows = [(venue, BASE - SEC, BASE + 100000 * SEC) for venue in ('bybit', 'bitget')]
    return FUNDING_HEADER + ''.join(','.join(map(str, row)) + '\n' for row in rows)


def quote(offset_ns=0, prices=(100000, 99999, 100301, 100300), qty=80,
          valid=True, force=0, history=True):
    sizes = [qty] * 4 if isinstance(qty, int) else list(qty)
    return dict(ns=BASE + offset_ns, prices=tuple(prices), sizes=sizes,
                valid=valid, force=force, history=history)


def write_tape(path, ticks, decimals=5):
    """Direct independent encoding, side order Aask, Abid, Bask, Bbid."""
    previous = [{}, {}, {}, {}]
    with path.open('wb') as out:
        out.write({5: b'BTWTP002', 6: b'BTWTP003'}[decimals])
        for tick in ticks:
            updates = []
            if tick['valid']:
                for side, (price, size) in enumerate(zip(tick['prices'], tick['sizes'])):
                    current, old = {price: size}, previous[side]
                    for p in sorted(current.keys() | old.keys()):
                        if current.get(p, 0) != old.get(p, 0):
                            updates.append((side, p * 10 ** (8 - decimals), current.get(p, 0)))
                    previous[side] = current
            out.write(struct.pack('<qBBBBI8q', tick['ns'], tick['valid'], tick['force'],
                                  tick['history'], 0, len(updates), *tick['prices'], *tick['sizes']))
            for row in updates:
                out.write(struct.pack('<B7xqq', *row))


def active(tick, **updates):
    values = dict(ns=tick['ns'], countA=3, countB=4, turnoverA=300,
                  turnoverB=400, ageA=0, ageB=0, mask=3)
    values.update(updates)
    return tuple(values[key] for key in ('ns', 'countA', 'countB', 'turnoverA',
                                        'turnoverB', 'ageA', 'ageB', 'mask'))


def flag(value):
    assert value in ('0', '1', 'false', 'true'), value
    return value in ('1', 'true')


class PerfectTakerContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix='perfect-taker-build-')
        cls.binary = Path(cls.build.name) / 'perfect_taker'
        compiler = shutil.which('clang++') or shutil.which('g++')
        if not compiler:
            raise RuntimeError('C++17 compiler unavailable')
        cls.compile_command = [compiler, '-std=c++17', '-O2', '-Wall', '-Wextra',
                               str(HERE / 'perfect_taker.cpp'), '-o', str(cls.binary)]
        subprocess.run(cls.compile_command, check=True, capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def run_case(self, ticks, policy=DEFAULT_POLICY, policies=None, rows=None,
                 decimals=5, start_ns=BASE, failure=None, thresholds=THRESHOLDS,
                 activity_extra=b'', funding=None):
        temp = tempfile.TemporaryDirectory(prefix='perfect-taker-case-')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        write_tape(root / 'tape.bin', ticks, decimals)
        if rows is None:
            rows = [active(t) for t in ticks]
        (root / 'activity.bin').write_bytes(
            b'BTWACT01' + b''.join(ACT.pack(*r) for r in rows) + activity_extra)
        (root / 'thresholds.csv').write_text(thresholds)
        if policies is None:
            policies = POLICY_HEADER + ','.join(map(str, policy)) + '\n'
        (root / 'policies.csv').write_text(policies)
        (root / 'funding.csv').write_text(funding_rows() if funding is None else funding)
        args = [str(self.binary), '--tape', str(root / 'tape.bin'),
                '--activity', str(root / 'activity.bin'), '--thresholds', str(root / 'thresholds.csv'),
                '--output', str(root / 'out'), '--entry-start-ns', str(start_ns),
                '--policies', str(root / 'policies.csv'), '--funding-csv', str(root / 'funding.csv')]
        proc = subprocess.run(args, capture_output=True, text=True)
        if failure is not None:
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            self.assertIn(failure, proc.stderr)
            self.assertFalse((root / 'out' / 'aggregate.json').exists())
            return root, args
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads((root / 'out' / 'aggregate.json').read_text())
        scenarios = {}
        for scenario in summary['scenarios']:
            for name in ('orders', 'cycles'):
                with (root / 'out' / scenario['name'] / (name + '.csv')).open() as source:
                    scenario[name + '_rows'] = list(csv.DictReader(source))
            scenarios[scenario['name']] = scenario
        return root, args, summary, scenarios

    def one(self, ticks, **kwargs):
        return self.run_case(ticks, **kwargs)[3]['case']

    def assert_accounting(self, scenario):
        """Rebuild venue balances solely from raw order side, price and quantity."""
        cash = {'bybit': D(1000), 'bitget': D(1000)}
        entries, by_cycle = {}, {}
        for order in scenario['orders_rows']:
            venue, cycle = order['venue'], order['cycle']
            qty, price, paid = map(D, (order['qty'], order['price'], order['fee']))
            self.assertEqual(paid, qty * price * D('.0003'))
            cash[venue] -= paid
            if order['intent'] == 'entry':
                entries[cycle, venue] = (qty, price, order['side'])
            else:
                eqty, eprice, side = entries[cycle, venue]
                self.assertEqual(qty, eqty)
                self.assertNotEqual(order['side'], side)
                cash[venue] += qty * (price - eprice) * (1 if side == 'buy' else -1)
            self.assertEqual(D(order['cash']), cash[venue])
            by_cycle.setdefault(cycle, []).append(order)
        self.assertEqual({k: D(v) for k, v in scenario['cash'].items()}, cash)
        for cycle in scenario['cycles_rows']:
            orders = by_cycle[cycle['cycle']]
            self.assertEqual(len(orders), 4)
            self.assertEqual(len({o['qty'] for o in orders}), 1)
            self.assertEqual(cycle['exit_parts'], '1')
            fees = sum(D(o['fee']) for o in orders)
            gross = D(0)
            venue_net = {}
            for venue in cash:
                entry, close = [o for o in orders if o['venue'] == venue]
                leg_gross = D(entry['qty']) * (D(close['price']) - D(entry['price'])) * (
                    1 if entry['side'] == 'buy' else -1)
                gross += leg_gross
                venue_net[venue] = leg_gross - D(entry['fee']) - D(close['fee'])
                self.assertEqual(venue_net[venue], D(cycle[venue + '_net_before_funding']))
            self.assertEqual((D(cycle['gross_pnl']), D(cycle['total_fees']),
                              D(cycle['net_before_funding'])), (gross, fees, gross - fees))
        if not scenario['unresolved_qty']:
            total_net = sum(D(c['net_before_funding']) for c in scenario['cycles_rows'])
            self.assertEqual(total_net, sum(cash.values()) - 2000)
            self.assertEqual(total_net, D(scenario['net_before_funding']))

    def test_full_display_zero_latency_four_orders_and_both_directions(self):
        entry = (100000, 99999, 100301, 100300)
        close = (100201, 100200, 100001, 100000)
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                p0 = entry[2:] + entry[:2] if reverse else entry
                p1 = close[2:] + close[:2] if reverse else close
                sizes = [200, 67, 83, 200] if reverse else [83, 200, 200, 67]
                s = self.one([quote(0, p0, sizes), quote(1, p1, sizes)])
                self.assertEqual((s['orders'], s['cycles']), (4, 1))
                c = s['cycles_rows'][0]
                self.assertEqual(c['qty'], '60')  # 100% of min(83,67), rounded to 10.
                self.assertEqual(c['long_venue'], 'bitget' if reverse else 'bybit')
                self.assertEqual(c['exit_reason'], 'take_profit')
                self.assertEqual([int(o['ns']) for o in s['orders_rows']], [BASE, BASE, BASE + 1, BASE + 1])
                self.assertEqual(c['holding_ns'], '1')
                self.assert_accounting(s)

    def test_003_exact_microprice_fee_and_cash(self):
        ticks = [quote(0, (1000000, 999990, 1003001, 1003000)),
                 quote(1, (1002000, 1001990, 1000011, 1000010))]
        _, _, meta, ss = self.run_case(ticks, decimals=6)
        s = ss['case']
        self.assertEqual((meta['tape_format'], meta['price_decimals'], meta['money_decimals']),
                         ('BTWTP003', 6, 10))
        self.assertEqual(s['orders_rows'][-1]['price'], '1.000011')
        self.assertEqual(s['cycles_rows'][0]['qty'], '80')
        self.assert_accounting(s)

    def test_003_invalid_native_tick_fails_closed(self):
        self.run_case([quote(0, (1000001, 999990, 1003001, 1003000))],
                      decimals=6, failure='native quote tick')

    def test_entry_notional_250_depth_common_step_and_minimum(self):
        s = self.one([quote(0, qty=10000)])
        self.assertEqual([o['qty'] for o in s['orders_rows']], ['240', '240'])
        for o in s['orders_rows']:
            self.assertLessEqual(D(o['price']) * D(o['qty']), 250)
        self.assertEqual(self.one([quote(0, qty=9)])['orders'], 0)
        cheap = (10000, 9999, 10031, 10030)
        self.assertEqual(self.one([quote(0, cheap, 40)])['orders'], 0)
        exact = self.one([quote(0, cheap, 50)])
        self.assertEqual([o['qty'] for o in exact['orders_rows']], ['50', '50'])
        self.assertEqual(D(exact['orders_rows'][0]['price']) * 50, 5)

    def test_each_account_cash_entry_fee_budget_is_independent(self):
        # A profitable paired move leaves the short venue with 40.12933648 USDT.
        # Forty new coins cost 40.12 notional but 40.132036 including entry fee.
        ticks = [quote(0, qty=10000),
                 quote(1, (510167, 510166, 500066, 500065), qty=10000),
                 quote(30 * SEC + 1, qty=10000)]
        s = self.one(ticks)
        self.assertEqual(s['cycles'], 1)
        first_close = next(o for o in s['orders_rows'] if o['venue'] == 'bitget' and o['intent'] == 'exit')
        self.assertEqual(D(first_close['cash']), D('40.129336480'))
        self.assertGreater(D(first_close['cash']), D('40.12'))
        self.assertLess(D(first_close['cash']), D('40.132036'))
        self.assertEqual([o['qty'] for o in s['orders_rows'][-2:]], ['30', '30'])
        self.assert_accounting(s)

    def test_insufficient_one_venue_cash_cannot_borrow_from_other(self):
        ticks = [quote(0, qty=10000),
                 quote(1, (525101, 525100, 515000, 514999), qty=10000),
                 quote(30 * SEC + 1, qty=10000)]
        s = self.one(ticks)
        self.assertEqual((s['orders'], s['cycles']), (4, 1))
        self.assertGreater(D(s['cash']['bybit']), 1000)
        self.assertGreater(D(s['cash']['bitget']), 0)
        self.assertLess(D(s['cash']['bitget']), 5)
        self.assert_accounting(s)

    def test_forced_exit_fully_closes_even_beyond_display_or_minimum(self):
        # Quantity 80 remains indivisible by execution assumptions even when
        # current L1 only shows one coin and closing notional falls below 5 USDT.
        ticks = [quote(0), quote(1, (1001, 1000, 1001, 1000), qty=1, force=2)]
        s = self.one(ticks)
        c = s['cycles_rows'][0]
        self.assertEqual((s['orders'], s['unresolved_qty'], c['exit_reason']), (4, 0, 'replay_day_end'))
        self.assertTrue(flag(c['exit_depth_assumed']))
        self.assertTrue(flag(c['exit_min_notional_assumed']))
        for o in s['orders_rows'][2:]:
            self.assertEqual((o['qty'], o['l1_display_qty'], o['excess_depth_qty']), ('80', '1', '79'))
            self.assertTrue(flag(o['exit_below_min']))
            self.assertEqual(int(o['ns']), BASE + 1)
        self.assert_accounting(s)

    def test_normal_exit_does_not_falsely_mark_assumed_depth_or_minimum(self):
        s = self.one([quote(0), quote(1, (100201, 100200, 100001, 100000))])
        c = s['cycles_rows'][0]
        self.assertFalse(flag(c['exit_depth_assumed']))
        self.assertFalse(flag(c['exit_min_notional_assumed']))
        self.assertTrue(all(o['excess_depth_qty'] == '0' for o in s['orders_rows']))

    def test_raw_spread_entry_boundary_uses_inclusive_midpoint_denominator(self):
        # 20000*(102000-100000)/(102000+100000)=198.019801... bp.
        # 199bp rejects; 198bp accepts. No fee/net-entry deduction is hidden.
        t = quote(0, (100000, 99999, 102001, 102000))
        ps = POLICY_HEADER + 'accept,198,5,20,100,60000\nreject,199,5,20,100,60000\n'
        _, _, _, ss = self.run_case([t], policies=ps)
        self.assertEqual((ss['accept']['orders'], ss['reject']['orders']), (2, 0))
        exact = quote(0, (99500, 99499, 100501, 100500))  # exactly 100bp
        self.assertEqual(self.one([exact], policy=('case', 100, 5, 20, 100, 60000))['orders'], 2)

    def test_history_activity_validity_and_entry_clock_gate_only_entry(self):
        ticks = [quote(0, history=False), quote(1, valid=False), quote(2), quote(3), quote(4),
                 quote(5, (100201, 100200, 100001, 100000), history=False)]
        rows = [active(t) for t in ticks]
        rows[3] = active(ticks[3], countB=3)
        rows[5] = active(ticks[5], mask=0, ageA=-1, ageB=-1)
        s = self.one(ticks, rows=rows, start_ns=BASE + 3)
        self.assertEqual([int(o['ns']) for o in s['orders_rows']], [BASE + 4, BASE + 4, BASE + 5, BASE + 5])
        self.assertEqual(s['cycles_rows'][0]['exit_reason'], 'take_profit')

    def test_each_q25_constraint_is_inclusive_and_both_venues_required(self):
        t = quote()
        self.assertEqual(self.one([t], rows=[active(t, ageA=SEC, ageB=SEC)])['orders'], 2)
        for bad in (dict(countA=2), dict(countB=3), dict(turnoverA=299), dict(turnoverB=399),
                    dict(ageA=SEC + 1), dict(ageB=-1), dict(mask=1), dict(mask=2)):
            with self.subTest(bad=bad):
                self.assertEqual(self.one([t], rows=[active(t, **bad)])['orders'], 0)

    def test_fixed_entry_reverse_baseline_does_not_stop_on_fees_alone(self):
        s = self.one([quote(0), quote(1), quote(10 * SEC)])
        self.assertEqual((s['orders'], s['cycles'], s['unresolved_qty']), (2, 0, 80))
        self.assert_accounting(s)

    def test_initial_reverse_baseline_is_quote_only_without_exit_depth_constraint(self):
        s = self.one([quote(0, qty=[80, 1, 1, 80]), quote(1, qty=[80, 1, 1, 80]),
                      quote(2, qty=[80, 1, 1, 80], force=2)])
        c = s['cycles_rows'][0]
        self.assertEqual(c['qty'], '80')
        self.assertEqual(c['exit_reason'], 'replay_day_end')
        self.assertEqual(D(c['initial_quote_net_proxy']), D(c['net_before_funding']))
        self.assertTrue(flag(c['exit_depth_assumed']))
        self.assert_accounting(s)

    def test_relative_stop_exact_boundary_both_directions(self):
        entry = (100000, 99999, 102001, 102000)
        for reverse in (False, True):
            for xl, xs, closes in ((99900, 102100, False), (99899, 102101, True), (99898, 102102, True)):
                with self.subTest(reverse=reverse, xl=xl):
                    finish = (xl + 1, xl, xs, xs - 1)
                    p0 = entry[2:] + entry[:2] if reverse else entry
                    p1 = finish[2:] + finish[:2] if reverse else finish
                    s = self.one([quote(0, p0), quote(1, p1)])
                    self.assertEqual(s['cycles'], int(closes))
                    if closes:
                        c = s['cycles_rows'][0]
                        self.assertEqual(c['exit_reason'], 'stop_loss')
                        if xl == 99899:
                            self.assertEqual(D(c['net_before_funding']) - D(c['initial_quote_net_proxy']), -D(c['qty']) * D('.002'))
                        self.assert_accounting(s)

    def test_take_profit_is_net_after_four_fees_with_exact_boundary(self):
        entry = (100000, 99999, 102001, 102000)
        for xl, xs, closes in ((98084, 99916, False), (98085, 99915, True), (98086, 99914, True)):
            with self.subTest(xl=xl):
                s = self.one([quote(0, entry), quote(1, (xl + 1, xl, xs, xs - 1))])
                self.assertEqual(s['cycles'], int(closes))
                if closes:
                    c = s['cycles_rows'][0]
                    self.assertEqual(c['exit_reason'], 'take_profit')
                    if xl == 98085:
                        self.assertEqual(D(c['net_before_funding']), D(c['qty']) * D('.0005'))
                    self.assert_accounting(s)

    def test_hard_net_stop_exact_boundary_and_priority_over_relative(self):
        entry = (100000, 99999, 102001, 102000)
        for xl, xs, closes in ((97561, 100439, False), (97560, 100440, True), (97559, 100441, True)):
            with self.subTest(xl=xl):
                s = self.one([quote(0, entry), quote(1, (xl + 1, xl, xs, xs - 1))],
                             policy=('case', 21, 5, 10000, 100, 60000))
                self.assertEqual(s['cycles'], int(closes))
                if closes:
                    c = s['cycles_rows'][0]
                    self.assertEqual(c['exit_reason'], 'hard_stop_loss')
                    if xl == 97560:
                        self.assertEqual(D(c['net_before_funding']), -D(c['qty']) * D('.01'))
        s = self.one([quote(0, entry), quote(1, (97561, 97560, 100440, 100439))])
        self.assertEqual(s['cycles_rows'][0]['exit_reason'], 'hard_stop_loss')

    def test_hold_deadline_is_ns_precise_and_exit_priority(self):
        for extra, closes in ((-1, False), (0, True), (1, True)):
            with self.subTest(extra=extra):
                ticks = [quote(123), quote(123 + 60 * SEC + extra)]
                s = self.one(ticks)
                self.assertEqual(s['cycles'], int(closes))
                if closes:
                    self.assertEqual(s['cycles_rows'][0]['holding_ns'], str(60 * SEC + extra))
                    self.assertEqual(s['cycles_rows'][0]['exit_reason'], 'holding_timeout')
        loss = (90001, 90000, 110301, 110300)
        for force, reason in ((0, 'holding_timeout'), (1, 'holding_timeout'), (2, 'replay_day_end')):
            s = self.one([quote(0), quote(60 * SEC, loss, force=force)])
            self.assertEqual(s['cycles_rows'][0]['exit_reason'], reason)

    def test_funding_force_on_invalid_quote_persists_until_new_valid_quote(self):
        ticks = [quote(0), quote(1, (1, 1, 1, 1), valid=False),
                 quote(60 * SEC, (100201, 100200, 100001, 100000), history=False)]
        rows = [active(ticks[0])] + [active(t, mask=0) for t in ticks[1:]]
        schedule = funding_rows([(v, BASE - SEC, BASE + 120 * SEC + 1) for v in ('bybit', 'bitget')])
        s = self.one(ticks, rows=rows, funding=schedule)
        c = s['cycles_rows'][0]
        self.assertEqual((c['exit_reason'], int(c['first_trigger_ns']), int(c['exit_ns'])),
                         ('funding_guard', BASE + 1, BASE + 60 * SEC))
        self.assertEqual([D(o['price']) for o in s['orders_rows'][2:]], [D('1.002'), D('1.00001')])

    def test_same_tick_entry_hard_stop_and_no_same_tick_reopen(self):
        # Very wide local spread means the reverse-L1 net already breaches hard100.
        s = self.one([quote(0, (100000, 98000, 105001, 105000), qty=80)])
        self.assertEqual((s['orders'], s['cycles'], s['unresolved_qty']), (4, 1, 0))
        c = s['cycles_rows'][0]
        self.assertEqual(c['exit_reason'], 'hard_stop_loss')
        self.assertEqual(c['holding_ns'], '0')
        self.assertTrue(flag(c['initial_hard_budget_breach']))
        self.assertTrue(all(int(o['ns']) == BASE for o in s['orders_rows']))
        self.assert_accounting(s)

    def test_cooldown_30_seconds_exact_ns_and_no_position_overlap(self):
        # Close at BASE+123ns; all quote clocks in one millisecond remain distinct.
        ticks = [quote(0), quote(1), quote(123, (100201, 100200, 100001, 100000)),
                 quote(30 * SEC + 122), quote(30 * SEC + 123), quote(30 * SEC + 124)]
        s = self.one(ticks)
        self.assertEqual((s['orders'], s['cycles'], s['unresolved_qty']), (6, 1, 80))
        self.assertEqual([int(o['ns']) for o in s['orders_rows'] if o['intent'] == 'entry'],
                         [BASE, BASE, BASE + 30 * SEC + 123, BASE + 30 * SEC + 123])
        self.assert_accounting(s)

    def test_realized_loss_cap_halts_new_entries_with_continuous_accounts(self):
        ticks = [quote(0, qty=10000), quote(1, (98001, 98000, 105001, 105000), qty=10000),
                 quote(31 * SEC, qty=10000), quote(61 * SEC, qty=10000)]
        s = self.one(ticks)
        self.assertEqual((s['orders'], s['cycles']), (4, 1))
        self.assertLessEqual(D(s['net_before_funding']), -10)
        self.assertEqual(s['halt_reason'], 'total_loss_cap')
        self.assert_accounting(s)

    def test_end_without_valid_quote_preserves_unresolved_without_synthetic_fill(self):
        for force in (0, 2):
            with self.subTest(force=force):
                s = self.one([quote(0), quote(70 * SEC, valid=False, force=force)])
                self.assertEqual((s['orders'], s['cycles'], s['unresolved_qty']), (2, 0, 80))
                self.assertTrue(all(int(o['ns']) == BASE for o in s['orders_rows']))
                self.assert_accounting(s)
        s = self.one([quote(0), quote(1, force=2)])
        self.assertEqual((s['orders'], s['cycles'], s['unresolved_qty']), (4, 1, 0))
        self.assertEqual(s['cycles_rows'][0]['exit_reason'], 'replay_day_end')

    def test_future_quote_mutation_cannot_change_completed_orders(self):
        prefix = [quote(0), quote(1, (100201, 100200, 100001, 100000))]
        a = self.one(prefix + [quote(SEC, valid=False)])
        b = self.one(prefix + [quote(SEC, (1, 1, 1, 1), force=2)])
        self.assertEqual(a['orders_rows'], b['orders_rows'])
        self.assertEqual(a['cycles_rows'], b['cycles_rows'])

    def test_different_policies_have_isolated_cash_and_positions(self):
        ps = POLICY_HEADER + 'quick,21,5,20,100,1\nslow,21,5,20,100,60000\n'
        _, _, _, ss = self.run_case([quote(0), quote(MS)], policies=ps)
        self.assertEqual((ss['quick']['orders'], ss['slow']['orders']), (4, 2))
        self.assertEqual((ss['quick']['unresolved_qty'], ss['slow']['unresolved_qty']), (0, 80))
        for scenario in ss.values():
            self.assert_accounting(scenario)

    def test_dayend_prohibition_legacy_force1_ignored_and_activity_alignment(self):
        self.assertEqual(self.one([quote(0, force=1)])['orders'], 2)
        self.assertEqual(self.one([quote(0, force=2), quote(SEC)])['orders'], 0)
        t = quote(0)
        for rows, extra, message in (([], b'', 'activity/tape timestamp or length mismatch'),
                                      ([active(t, ns=BASE + 1)], b'', 'activity/tape timestamp or length mismatch'),
                                      ([active(t), active(quote(1))], b'', 'activity has extra rows'),
                                      ([active(t)], b'\0', 'truncated activity row')):
            with self.subTest(message=message):
                self.run_case([t], rows=rows, activity_extra=extra, failure=message)

    def test_causal_funding_known_unknown_expired_and_exact_window(self):
        for delta, allowed in ((-1, False), (0, False), (1, True)):
            with self.subTest(delta=delta):
                schedule = funding_rows([(v, BASE, BASE + 120 * SEC + delta) for v in ('bybit', 'bitget')])
                self.assertEqual(self.one([quote(0)], funding=schedule)['orders'], 2 if allowed else 0)
        # Knowledge arriving one nanosecond later cannot authorize the first tick.
        schedule = funding_rows([(v, BASE + 1, BASE + 1000 * SEC) for v in ('bybit', 'bitget')])
        s = self.one([quote(0), quote(1)], funding=schedule)
        self.assertEqual([int(o['ns']) for o in s['orders_rows']], [BASE + 1, BASE + 1])
        # Observations which have expired at the current receive clock are unsafe.
        expired = funding_rows([(v, BASE - 2, BASE - 1) for v in ('bybit', 'bitget')])
        self.assertEqual(self.one([quote(0)], funding=expired)['orders'], 0)

    def test_future_schedule_update_never_applies_early_and_guard_scales_with_hold(self):
        # The old observation enters its 120s guard at t=1ns; its replacement is
        # received at t=2ns, so the already held position must close at t=1ns.
        rows = [(v, BASE - SEC, BASE + 120 * SEC + 1) for v in ('bybit', 'bitget')]
        rows += [(v, BASE + 2, BASE + 1000 * SEC) for v in ('bybit', 'bitget')]
        s = self.one([quote(0), quote(1), quote(2)], funding=funding_rows(rows))
        c = s['cycles_rows'][0]
        self.assertEqual((c['exit_reason'], int(c['exit_ns'])), ('funding_guard', BASE + 1))
        ps = POLICY_HEADER + 'short,21,5,20,100,1000\nlong,21,5,20,100,60000\n'
        schedule = funding_rows([(v, BASE - SEC, BASE + 100 * SEC) for v in ('bybit', 'bitget')])
        _, _, _, ss = self.run_case([quote(0)], funding=schedule, policies=ps)
        self.assertEqual((ss['short']['orders'], ss['long']['orders']), (2, 0))

    def test_funding_guard_priority_over_hold_risk_but_below_dayend(self):
        schedule = funding_rows([(v, BASE - SEC, BASE + 180 * SEC) for v in ('bybit', 'bitget')])
        for force, reason in ((0, 'funding_guard'), (2, 'replay_day_end')):
            s = self.one([quote(0), quote(60 * SEC, (90001, 90000, 110301, 110300), force=force)],
                         funding=schedule)
            self.assertEqual(s['cycles_rows'][0]['exit_reason'], reason)

    def test_funding_contract_rejects_malformed_or_missing_venue(self):
        valid = [(v, BASE - SEC, BASE + 1000 * SEC) for v in ('bybit', 'bitget')]
        for rows in ([], valid[:1], [('unknown', BASE - SEC, BASE + SEC)] + valid,
                     valid + [('bybit', BASE - SEC, BASE + 2000 * SEC)],
                     [('bybit', BASE, BASE), ('bitget', BASE, BASE + SEC)],
                     [('bybit', BASE + 1, BASE + SEC), ('bitget', BASE, BASE + SEC)]):
            with self.subTest(rows=rows):
                self.run_case([quote(0)], funding=funding_rows(rows), failure='funding')

    def test_illegal_policies_and_exclusive_output(self):
        invalid = [POLICY_HEADER,
                   POLICY_HEADER + '../bad,21,5,20,100,60000\n',
                   POLICY_HEADER + 'a,21,5,20,100,60000\na,21,5,20,100,60000\n',
                   POLICY_HEADER.replace('entry_bps', 'entry'),
                   POLICY_HEADER + 'a,21,5,20,100,60000,unexpected\n']
        for index, values in ((1, '-1'), (1, '10001'), (2, '0'), (3, '0'),
                              (4, '0'), (5, '0'), (5, '86400001'), (2, '2.5'), (3, '2x')):
            row = list(map(str, DEFAULT_POLICY))
            row[index] = values
            invalid.append(POLICY_HEADER + ','.join(row) + '\n')
        for policy in invalid:
            with self.subTest(policy=policy):
                # Numeric parsing is deliberately shared with threshold CSVs,
                # so malformed policy numbers may name that parser in stderr.
                self.run_case([quote(0)], policies=policy, failure='ERROR:')
        root, args, _, _ = self.run_case([quote(0)])
        original = (root / 'out' / 'aggregate.json').read_bytes()
        proc = subprocess.run(args, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('output already exists', proc.stderr)
        self.assertEqual((root / 'out' / 'aggregate.json').read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
