"""Focused synthetic execution tests; optional frozen-Python differential oracle.

Run: /Users/mori/code/crypto/.venv/bin/python test_screen.py
Only compiles a temporary binary and creates temporary synthetic tapes/results.
"""
import csv
import importlib.util
import json
from pathlib import Path
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal as D

HERE = Path(__file__).resolve().parent
FROZEN = Path('/Users/mori/code/crypto/state/arb-runs/20261012-btw-l1-net-2bps-code')
SOURCE = Path('/Users/mori/.codex/worktrees/crypto-arbitrage-research/crypto/src')
BASE = 1790812800000000000


def quote(ms, prices=(100000, 99999, 100301, 100300), qty=80,
          valid=True, force=0, history=True, extra=None):
    # Side order Aask, Abid, Bask, Bbid, each mapping includes its L1.
    sizes = [qty] * 4 if isinstance(qty, int) else qty
    levels = [{p: q} for p, q in zip(prices, sizes)]
    if extra:
        for side, additions in extra.items():
            levels[side].update(additions)
    return dict(ns=BASE + ms * 1_000_000, prices=prices, sizes=sizes,
                valid=valid, force=force, history=history, levels=levels)


def tape(path, ticks):
    previous = [{}, {}, {}, {}]
    with path.open('wb') as stream:
        stream.write(b'BTWTP002')
        for t in ticks:
            updates = []
            if t['valid']:
                for side in range(4):
                    cur, old = t['levels'][side], previous[side]
                    for price in sorted(old.keys() | cur.keys()):
                        if cur.get(price, 0) != old.get(price, 0):
                            price_units=D(price)*1000
                            if price_units!=price_units.to_integral_value():
                                raise ValueError('update price exceeds 1e-8 precision')
                            updates.append((side, int(price_units), cur.get(price, 0)))
                    previous[side] = cur
            stream.write(struct.pack('<qBBBBI8q', t['ns'], t['valid'], t['force'],
                                     t['history'], 0, len(updates), *t['prices'], *t['sizes']))
            for u in updates:
                stream.write(struct.pack('<B7xqq', *u))


class ScreenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix='btw-screen-test-')
        cls.binary = Path(cls.build.name) / 'screen'
        compiler = shutil.which('clang++') or shutil.which('g++')
        if not compiler:
            raise RuntimeError('C++17 compiler unavailable')
        subprocess.run([compiler, '-std=c++17', '-O2', '-Wall', '-Wextra',
                        str(HERE / 'screen.cpp'), '-o', str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def run_tape(self, ticks, all_scenarios=False):
        temp = tempfile.TemporaryDirectory(prefix='btw-screen-case-')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        source, out = root / 'tape.bin', root / 'output'
        tape(source, ticks)
        args = [str(self.binary), str(source), str(out)]
        if not all_scenarios:
            args += ['--baseline-only']
        subprocess.run(args, capture_output=True, text=True, check=True)
        summary = json.loads((out / 'aggregate.json').read_text())
        result = {}
        for s in summary['scenarios']:
            d = out / s['name']
            with (d / 'orders.csv').open() as f:
                s['order_rows'] = list(csv.DictReader(f))
            with (d / 'cycles.csv').open() as f:
                s['cycle_rows'] = list(csv.DictReader(f))
            result[s['name']] = s
        return result, root

    def baseline(self, ticks):
        return self.run_tape(ticks)[0]['baseline_net_tp2_sl2']

    def test_fee_only_stop_and_exact_cash_reconciliation(self):
        s = self.baseline([quote(0), quote(100)])
        self.assertEqual((s['orders'], s['cycles'], s['losses']), (4, 1, 1))
        c = s['cycle_rows'][0]
        self.assertEqual(c['exit_reason'], 'stop_loss')
        self.assertEqual(D(c['net_before_funding']), D(c['gross_pnl']) - D(c['total_fees']))
        self.assertEqual(sum(map(D, s['cash'].values())) - 2000, D(c['net_before_funding']))
        self.assertEqual(len({r['qty'] for r in s['order_rows']}), 1)
        for r in s['order_rows']:
            self.assertEqual(D(r['fee']), D(r['price']) * D(r['qty']) * D('.0003'))

    def test_inclusive_exact_net_boundaries(self):
        entry = (100000, 99999, 102001, 102000)
        for reverse in (False, True):
            for xl, xs, expected in ((98069, 99930, None), (98070, 99930, 'take_profit'),
                                     (98071, 99930, 'take_profit'),
                                     (98051, 99950, None), (98050, 99950, 'stop_loss'),
                                     (98049, 99950, 'stop_loss')):
                exit_prices = (xl+1, xl, xs, xs-1)
                p0 = entry[2:] + entry[:2] if reverse else entry
                p1 = exit_prices[2:] + exit_prices[:2] if reverse else exit_prices
                s = self.baseline([quote(0, p0), quote(100, p1)])
                self.assertEqual(s['cycles'], int(expected is not None), (reverse, xl, xs, s))
                if expected:
                    self.assertEqual(s['cycle_rows'][0]['exit_reason'], expected)
                    if (xl, xs) in ((98070, 99930), (98050, 99950)):
                        self.assertEqual(D(s['net_before_funding']), D('.004') * (1 if expected == 'take_profit' else -1))

    def test_partial_exit_sticky_reason_and_no_budget_reset(self):
        ticks = [quote(0), quote(100, qty=(80, 40, 40, 80)),
                 quote(200, qty=(80, 40, 40, 80)),
                 quote(300, (100201, 100200, 100001, 100000), qty=40)]
        s = self.baseline(ticks)
        self.assertEqual((s['orders'], s['cycles']), (6, 1))
        self.assertEqual(s['cycle_rows'][0]['exit_parts'], '2')
        self.assertEqual(s['cycle_rows'][0]['exit_reason'], 'stop_loss')
        self.assertEqual([int(r['ns']) - BASE for r in s['order_rows'] if r['intent'] == 'exit'],
                         [100_000_000, 100_000_000, 300_000_000, 300_000_000])

    def test_retained_top50_and_deleted_level_budget(self):
        # Entry consumes A ask 100000 and B bid 100300. Exit at t100. At t30200
        # both entry levels return to best, still present in top50: no new fill.
        t0 = quote(0)
        t1 = quote(100)
        t2 = quote(30100, (99990,99989,100311,100310),
                   extra={0:{100000:80},3:{100300:80}})
        t3 = quote(30200)
        s = self.baseline([t0,t1,t2,t3])
        # t2 is itself eligible and enters using its fresh prices; therefore
        # isolate returned-level behavior using disabled history at t2.
        t2['history'] = False
        s = self.baseline([t0,t1,t2,t3])
        self.assertEqual(s['orders'], 4)
        removed = quote(30300, (99990,99989,100311,100310), history=False)
        returned = quote(30400)
        s = self.baseline([t0,t1,t2,t3,removed,returned])
        self.assertEqual(s['orders'], 6)

    def test_invalid_feed_does_not_update_budget_and_force_priority(self):
        s = self.baseline([quote(0), quote(100, valid=False, force=1), quote(60100)])
        self.assertEqual(s['cycle_rows'][0]['exit_reason'], 'funding_guard')
        self.assertEqual(s['cycle_rows'][0]['exit_ns'], str(BASE+60100*1_000_000))
        s = self.baseline([quote(0), quote(60000)])
        self.assertEqual(s['cycle_rows'][0]['exit_reason'], 'holding_timeout')

    def test_cooldown_and_same_tick_reentry_gate(self):
        ticks=[quote(0),quote(100),quote(200,qty=160),quote(30099,qty=160),quote(30100,qty=160)]
        s = self.baseline(ticks)
        self.assertEqual(s['orders'], 6)
        self.assertEqual(s['order_rows'][-1]['ns'], str(BASE+30100*1_000_000))

    def test_relative_does_not_stop_merely_due_to_entry_fees(self):
        result, _ = self.run_tape([quote(0),quote(100)], True)
        self.assertEqual(result['baseline_net_tp2_sl2']['cycles'], 1)
        for name,s in result.items():
            if name.startswith('entry_deterioration'):
                self.assertEqual(s['cycles'], 0)
                self.assertEqual(s['unresolved_qty'], 20)

    def test_relative_deterioration_and_hard_cap(self):
        result,_ = self.run_tape([quote(0),quote(100,(99951,99950,100351,100350))], True)
        self.assertEqual(result['entry_deterioration_tp2_sl5']['cycle_rows'][0]['exit_reason'], 'stop_loss')
        self.assertEqual(result['entry_deterioration_tp2_sl20']['cycles'], 0)
        # Wide venue spreads put the initial reverse quote net below hard cap.
        entry=(100000,99000,101300,100300)
        result,_=self.run_tape([quote(0,entry),quote(100,entry)],True)
        s=result['entry_deterioration_tp2_sl20']
        self.assertEqual((s['orders'],s['initial_hard_budget_breaches']), (4,1))
        self.assertEqual(s['cycle_rows'][0]['exit_reason'], 'hard_stop_loss')

    def test_baseline_after_entry_budget_snapshot_and_first_trigger(self):
        result,root=self.run_tape([quote(0),quote(100,valid=False,force=1),quote(200)])
        d=root/'output'/'baseline_net_tp2_sl2'
        snapshot=json.loads((d/'entry_budgets.jsonl').read_text())
        self.assertEqual(snapshot['entry_ns'],BASE)
        self.assertEqual(snapshot['entry_prices'],{'bybit':100000,'bitget':100300})
        self.assertEqual(snapshot['long'],'bybit')
        self.assertEqual(snapshot['qty'],20)
        self.assertTrue(snapshot['after_entry_consumption'])
        self.assertEqual(snapshot['price_units'],'1e-8')
        self.assertEqual(snapshot['sides'][0][0]['price_units_1e8'],100000000)
        self.assertEqual([s[0]['remaining_quarter'] for s in snapshot['sides']], [0,80,80,0])
        self.assertEqual(result['baseline_net_tp2_sl2']['cycle_rows'][0]['first_trigger_ns'],str(BASE+100_000_000))

    def test_off_grid_far_level_preserved_exactly(self):
        # A far depth level may not obey today's L1 tick rule. It must survive
        # intact as an observation key, never round onto an adjacent level.
        ticks=[quote(0,extra={0:{D('100001.4'):67,100001:80}}),
               quote(100,extra={0:{D('100001.4'):59,100001:80}})]
        result,root=self.run_tape(ticks)
        snapshot=json.loads((root/'output'/'baseline_net_tp2_sl2'/'entry_budgets.jsonl').read_text())
        prices={r['price_units_1e8']:r for r in snapshot['sides'][0]}
        self.assertEqual(prices[100001400]['displayed_qty'],67)
        self.assertEqual(prices[100001000]['displayed_qty'],80)
        self.assertEqual(result['baseline_net_tp2_sl2']['orders'],4)

    def test_total_loss_cap_stops_after_closed_pair(self):
        s=self.baseline([quote(0,qty=800),quote(100,(70001,70000,130000,129999),qty=800),
                         quote(40000,qty=1600)])
        self.assertEqual(s['cycles'],1)
        self.assertEqual(s['orders'],4)
        self.assertLess(D(s['net_before_funding']),-10)
        self.assertEqual(s['halt_reason'],'total_loss_cap')

    def test_maximum_entry_notional_conservative_upper_price(self):
        s=self.baseline([quote(0,qty=4000)])
        self.assertEqual(s['order_rows'][0]['qty'],'240')
        for r in s['order_rows']:
            self.assertLessEqual(D(r['qty'])*D(r['price']),250)

    @unittest.skipUnless((HERE/'runner.py').exists(), 'candidate Python implementation unavailable')
    def test_all_candidates_against_authoritative_python(self):
        rng=random.Random(9093)
        ticks=[]
        for i in range(350):
            reverse=(i//9)%2
            mid=100000+rng.randrange(-200,201)
            gap=rng.choice([100,250,300,400])
            spread=rng.choice([1,10,100,1000])
            prices=(mid,mid-spread,mid+gap+spread,mid+gap)
            if reverse:prices=prices[2:]+prices[:2]
            ticks.append(quote(i*5300,prices,qty=[rng.choice([20,40,80,160,400,2000]) for _ in range(4)],
                               valid=i%53!=0,force=2 if i==349 else (1 if i%97==0 else 0)))
        result,root=self.run_tape(ticks,True)
        # Run in a clean interpreter: frozen and candidate modules have identical
        # names, and must never reuse one another's Python import cache.
        (root/'ticks.json').write_text(json.dumps(ticks))
        (root/'policies.json').write_text(json.dumps([{k:s[k] for k in ('name','mode','tp_bps','sl_bps','hard_cap_bps')} for s in result.values()]))
        code='''
import json,sys
from pathlib import Path
from decimal import Decimal as D
sys.path[:0]=sys.argv[2:4]
from runner import FirstLevelPaper
from risk_policy import Policy
from btc_cta.cross_arb.market import Book,Rules
from btc_cta.cross_arb.depth_sizing import DepthSizingConfig
root=Path(sys.argv[1]);ticks=json.loads((root/'ticks.json').read_text());results={}
rules={v:Rules(D(10),D(10),D(5),D('.00001')) for v in ('bybit','bitget')}
for config in json.loads((root/'policies.json').read_text()):
    policy=Policy(bybit_fee_bps=D(3),bitget_fee_bps=D(3),price_guard_bps=D(0),max_cycles=10000,
                  risk_mode=config['mode'],take_profit_bps=D(config['tp_bps']),stop_loss_bps=D(config['sl_bps']),hard_stop_loss_bps=D(100))
    s=FirstLevelPaper(root/(config['name']+'.sqlite'),policy,symbol='BTWUSDT',depth_sizing=DepthSizingConfig())
    for t in ticks:
        now=t['ns']//1000000;books=None
        if t['valid']:
            books={}
            for v,name in enumerate(('bybit','bitget')):
                asks=tuple((D(p)/100000,D(q)) for p,q in sorted(((int(p),q) for p,q in t['levels'][2*v].items())))
                bids=tuple((D(p)/100000,D(q)) for p,q in sorted(((int(p),q) for p,q in t['levels'][2*v+1].items()),reverse=True))
                books[name]=Book(name,bids,asks,now,now,0)
        s.context={'decision_cursor_ns':t['ns']}
        s.tick_offline(books,rules,now,{0:None,1:'funding_guard:test',2:'replay_day_end'}[t['force']])
    results[config['name']]={'orders':[json.loads(r[0]) for r in s.db.execute('SELECT value FROM orders ORDER BY id')],
        'cycles':[json.loads(r[0]) for r in s.db.execute('SELECT value FROM cycles ORDER BY id')],'cash':s.state['cash']}
    s.close()
(root/'reference.json').write_text(json.dumps(results))
'''
        subprocess.run([sys.executable,'-c',code,str(root),str(HERE),str(SOURCE)],capture_output=True,text=True,check=True)
        reference=json.loads((root/'reference.json').read_text())
        for name,cc in result.items():
            py=reference[name]
            self.assertEqual(len(py['orders']),cc['orders'],name)
            self.assertEqual(len(py['cycles']),cc['cycles'],name)
            for po,co in zip(py['orders'],cc['order_rows']):
                self.assertEqual(po['decision_cursor_ns'],int(co['ns']),name)
                for k in ('venue','side','intent'):self.assertEqual(po[k],co[k],name)
                for k in ('qty','price','fee'):self.assertEqual(D(po[k]),D(co[k]),name)
            for pc,sc in zip(py['cycles'],cc['cycle_rows']):
                for k in ('gross_pnl','total_fees','net_before_funding','bybit_net_before_funding','bitget_net_before_funding'):
                    self.assertEqual(D(pc[k]),D(sc[k]),(name,k))
                self.assertEqual(pc['exit_reason'].split(':')[0],sc['exit_reason'],name)
            self.assertEqual({v:D(c) for v,c in py['cash'].items()},{v:D(c) for v,c in cc['cash'].items()},name)

    def test_history_and_entry_threshold(self):
        s = self.baseline([quote(0,history=False),quote(100,(100000,99999,100201,100200)),quote(200)])
        self.assertEqual(s['orders'],2)
        self.assertEqual(s['order_rows'][0]['ns'],str(BASE+200*1_000_000))

    def test_existing_output_and_truncated_tape_fail_closed(self):
        _,root=self.run_tape([quote(0)])
        r=subprocess.run([str(self.binary),str(root/'tape.bin'),str(root/'output')],capture_output=True,text=True)
        self.assertNotEqual(r.returncode,0)
        self.assertIn('refusing overwrite',r.stderr)
        (root/'bad.bin').write_bytes(b'BTWTP002'+b'\0')
        r=subprocess.run([str(self.binary),str(root/'bad.bin'),str(root/'bad-output')],capture_output=True,text=True)
        self.assertNotEqual(r.returncode,0)
        self.assertFalse((root/'bad-output'/'aggregate.json').exists())

    @unittest.skipUnless(FROZEN.exists() and SOURCE.exists(), 'frozen Python source unavailable')
    def test_differential_against_frozen_python_execution(self):
        sys.path[:0]=[str(FROZEN),str(SOURCE)]
        spec=importlib.util.spec_from_file_location('screen_frozen_runner',FROZEN/'runner.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        from btc_cta.cross_arb.market import Book, Rules
        from btc_cta.cross_arb.depth_sizing import DepthSizingConfig
        from risk_policy import Policy
        rules={v:Rules(D(10),D(10),D(5),D('.00001')) for v in ('bybit','bitget')}
        rng=random.Random(8849)
        ticks=[]
        # Hundreds of entry/exit opportunities, realistic integer tick prices,
        # variable depth, invalidations, both directions, and funding/day end.
        for i in range(650):
            reverse=(i//7)%2
            mid=100000+rng.randrange(-100,101)
            gap=rng.choice([100,250,300,400])
            prices=(mid,mid-rng.choice([1,10,100]),mid+gap+1,mid+gap)
            if reverse:prices=prices[2:]+prices[:2]
            ticks.append(quote(i*5300,prices,qty=[rng.choice([20,40,80,160,400,2000]) for _ in range(4)],
                               valid=i%53!=0,force=2 if i==649 else (1 if i%97==0 else 0)))
        cpp,root=self.run_tape(ticks)
        cpp=cpp['baseline_net_tp2_sl2']
        policy=Policy(bybit_fee_bps=D(3),bitget_fee_bps=D(3),price_guard_bps=D(0),max_cycles=10000)
        session=module.FirstLevelPaper(root/'reference.sqlite',policy,symbol='BTWUSDT',depth_sizing=DepthSizingConfig())
        for t in ticks:
            now=t['ns']//1_000_000
            books=None
            if t['valid']:
                books={}
                for v,name in enumerate(('bybit','bitget')):
                    asks=tuple((D(p)/100000,D(q)) for p,q in sorted(t['levels'][2*v].items()))
                    bids=tuple((D(p)/100000,D(q)) for p,q in sorted(t['levels'][2*v+1].items(),reverse=True))
                    books[name]=Book(name,bids,asks,now,now,0)
            session.context={'decision_cursor_ns':t['ns']}
            session.tick_offline(books,rules,now,{0:None,1:'funding_guard:test',2:'replay_day_end'}[t['force']])
        pyorders=[json.loads(r[0]) for r in session.db.execute('SELECT value FROM orders ORDER BY id')]
        self.assertEqual(len(pyorders),cpp['orders'])
        for py,cc in zip(pyorders,cpp['order_rows']):
            self.assertEqual(py['decision_cursor_ns'],int(cc['ns']))
            for k in ('venue','side','intent'):self.assertEqual(py[k],cc[k])
            for k in ('qty','price','fee'):self.assertEqual(D(py[k]),D(cc[k]))
        pycycles=[json.loads(r[0]) for r in session.db.execute('SELECT value FROM cycles ORDER BY id')]
        self.assertEqual(len(pycycles),cpp['cycles'])
        for py,cc in zip(pycycles,cpp['cycle_rows']):
            for k in ('gross_pnl','total_fees','net_before_funding','bybit_net_before_funding','bitget_net_before_funding'):
                self.assertEqual(D(py[k]),D(cc[k]))
            for k in ('exit_parts','holding_ms'):self.assertEqual(py[k],int(cc[k]))
            self.assertEqual(py['exit_reason'].split(':')[0],cc['exit_reason'])
        self.assertEqual({v:D(c) for v,c in session.state['cash'].items()}, {v:D(c) for v,c in cpp['cash'].items()})
        equity=[json.loads(r[0]) for r in session.db.execute('SELECT value FROM equity ORDER BY id')]
        peak=D(2000);drawdown=D(0);complete=incomplete=0
        for i in range(0,len(equity),2):
            values=[row['liquidation_equity_before_funding'] for row in equity[i:i+2]]
            if None in values:
                incomplete+=1;continue
            complete+=1;value=sum(map(D,values));peak=max(peak,value);drawdown=max(drawdown,peak-value)
        self.assertEqual(cpp['complete_liquidation_equity_marks'],complete)
        self.assertEqual(cpp['incomplete_liquidation_equity_marks'],incomplete)
        self.assertEqual(D(cpp['complete_mark_max_drawdown_usdt']),drawdown)
        session.close()


if __name__=='__main__':
    unittest.main(verbosity=2)
