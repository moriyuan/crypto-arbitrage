"""Offline synthetic contract tests; no historical replay or external services."""
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

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
OLD = HERE.parent / '20261012-btw-exit-optimization-code'
spec = importlib.util.spec_from_file_location('old_screen_fixtures', OLD / 'test_screen.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
quote, tape, BASE = fixtures.quote, fixtures.tape, fixtures.BASE
ACT = struct.Struct('<qIIqqqqQ')
HEADER = 'name,countA,countB,turnoverAunits,turnoverBunits,maxAgeNs\n'
THRESHOLDS = HEADER + 'q10,1,2,100,200,1000000000\nq25,3,4,300,400,1000000000\n'


def active(t, **changes):
    row = dict(ns=t['ns'], countA=4, countB=4, turnoverA=400,
               turnoverB=400, ageA=0, ageB=0, mask=3)
    row.update(changes)
    return tuple(row[k] for k in ('ns','countA','countB','turnoverA','turnoverB','ageA','ageB','mask'))


class ActivityReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix='btw-activity-build-')
        cls.binary = Path(cls.build.name) / 'activity_replay'
        cls.old_binary = Path(cls.build.name) / 'old_screen'
        compiler = shutil.which('clang++') or shutil.which('g++')
        if not compiler:
            raise RuntimeError('C++17 compiler unavailable')
        for src, binary in ((HERE/'activity_replay.cpp', cls.binary), (OLD/'screen.cpp', cls.old_binary)):
            subprocess.run([compiler, '-std=c++17', '-O2', '-Wall', '-Wextra', str(src), '-o', str(binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def run_case(self, ticks, rows=None, thresholds=THRESHOLDS, extra=b'', magic=b'BTWACT01', fail=None):
        temp = tempfile.TemporaryDirectory(prefix='btw-activity-case-')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        tape(root/'tape.bin', ticks)
        if rows is None:
            rows = [active(t) for t in ticks]
        (root/'activity.bin').write_bytes(magic + b''.join(ACT.pack(*row) for row in rows) + extra)
        (root/'thresholds.csv').write_text(thresholds)
        args = [str(self.binary), '--tape', str(root/'tape.bin'), '--activity', str(root/'activity.bin'),
                '--thresholds', str(root/'thresholds.csv'), '--output', str(root/'out')]
        proc = subprocess.run(args, capture_output=True, text=True)
        if fail:
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn(fail, proc.stderr)
            self.assertFalse((root/'out'/'aggregate.json').exists())
            return root, args
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads((root/'out'/'aggregate.json').read_text())
        return root, {s['name']:s for s in summary['scenarios']}

    def rows(self, root, scenario, filename):
        with (root/'out'/scenario/filename).open() as f:
            return list(csv.DictReader(f))

    def test_gateoff_exact_old_selected_parity(self):
        rng = random.Random(7391)
        ticks = []
        for i in range(450):
            mid = 100000 + rng.randint(-500,500)
            gap, spread = rng.choice([100,250,300,800]), rng.choice([1,10,100,1000])
            prices=(mid,mid-spread,mid+gap+spread,mid+gap)
            if (i//7)%2:
                prices=prices[2:]+prices[:2]
            ticks.append(quote(i*1700, prices, qty=[rng.choice([40,80,160,400,2000]) for _ in range(4)],
                valid=i%43!=0, force=2 if i==449 else (1 if i%89==0 else 0), history=i%31!=0))
        # gateoff is independent of all activity flags/thresholds, while stream validation remains mandatory.
        root, new = self.run_case(ticks, [active(t,countA=0,countB=0,turnoverA=0,turnoverB=0,ageA=-1,ageB=-1,mask=0) for t in ticks])
        subprocess.run([str(self.old_binary),str(root/'tape.bin'),str(root/'old')],check=True,capture_output=True)
        old=json.loads((root/'old'/'aggregate.json').read_text())
        selected=next(s for s in old['scenarios'] if s['name']=='entry_deterioration_tp5_sl20')
        for key,value in selected.items():
            if key!='name':
                self.assertEqual(new['gateoff'][key],value,key)
        self.assertGreater(new['gateoff']['orders'],0)
        for filename in ('orders.csv','cycles.csv'):
            self.assertEqual((root/'out'/'gateoff'/filename).read_bytes(),
                             (root/'old'/'entry_deterioration_tp5_sl20'/filename).read_bytes())
        self.assertEqual(new['q10']['orders'],0)
        self.assertEqual(new['q25']['orders'],0)

    def test_active_gateoff_q10_q25_are_identical(self):
        ticks=[quote(0),quote(100,(100201,100200,100001,100000)),
               quote(30200,qty=160),quote(30300,(99801,99800,100501,100500),qty=160)]
        root,s=self.run_case(ticks)
        for name in ('q10','q25'):
            for filename in ('orders.csv','cycles.csv','entry_budgets.jsonl'):
                self.assertEqual((root/'out'/name/filename).read_bytes(),
                                 (root/'out'/'gateoff'/filename).read_bytes())
            self.assertEqual(s[name]['activity_rejected_entry_decisions'],0)

    def test_inactive_cannot_block_tp_hard_timeout_or_dayend(self):
        for second,reason in [(quote(100,(100201,100200,100001,100000)),'take_profit'),
            (quote(100,(90001,90000,110301,110300)),'hard_stop_loss'),
            (quote(60000),'holding_timeout'),(quote(100,force=2),'replay_day_end')]:
            with self.subTest(reason=reason):
                ticks=[quote(0),second]
                root,s=self.run_case(ticks,[active(ticks[0]),active(second,mask=0)])
                for name in s:
                    self.assertEqual(self.rows(root,name,'cycles.csv')[0]['exit_reason'],reason)
                    self.assertEqual(s[name]['orders'],4)

    def test_history_and_future_activity_preserve_causality(self):
        ticks=[quote(0,history=False),quote(100),quote(200)]
        root1,_=self.run_case(ticks)
        root2,_=self.run_case(ticks,[active(ticks[0]),active(ticks[1]),active(ticks[2],mask=0)])
        for name in ('gateoff','q10','q25'):
            orders=self.rows(root1,name,'orders.csv')
            self.assertEqual(len(orders),2)
            self.assertEqual(orders[0]['ns'],str(ticks[1]['ns']))
            self.assertEqual(orders,self.rows(root2,name,'orders.csv'))

    def test_gate_exact_inclusive_threshold_and_two_venues(self):
        t=quote(0)
        root,s=self.run_case([t],[active(t,countA=1,countB=2,turnoverA=100,turnoverB=200,ageA=1000000000,ageB=1000000000)])
        self.assertEqual(s['q10']['orders'],2)
        self.assertEqual(s['q25']['orders'],0)
        for field,value,reason in [('countA',0,'bybit_count'),('countB',1,'bitget_count'),
              ('turnoverA',99,'bybit_turnover'),('turnoverB',199,'bitget_turnover'),
              ('ageA',1000000001,'bybit_age'),('ageB',-1,'bitget_age'),('mask',1,'bitget_warm_valid')]:
            with self.subTest(field=field):
                changes=dict(countA=1,countB=2,turnoverA=100,turnoverB=200,ageA=0,ageB=0,mask=3)
                changes[field]=value
                _,s=self.run_case([t],[active(t,**changes)])
                self.assertEqual(s['q10']['orders'],0)
                self.assertEqual(s['q10']['activity_entry_rejection_reasons'][reason],1)
                self.assertEqual(s['q10']['activity_rejected_entry_directions'],1)

    def test_activity_never_blocks_risk_exit(self):
        ticks=[quote(0),quote(100,(99801,99800,100501,100500))]
        root,s=self.run_case(ticks,[active(ticks[0]),active(ticks[1],mask=0,ageA=-1,ageB=-1)])
        for name in s:
            self.assertEqual(s[name]['cycles'],1)
            self.assertEqual(self.rows(root,name,'cycles.csv')[0]['exit_reason'],'stop_loss')
            self.assertEqual(s[name]['activity_rejected_entry_decisions'],0)

    def test_inactive_partial_sticky_and_forced_exit(self):
        ticks=[quote(0),quote(100,(99801,99800,100501,100500),qty=40),
               quote(200,(99801,99800,100501,100500),qty=40),
               quote(300,(100201,100200,100001,100000),qty=40)]
        root,s=self.run_case(ticks,[active(ticks[0])]+[active(t,mask=0) for t in ticks[1:]])
        for name in s:
            cycle=self.rows(root,name,'cycles.csv')[0]
            self.assertEqual((s[name]['orders'],cycle['exit_parts'],cycle['exit_reason']),(6,'2','stop_loss'))
        ticks=[quote(0),quote(100,valid=False,force=1),quote(200)]
        root,s=self.run_case(ticks,[active(ticks[0])]+[active(t,mask=0) for t in ticks[1:]])
        for name in s:
            self.assertEqual(self.rows(root,name,'cycles.csv')[0]['exit_reason'],'funding_guard')

    def test_gate_rejection_does_not_consume_budget(self):
        ticks=[quote(0),quote(100)]
        root,s=self.run_case(ticks,[active(ticks[0],mask=0),active(ticks[1])])
        for name in ('q10','q25'):
            orders=self.rows(root,name,'orders.csv')
            self.assertEqual([r['qty'] for r in orders],['20','20'])
            self.assertEqual(orders[0]['ns'],str(ticks[1]['ns']))
            self.assertEqual(s[name]['activity_rejected_entry_decisions'],1)

    def test_snapshots_before_consumption_and_exact_far_price(self):
        from decimal import Decimal
        t=quote(0,extra={0:{Decimal('100001.4'):67}})
        root,s=self.run_case([t])
        for name in s:
            record=json.loads((root/'out'/name/'entry_budgets.jsonl').read_text())
            self.assertFalse(record['after_entry_consumption'])
            self.assertTrue(record['pre_entry_consumption'])
            self.assertEqual((record['entry_ns'],record['ns'],record['qty'],record['newqty']), (BASE,BASE,20,20))
            self.assertEqual(record['long'],'bybit')
            self.assertEqual(record['short'],'bitget')
            self.assertEqual(record['prices'],list(t['prices']))
            self.assertEqual(record['displayed'],list(t['sizes']))
            self.assertEqual(record['entry_prices'],dict(bybit=100000,bitget=100300))
            for side,price in enumerate(t['prices']):
                item=next(i for i in record['sides'][side] if i['price_units_1e8']==price*1000)
                self.assertEqual(item['remaining_quarter'],80)
            self.assertIn(100001400,[i['price_units_1e8'] for i in record['sides'][0]])

    def test_activity_file_alignment_and_invalid_rows(self):
        t=quote(0)
        for rows,extra,magic,error in [([],b'',b'BTWACT01','activity ended before tape'),
             ([active(t)],b'\0',b'BTWACT01','truncated activity row'),
             ([active(t),active(quote(1))],b'',b'BTWACT01','activity has extra rows'),
             ([active(t,ns=t['ns']+1)],b'',b'BTWACT01','activity/tape timestamp mismatch'),
             ([active(t)],b'',b'WRONG001','invalid activity magic'),
             ([active(t,turnoverA=-1)],b'',b'BTWACT01','invalid activity row'),
             ([active(t,ageA=-2)],b'',b'BTWACT01','invalid activity row'),
             ([active(t,mask=4)],b'',b'BTWACT01','invalid activity row')]:
            with self.subTest(error=error):
                self.run_case([t],rows,extra=extra,magic=magic,fail=error)

    def test_threshold_validation_and_exclusive_output(self):
        for thresholds in [HEADER, THRESHOLDS+'q10,1,1,1,1,1\n', THRESHOLDS.replace('q25','unexpected'),
             THRESHOLDS.replace('q10,1','q10,-1'),THRESHOLDS.replace('q10,1','q10,4294967296'),
             THRESHOLDS.replace('q10,1','q10,1x'),THRESHOLDS.replace('maxAgeNs','max_age_ns')]:
            with self.subTest(thresholds=thresholds):
                self.run_case([quote(0)],thresholds=thresholds,fail='threshold')
        root,_=self.run_case([quote(0)])
        original=(root/'out'/'aggregate.json').read_bytes()
        proc=subprocess.run([str(self.binary),'--tape',str(root/'tape.bin'),'--activity',str(root/'activity.bin'),
            '--thresholds',str(root/'thresholds.csv'),'--output',str(root/'out')],capture_output=True,text=True)
        self.assertNotEqual(proc.returncode,0)
        self.assertIn('output already exists',proc.stderr)
        self.assertEqual((root/'out'/'aggregate.json').read_bytes(),original)

if __name__=='__main__':
    unittest.main()
