"""Behavior contracts for optional aligned entry masks, without market replays."""
import csv
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

import test_perfect_taker as fixture

HERE = Path(__file__).resolve().parent
FROZEN = HERE.parent/'20261013-btw-perfect-taker-code'
ROW = struct.Struct('<qI')


class EntryGateContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix='entry-gates-build-')
        cls.binary = Path(cls.build.name)/'new'
        cls.old_binary = Path(cls.build.name)/'old'
        for source, output in ((HERE/'perfect_taker.cpp', cls.binary), (FROZEN/'perfect_taker.cpp', cls.old_binary)):
            subprocess.run(['clang++','-std=c++17','-O2','-Wall','-Wextra',str(source),'-o',str(output)], check=True, capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def run_case(self, ticks, masks=None, raw_gate=None, policies=None, activity=None, old=False, decimals=5, expect=None):
        tmp = tempfile.TemporaryDirectory(prefix='entry-gates-case-')
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        fixture.write_tape(folder/'tape.bin', ticks, decimals)
        activity = [fixture.active(t) for t in ticks] if activity is None else activity
        (folder/'activity.bin').write_bytes(b'BTWACT01'+b''.join(fixture.ACT.pack(*r) for r in activity))
        (folder/'thresholds.csv').write_text(fixture.THRESHOLDS)
        (folder/'funding.csv').write_text(fixture.funding_rows())
        (folder/'policies.csv').write_text(fixture.POLICY_HEADER+(policies or ','.join(map(str,fixture.DEFAULT_POLICY))+'\n'))
        command = [str(self.old_binary if old else self.binary),'--tape',str(folder/'tape.bin'),'--activity',str(folder/'activity.bin'),
                   '--thresholds',str(folder/'thresholds.csv'),'--funding-csv',str(folder/'funding.csv'),'--policies',str(folder/'policies.csv'),
                   '--entry-start-ns',str(fixture.BASE),'--output',str(folder/'out')]
        if masks is not None or raw_gate is not None:
            if raw_gate is None:
                raw_gate=b'BTWGATE1'+b''.join(ROW.pack(t['ns'],mask) for t,mask in zip(ticks,masks))
            (folder/'gates.bin').write_bytes(raw_gate)
            command += ['--entry-gates',str(folder/'gates.bin')]
        proc = subprocess.run(command,capture_output=True,text=True)
        if expect is not None:
            self.assertNotEqual(proc.returncode,0,proc.stdout)
            self.assertIn(expect,proc.stderr)
            self.assertFalse((folder/'out'/'aggregate.json').exists())
            return folder
        self.assertEqual(proc.returncode,0,proc.stderr)
        aggregate=json.loads((folder/'out'/'aggregate.json').read_text())
        return folder,aggregate

    def test_no_gate_exact_old_csv_for_both_price_schemas(self):
        for decimals in (5,6):
            factor=10**(decimals-5)
            ticks=[fixture.quote(0,prices=tuple(p*factor for p in (100000,99999,100301,100300))),
                   fixture.quote(60*fixture.SEC,prices=tuple(p*factor for p in (100001,100000,100300,100299))),
                   fixture.quote(90*fixture.SEC+1,prices=tuple(p*factor for p in (100000,99999,100301,100300))),
                   fixture.quote(150*fixture.SEC+1,prices=tuple(p*factor for p in (100001,100000,100300,100299)),force=2)]
            new,a=self.run_case(ticks,decimals=decimals)
            old,b=self.run_case(ticks,decimals=decimals,old=True)
            for name in ('orders.csv','cycles.csv','equity.csv'):
                self.assertEqual((new/'out/case'/name).read_bytes(),(old/'out/case'/name).read_bytes())
            for key,value in b['scenarios'][0].items():
                self.assertEqual(a['scenarios'][0][key],value,key)

    def test_false_gate_prevents_new_entry(self):
        _,result=self.run_case([fixture.quote(),fixture.quote(60*fixture.SEC,force=2)],masks=[0,0])
        self.assertEqual(result['scenarios'][0]['orders'],0)
        self.assertTrue(result['entry_gate']['enabled'])
        self.assertEqual(result['scenarios'][0]['base_activity_pass_decisions'],2)
        self.assertEqual(result['scenarios'][0]['additional_entry_gate_fail_decisions'],2)

    def test_false_gate_does_not_block_held_exit(self):
        _,result=self.run_case([fixture.quote(),fixture.quote(60*fixture.SEC)],masks=[1,0])
        scenario=result['scenarios'][0]
        self.assertEqual(scenario['orders'],4)
        self.assertEqual(scenario['unresolved_qty'],0)
        self.assertEqual(scenario['exit_reasons'],{'holding_timeout':1})

    def test_false_gate_does_not_block_risk_exit(self):
        # A 29.9bp worsening exceeds the fixed 20bp entry-deterioration stop.
        _,result=self.run_case([fixture.quote(),fixture.quote(fixture.SEC,prices=(99701,99700,100301,100300))],masks=[1,0])
        self.assertEqual(result['scenarios'][0]['cycles'],1)
        self.assertEqual(result['scenarios'][0]['exit_reasons'],{'stop_loss':1})

    def test_policy_bits_follow_csv_order_and_cannot_override_activity(self):
        ticks=[fixture.quote(),fixture.quote(60*fixture.SEC,force=2)]
        policies='first,21,5,20,100,60000\nsecond,21,5,20,100,60000\n'
        _,result=self.run_case(ticks,masks=[2,0],policies=policies)
        self.assertEqual(result['entry_gate']['bit_order'],['first','second'])
        self.assertEqual([s['orders'] for s in result['scenarios']],[0,4])
        _,denied=self.run_case(ticks,masks=[3,3],policies=policies,activity=[fixture.active(t,countA=0) for t in ticks])
        self.assertEqual([s['orders'] for s in denied['scenarios']],[0,0])

    def test_all_true_gate_exact_no_gate_csv(self):
        ticks=[fixture.quote(),fixture.quote(60*fixture.SEC,force=2)]
        gated,_=self.run_case(ticks,masks=[1,1])
        plain,_=self.run_case(ticks)
        for name in ('orders.csv','cycles.csv','equity.csv'):
            self.assertEqual((gated/'out/case'/name).read_bytes(),(plain/'out/case'/name).read_bytes())

    def test_timestamp_mismatch(self):
        ticks=[fixture.quote()]
        self.run_case(ticks,raw_gate=b'BTWGATE1'+ROW.pack(ticks[0]['ns']+1,1),expect='entry gate timestamp')

    def test_missing_extra_and_truncated_rows(self):
        ticks=[fixture.quote(),fixture.quote(fixture.SEC)]
        with self.subTest('missing'):
            self.run_case(ticks,raw_gate=b'BTWGATE1'+ROW.pack(ticks[0]['ns'],1),expect='entry gate ended')
        with self.subTest('extra'):
            self.run_case(ticks,raw_gate=b'BTWGATE1'+b''.join(ROW.pack(fixture.BASE+i*fixture.SEC,1) for i in range(3)),expect='entry gate has extra')
        with self.subTest('truncated'):
            self.run_case(ticks,raw_gate=b'BTWGATE1'+ROW.pack(ticks[0]['ns'],1)+b'\x01',expect='truncated entry gate')

    def test_magic_high_bits_and_nonincreasing_clock(self):
        ticks=[fixture.quote(),fixture.quote(fixture.SEC)]
        with self.subTest('magic'):
            self.run_case(ticks,raw_gate=b'BTWGATE0',expect='entry gate magic')
        with self.subTest('highbit'):
            self.run_case(ticks,masks=[2,2],expect='entry gate unknown bits')
        with self.subTest('duplicate'):
            self.run_case(ticks,raw_gate=b'BTWGATE1'+ROW.pack(ticks[0]['ns'],1)*2,expect='entry gate clock')

    def test_32nd_bit_is_supported_but_33_policies_rejected_with_gate(self):
        ticks=[fixture.quote(),fixture.quote(60*fixture.SEC,force=2)]
        policies=''.join(f'p{i},21,5,20,100,60000\n' for i in range(32))
        _,result=self.run_case(ticks,masks=[1<<31,0],policies=policies)
        self.assertEqual(sum(s['orders'] for s in result['scenarios']),4)
        self.assertEqual(result['scenarios'][31]['orders'],4)
        self.run_case(ticks,masks=[0,0],policies=policies+'p32,21,5,20,100,60000\n',expect='entry gates support at most 32')

    def test_no_gate_retains_192_policy_capacity(self):
        policies=''.join(f'p{i},21,5,20,100,60000\n' for i in range(192))
        _,result=self.run_case([fixture.quote(force=2)],policies=policies)
        self.assertEqual(len(result['scenarios']),192)


if __name__=='__main__':
    unittest.main()
