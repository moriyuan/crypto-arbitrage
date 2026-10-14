"""Sequential bounded public archive acquisition followed by independent audit."""
import argparse,json,os,sys,time
from pathlib import Path
from download_public_days import collect,write,sha,ALLOWED_DATES
from audit_public_day import audit

p=argparse.ArgumentParser();p.add_argument('--output-root',type=Path,required=True);p.add_argument('--dates',nargs='+',choices=ALLOWED_DATES,default=list(ALLOWED_DATES));a=p.parse_args();root=a.output_root.resolve();code=Path(__file__).resolve().parent
launch={'pid':os.getpid(),'started_epoch':time.time(),'argv':sys.argv,'dates':a.dates,'dates_preselected_by_availability':True,'execution_enabled':False,'private_credentials_used':False,'code_sha256':{name:sha(code/name) for name in ('collect_and_audit_days.py','download_public_days.py','audit_public_day.py')}}
if (root/'raw-collection.launch.json').exists():raise FileExistsError('existing collection launch')
write(root/'raw-collection.launch.json',launch)
status={'state':'RUNNING','completed':[],'execution_enabled':False}
def update():status['heartbeat_epoch']=time.time();write(root/'raw-collection-status.json',status)
update()
try:
 for date in a.dates:
  status.update(date=date,phase='DOWNLOAD');update();collect(root,date,'download')
  status['phase']='AUDIT';update();audit(root/'days'/date/'archive')
  status['completed'].append(date);update()
 status.update(state='COMPLETED',phase='DONE');update()
except BaseException as e:
 status.update(state='FAILED',error=type(e).__name__+': '+str(e));update();raise
