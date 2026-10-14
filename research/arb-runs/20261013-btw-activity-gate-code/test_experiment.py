import unittest
from experiment import select_gate


def row(q, day, net, cycles=10, dd='1', closed=0):
    return dict(quantile=q, day=day, net=str(net), cycles=cycles,
                drawdown=dd, unresolved_qty=closed, audit='PASS',
                funding_status='VERIFIED_NO_SETTLEMENT')


def candidates():
    return [row(q,d,n) for q,n in [(25,-2),(50,-1),(75,1)] for d in ('A','B')]


class SelectionTests(unittest.TestCase):
    def test_best_worst_day(self):
        self.assertEqual(select_gate(candidates(), ['A','B'])['selected_quantile'],75)

    def test_sparse_gate_not_promoted(self):
        rows=candidates()
        for r in rows:
            if r['quantile']==75:r['cycles']=9
        self.assertEqual(select_gate(rows,['A','B'])['selected_quantile'],50)

    def test_no_eligible_improvement_keeps_baseline(self):
        rows=candidates()
        for r in rows:
            if r['quantile']!=25:r['cycles']=0
        result=select_gate(rows,['A','B'])
        self.assertEqual(result['selected_quantile'],25)
        self.assertFalse(result['promoted'])

    def test_worst_day_alone_not_enough(self):
        rows=[row(25,'A',-2),row(25,'B',10),row(50,'A',-1),row(50,'B',-1),row(75,'A',-3),row(75,'B',20)]
        self.assertEqual(select_gate(rows,['A','B'])['selected_quantile'],25)

    def test_tie_prefers_lower_quantile(self):
        rows=candidates()
        for r in rows:
            if r['quantile']>=50:r['net']='1'
        self.assertEqual(select_gate(rows,['A','B'])['selected_quantile'],50)

    def test_evaluation_contamination_rejected(self):
        with self.assertRaises(ValueError):select_gate(candidates()+[row(75,'C',999)],['A','B'])

    def test_residual_and_audit_failure_rejected(self):
        for field,value in [('unresolved_qty',10),('audit','FAIL'),('funding_status','UNKNOWN')]:
            rows=candidates();rows[0][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):select_gate(rows,['A','B'])

    def test_duplicate_and_missing_rejected(self):
        for rows in [candidates()+[candidates()[0]],candidates()[:-1]]:
            with self.assertRaises(ValueError):select_gate(rows,['A','B'])


if __name__=='__main__':unittest.main()
