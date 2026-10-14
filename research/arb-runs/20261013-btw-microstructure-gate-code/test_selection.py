import copy
import unittest
from selection import choose
DAYS=['A','B']
POLICIES=[{'id':'baseline','role':'baseline'}, {'id':'book_coverage','role':'coverage_control'}, {'id':'trade_coverage','role':'coverage_control'}, {'id':'flow_coverage','role':'coverage_control'}, {'id':'candidate','role':'candidate','control_id':'book_coverage'}]
def row(name,nets,n=10):
    return {'id':name,'days':{d:{'net':str(x),'drawdown':'1','cycles':n,'unresolved_qty':0,'funding_status':'VERIFIED_NO_SETTLEMENT','accounting_pass':True} for d,x in zip(DAYS,nets)}}
def source():
    return [row('baseline',[-10,1]), row('book_coverage',[-9,1]), row('trade_coverage',[-9,1]),row('flow_coverage',[-9,1]),row('candidate',[-8,2])]
class SelectionTests(unittest.TestCase):
    def test_promotes_only_candidate(self):
        best, scored=choose(source(),DAYS,POLICIES)
        self.assertEqual(best['id'],'candidate')
    def test_missing_data_small_sample_rejected(self):
        rows=source();rows[-1]=row('candidate',[0,3],n=9)
        best,scored=choose(rows,DAYS,POLICIES);self.assertIsNone(best)
        self.assertIn('minimum_cycles_each_fit_day',scored[-1]['rejection_reasons'])
    def test_must_beat_coverage_control(self):
        rows=source();rows[1]=row('book_coverage',[-7,2])
        self.assertIsNone(choose(rows,DAYS,POLICIES)[0])
    def test_worst_and_total_must_both_improve_strictly(self):
        for nets in ([-10,10],[-8,-1]):
            rows=source();rows[-1]=row('candidate',nets)
            self.assertIsNone(choose(rows,DAYS,POLICIES)[0])
    def test_failed_audit_or_unresolved_rejected(self):
        for key,value in [('accounting_pass',False),('unresolved_qty',10),('funding_status','UNKNOWN')]:
            rows=source();rows[-1]['days']['B'][key]=value
            self.assertIsNone(choose(rows,DAYS,POLICIES)[0])
    def test_forbids_evaluation_or_duplicate_input(self):
        rows=source();rows[-1]['days']['C']=copy.deepcopy(rows[-1]['days']['A'])
        with self.assertRaises(ValueError):choose(rows,DAYS,POLICIES)
        with self.assertRaises(ValueError):choose(source()+[source()[-1]],DAYS,POLICIES)
    def test_invalid_explicit_control_rejected(self):
        for control in ('unknown','candidate'):
            policies=copy.deepcopy(POLICIES);policies[-1]['control_id']=control
            with self.assertRaises(ValueError):choose(source(),DAYS,policies)
    def test_nonfinite_fails_closed(self):
        rows=source();rows[-1]['days']['A']['net']='NaN'
        with self.assertRaises(ValueError):choose(rows,DAYS,POLICIES)
if __name__=='__main__':unittest.main()
