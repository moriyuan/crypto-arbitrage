"""Small analytical-contract tests; no replay or real market files."""
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest

from attribution import bin_name, build, cycle_record, diagnostic_policy, temporal_losses, totals


def fixture(long='bybit', close_long='1.004', close_short='1.009', display=20):
    short = 'bitget' if long == 'bybit' else 'bybit'
    entry = {long: D('1'), short: D('1.009')}
    closed = {long: D(close_long), short: D(close_short)}
    quotes = {long: {'ask': D('1'), 'bid': D('.999')}, short: {'ask': D('1.010'), 'bid': D('1.009')}}
    cycle = {'cycle': '1', 'qty': '10', 'entry_ns': '1790856000000000000', 'exit_ns': '1790856001000000001',
             'holding_ns': '1000000001', 'long_venue': long, 'short_venue': short,
             'long_entry_price': '1', 'short_entry_price': '1.009', 'long_exit_price': close_long,
             'short_exit_price': close_short, 'exit_reason': 'holding_timeout',
             'initial_quote_net_proxy': '-0.032054', 'initial_hard_budget_breach': '0'}
    orders, total_gross, total_fees = [], D(0), D(0)
    for venue in ('bybit', 'bitget'):
        venue_fees = D(0)
        for intent in ('entry', 'exit'):
            price = entry[venue] if intent == 'entry' else closed[venue]
            fee = price*10*D('.0003')
            paid_display = 20 if intent == 'entry' else display
            orders.append({'venue': venue, 'intent': intent, 'ns': cycle[intent+'_ns'], 'qty': '10',
                           'side': ('buy' if venue == long else 'sell') if intent == 'entry' else ('sell' if venue == long else 'buy'),
                           'price': str(price), 'fee': str(fee), 'l1_display_qty': str(paid_display),
                           'excess_depth_qty': str(max(0, 10-paid_display)) if intent == 'exit' else '0',
                           'exit_below_min': str(int(intent == 'exit' and price*10 < 5)),
                           'exit_reason': cycle['exit_reason'] if intent == 'exit' else ''})
            venue_fees += fee
        gross = 10*(closed[venue]-entry[venue])*(1 if venue == long else -1)
        cycle[venue+'_net_before_funding'] = str(gross-venue_fees)
        total_gross += gross
        total_fees += venue_fees
        for side in ('ask', 'bid'):
            cycle['entry_'+venue+'_'+side] = str(quotes[venue][side])
    cycle.update(gross_pnl=str(total_gross), total_fees=str(total_fees), net_before_funding=str(total_gross-total_fees),
                 exit_depth_assumed=str(int(display < 10)), exit_min_notional_assumed=str(int(min(closed.values())*10 < 5)))
    return cycle, orders


class AttributionTests(unittest.TestCase):
    def test_exact_liquidation_cost_and_non_tp_winner(self):
        row = cycle_record('2026-10-01', 'test', *fixture())
        self.assertEqual(row['gross_usdt'], D('.04'))
        self.assertEqual(row['fees_usdt'], D('.012066'))
        self.assertEqual(row['net_before_funding_usdt'], D('.027934'))
        self.assertEqual(row['entry_liquidation_cost_bps'], D('32.054'))
        self.assertEqual(row['holding_seconds'], D('1.000000001'))
        stats = totals([row])
        self.assertEqual((stats['net_wins'], stats['take_profit_triggers'], stats['net_wins_without_take_profit']), (1, 0, 1))

    def test_fee_swallowed_profit_and_direction_symmetry(self):
        rows = [cycle_record('2026-10-01', 'test', *fixture(long=v, close_long='1.0005')) for v in ('bybit', 'bitget')]
        self.assertEqual(rows[0]['net_before_funding_usdt'], D('-.0070555'))
        self.assertEqual(rows[0]['net_before_funding_usdt'], rows[1]['net_before_funding_usdt'])
        self.assertEqual(totals(rows)['gross_positive_net_negative'], 2)
        self.assertEqual(len(diagnostic_policy(rows)['groups']['direction']), 2)

    def test_depth_and_minimum_flags_preserve_idealization(self):
        row = cycle_record('2026-10-01', 'test', *fixture(close_long='.4', close_short='.402', display=5))
        self.assertTrue(row['exit_depth_assumed'])
        self.assertTrue(row['exit_min_notional_assumed'])
        self.assertEqual(row['exit_excess_depth_qty_sum_legs'], 10)
        self.assertEqual(diagnostic_policy([row])['execution_assumption_effect']['depth_exceeded_cycle_fraction'], D(1))

    def test_bad_fee_rejected(self):
        cycle, orders = fixture()
        orders[0]['fee'] = '.006'
        with self.assertRaisesRegex(ValueError, 'fee !='):
            cycle_record('2026-10-01', 'test', cycle, orders)

    def test_bins_are_lower_inclusive(self):
        edges = [D(15), D(30)]
        self.assertEqual(bin_name(D('14.99'), edges), '<15')
        self.assertEqual(bin_name(D(15), edges), '[15,30)')
        self.assertEqual(bin_name(D(30), edges), '>=30')

    def test_unfrozen_experiment_cannot_read_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                build(directory)
            self.assertFalse((Path(directory)/'attribution.json').exists())

    def test_loss_streak_resets_on_zero_and_each_date(self):
        base = cycle_record('2026-10-01', 'test', *fixture())
        rows = []
        for index, net in enumerate((-1, -2, 0, -3, -4, -5, 1)):
            rows.append({**base, 'cycle': index+1, 'exit_ns': base['exit_ns']+index,
                         'net_before_funding_usdt': D(net)})
        rows.append({**rows[-2], 'date': '2026-10-02', 'cycle': 1})
        result = temporal_losses(rows)
        self.assertEqual(result['2026-10-01']['max_consecutive_net_losses'], 3)
        self.assertEqual(result['2026-10-01']['max_streak_net_usdt'], D(-12))
        self.assertEqual(result['2026-10-01']['absolute_loss_usdt'], D(15))
        self.assertEqual(result['2026-10-02']['max_consecutive_net_losses'], 1)


if __name__ == '__main__':
    unittest.main()
