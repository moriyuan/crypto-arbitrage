import unittest
from audit_activity_experiment import higher_index, validate_transport

class AuditBoundaries(unittest.TestCase):
    def test_higher_index(self):
        self.assertEqual([higher_index(8,q) for q in (25,50,75)],[2,4,6])
        self.assertEqual(higher_index(1,75),0)
    def test_empty_rejected(self):
        with self.assertRaises(AssertionError):higher_index(0,25)
    def test_invalid_quantile_rejected(self):
        with self.assertRaises(AssertionError):higher_index(3,10)
    def test_transport_alias_exact(self):
        row={'name':'q25','countA':'3','countB':'4','turnoverAunits':'5','turnoverBunits':'6','maxAgeNs':'5000000000'}
        validate_transport([dict(row,name='q10'),row],[3,4,5,6])
        for key in ('countA','maxAgeNs','name'):
            mutated=dict(row);mutated[key]='1'
            with self.assertRaises(AssertionError):validate_transport([dict(row,name='q10'),mutated],[3,4,5,6])
    def test_duplicate_transport_rejected(self):
        row={'name':'q25','countA':'3','countB':'4','turnoverAunits':'5','turnoverBunits':'6','maxAgeNs':'5000000000'}
        with self.assertRaises(AssertionError):validate_transport([row,row],[3,4,5,6])


class LaunchBindingTests(unittest.TestCase):
    def setUp(self):
        import tempfile, json
        from pathlib import Path
        from audit_activity_experiment import sha
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.variant=self.root/'variants/q50';self.variant.mkdir(parents=True)
        self.day='2026-08-01';self.directory=self.variant/'search'/self.day;self.directory.mkdir(parents=True)
        self.dump=lambda p,v:p.write_text(json.dumps(v))
        for p in (self.variant/'protocol.json',self.root/'fixed-policy.csv',self.directory/'aggregate.json',self.root/'perfect_taker'):
            p.write_text('fixed')
        (self.variant/'policies.csv').write_text('fixed')
        engine={str(self.root/'perfect_taker'):sha(self.root/'perfect_taker')}
        self.dump(self.root/'prepared.json',{'execution_code_sha256':engine})
        files={k:{'path':str(self.root/k),'sha256':'datahash'} for k in ('tape','activity','thresholds','funding_csv')}
        spec={'files':files,'entry_start_ns':1}
        self.dump(self.variant/'input-manifest.json',{'days':{self.day:spec},'actual_activity_quantile':50,'protocol_sha256':sha(self.variant/'protocol.json')})
        self.launch={'day':self.day,'phase':'search','execution_enabled':False,'inputs':spec,'protocol_sha256':sha(self.variant/'protocol.json'),'policies_sha256':sha(self.variant/'policies.csv'),'code_sha256':engine,'started_utc':'2026-10-13T01:00:00+00:00'}
        self.launch['command']=[str(self.root/'perfect_taker'),'--tape',files['tape']['path'],'--activity',files['activity']['path'],'--thresholds',files['thresholds']['path'],'--funding-csv',files['funding_csv']['path'],'--policies',str(self.variant/'policies.csv'),'--entry-start-ns','1','--output',str(self.directory)]
        self.status={'status':'COMPLETED_PENDING_AUDIT','aggregate_sha256':sha(self.directory/'aggregate.json'),'completed_utc':'2026-10-13T01:00:01+00:00'}
        self.flush()
    def flush(self):
        self.dump(self.directory.with_suffix('.launch.json'),self.launch);self.dump(self.directory.with_suffix('.status.json'),self.status)
    def validate(self):
        from audit_activity_experiment import validate_launch
        return validate_launch(self.root,self.variant,self.day,'search')
    def test_exact_launch_accepted(self):self.assertEqual(self.validate()['actual_activity_quantile'],50)
    def test_wrong_threshold_command_rejected(self):
        self.launch['command'][6]='wrong_threshold';self.flush()
        with self.assertRaisesRegex(AssertionError,'exact launched command'):self.validate()
    def test_changed_result_rejected(self):
        (self.directory/'aggregate.json').write_text('changed')
        with self.assertRaisesRegex(AssertionError,'completed current result'):self.validate()
    def test_changed_engine_rejected(self):
        (self.root/'perfect_taker').write_text('changed')
        with self.assertRaisesRegex(AssertionError,'unchanged execution engine'):self.validate()
    def test_wrong_launch_inputs_rejected(self):
        self.launch['inputs']['entry_start_ns']=2;self.flush()
        with self.assertRaisesRegex(AssertionError,'launch exact variant inputs'):self.validate()
    def test_chronology_rejected(self):
        self.status['completed_utc']='2026-10-13T00:59:59+00:00';self.flush()
        with self.assertRaisesRegex(AssertionError,'completion chronology'):self.validate()

if __name__=='__main__':unittest.main()
