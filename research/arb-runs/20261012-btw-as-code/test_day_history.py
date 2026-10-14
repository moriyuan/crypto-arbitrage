import unittest
from decimal import Decimal as D
from day_history import parse_rows, independent, HourlyBar, HistoryGate, RollingHistoryConfig, HOUR, DAY

class HistoryTests(unittest.TestCase):
    def test_range_and_duplicate_conflict(self):
        rows=[[str(t),'1','1','1',str(c),'1','1'] for t,c in [(0,1),(HOUR,2),(2*HOUR,3)]]
        p={'http_status':200,'response':{'code':'00000','data':rows}}
        self.assertEqual(list(parse_rows(p,'bitget',HOUR,2*HOUR)),[HOUR])
        rows.append([str(HOUR),'1','1','1','4','1','1'])
        with self.assertRaises(ValueError):parse_rows(p,'bitget',HOUR,2*HOUR)
    def test_missing_not_filled_and_publication_delay(self):
        end=40*DAY
        bars=[HourlyBar(t,D(1)+D(i)/10000,D(100)) for i,t in enumerate(range(end-20*DAY-HOUR,end,HOUR))]
        data={'BTWUSDT':{'bybit':bars,'bitget':bars}}
        gate=HistoryGate(RollingHistoryConfig(),data,identity_verified_symbols=['BTWUSDT'])
        self.assertFalse(gate.evaluate('BTWUSDT',end+59999)['history_filter_passed'])
        self.assertTrue(gate.evaluate('BTWUSDT',end+60000)['history_filter_passed'])
        self.assertEqual(independent(data,end)[1],{'BTWUSDT':1})
        data['BTWUSDT']['bitget']=bars[:-1]
        self.assertEqual(independent(data,end)[1],{})
if __name__=='__main__':unittest.main()
