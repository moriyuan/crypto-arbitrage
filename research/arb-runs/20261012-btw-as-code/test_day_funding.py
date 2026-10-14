import gzip,json,unittest
from day_funding import ns,parse_events,bracket,schedule_rows

class FundingTests(unittest.TestCase):
    def test_exact_receipt_clock(self):
        self.assertEqual(ns('2026-09-01T00:00:00.123456789Z')%10**9,123456789)
    def test_requires_both_brackets(self):
        rows=[{'symbol':'BTWUSDT','fundingTime':str(t),'fundingRate':'0.0001'} for t in (10,20,30)]
        events=parse_events({'code':'00000','data':rows},'bitget')
        self.assertEqual(bracket(events,15,25)['target_day_event_count'],1)
        with self.assertRaises(ValueError):bracket(events[1:],15,25)
    def test_expired_announcement_not_available(self):
        stamp=ns('2026-09-01T00:00:01.000000001Z')
        row={'arg':{'topic':'ticker','symbol':'BTWUSDT'},'data':[{'nextFundingTime':str(stamp//10**6)}]}
        raw=gzip.compress(('2026-09-01T00:00:01.000000001Z '+json.dumps(row)+'\n').encode())
        self.assertEqual(schedule_rows(raw,'bitget','x','2026-09-01')[0],[])
if __name__=='__main__':unittest.main()
