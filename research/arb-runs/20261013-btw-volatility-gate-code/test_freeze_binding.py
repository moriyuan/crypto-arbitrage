import copy,json,tempfile,unittest
from pathlib import Path
from freeze_selection import validate_feature_acceptance
from run_day import sha
class BindingTests(unittest.TestCase):
 def fixture(self,path):
  (path/'protocol.json').write_text('{}');(path/'input-manifest.json').write_text('{}')
  audit=path/'audit.json';audit.write_text('{}')
  manifest={'days':{'A':{'files':{'entry_gates':{'sha256':'gate'},'tape':{'sha256':'tape'}},'feature_evidence':{'feature_npz':{'sha256':'feature'}}}}}
  acceptance={'status':'PASS','execution_enabled':False,'protocol_sha256':sha(path/'protocol.json'),'input_manifest_sha256':sha(path/'input-manifest.json'),'days':{'A':{'status':'PASS','gate_file_sha256':'gate','feature_file_sha256':'feature','tape_sha256':'tape'}},'source_sha256':{str(audit):sha(audit)}}
  return manifest,acceptance,audit
 def test_accept(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);m,a,_=self.fixture(p);validate_feature_acceptance(p,m,a)
 def test_stale_binding(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);m,a,_=self.fixture(p)
   for key in ['protocol_sha256','input_manifest_sha256']:
    b=copy.deepcopy(a);b[key]='old'
    with self.assertRaises(ValueError):validate_feature_acceptance(p,m,b)
   for key in ['gate_file_sha256','feature_file_sha256','tape_sha256']:
    b=copy.deepcopy(a);b['days']['A'][key]='old'
    with self.assertRaises(ValueError):validate_feature_acceptance(p,m,b)
 def test_changed_audit(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);m,a,f=self.fixture(p);f.write_text('changed')
   with self.assertRaises(ValueError):validate_feature_acceptance(p,m,a)
if __name__=='__main__':unittest.main()
