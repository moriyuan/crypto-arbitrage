"""Final read-only binding/replay review, with independently recomputed selection."""
import datetime as dt
from decimal import Decimal as D
import json
from pathlib import Path
from audit_gate_features import sha,read,require

HERE=Path(__file__).resolve().parent

def review(root):
 p=read(root/'protocol.json');m=read(root/'input-manifest.json');frozen=read(root/'selection-frozen.json');feature=read(root/'feature-gate-acceptance.json')
 require(feature['status']=='PASS' and feature['protocol_sha256']==sha(root/'protocol.json') and feature['input_manifest_sha256']==sha(root/'input-manifest.json'),'feature acceptance bound')
 for path,digest in {**feature['source_sha256'],**frozen['source_sha256'],**m['execution_code_sha256'],**m['feature_code_sha256'],**frozen['code_sha256']}.items():require(sha(path)==digest,'frozen source hash '+path)
 require(frozen['protocol_sha256']==sha(root/'protocol.json') and frozen['input_manifest_sha256']==sha(root/'input-manifest.json') and frozen['policies_sha256']==sha(root/'policies.csv'),'selection configuration bound')
 require(frozen['execution_code_sha256']==m['execution_code_sha256'],'engine unchanged across phases')
 require(frozen['grid_scores_sha256']==sha(root/'grid-scores.json'),'frozen score hash')
 binary=next(path for path in m['execution_code_sha256'] if Path(path).name=='perfect_taker')
 policyids=[x['id'] for x in p['policies']];byid={x['id']:x for x in p['policies']};metrics={};accepted={};bind={};orig=Path(p['source_experiment'])
 for day,spec in m['days'].items():
  phase='search' if day in p['fit_dates'] else 'evaluation';folder=root/phase/day
  launch=read(folder.with_suffix('.launch.json'));status=read(folder.with_suffix('.status.json'));a=read(folder/'aggregate.json');light=read(folder/'perfect-audit.json');deep=read(folder/'perfect-deep-audit.json')
  require(status['status']=='COMPLETED_PENDING_AUDIT' and status['aggregate_sha256']==sha(folder/'aggregate.json'),'completed replay status bound')
  require(launch['day']==day and launch['phase']==phase and launch['execution_enabled'] is False and launch['inputs']==spec,'launch identity and exact inputs')
  require(launch['protocol_sha256']==sha(root/'protocol.json') and launch['policies_sha256']==sha(root/'policies.csv') and launch['input_manifest_sha256']==sha(root/'input-manifest.json'),'launch frozen config')
  for path,digest in launch['code_sha256'].items():require(sha(path)==digest,'launch actual code unchanged')
  files=spec['files'];command=[binary,'--tape',files['tape']['path'],'--activity',files['activity']['path'],'--thresholds',files['thresholds']['path'],'--funding-csv',files['funding_csv']['path'],'--policies',str(root/'policies.csv'),'--entry-start-ns',str(spec['entry_start_ns']),'--entry-gates',files['entry_gates']['path'],'--output',str(folder)]
  require(launch['command']==command,'exact native gate launch command')
  if phase=='search':require(dt.datetime.fromisoformat(status['completed_utc'])<dt.datetime.fromisoformat(frozen['frozen_utc']),'fit completed before selection')
  else:
   require(dt.datetime.fromisoformat(launch['started_utc'])>dt.datetime.fromisoformat(frozen['frozen_utc']),'evaluation after frozen selection')
   require(launch['selection_frozen_sha256']==sha(root/'selection-frozen.json'),'evaluation selection exact hash')
  require(a['entry_gate']['bit_order']==policyids and [x['id'] for x in a['scenarios']]==policyids,'exact15policy order')
  require(light['status']==deep['status']=='PASS' and set(deep['scenarios'])==set(policyids),'all policy accounting/deep acceptance')
  require(deep['light_audit_sha256']==sha(folder/'perfect-audit.json'),'deep bound to frozen light')
  require(deep['source_entry_exit_quotes_sha256']==sha(deep['source_entry_exit_quotes_path']),'native quote provenance')
  for audit in (light,deep):
   require(audit['aggregate_sha256']==sha(folder/'aggregate.json') and audit['launch_sha256']==sha(folder.with_suffix('.launch.json')) and audit['input_manifest_sha256']==sha(root/'input-manifest.json'),'independent audit binding')
  baseline={}
  for name in ('orders.csv','cycles.csv','equity.csv'):
   new=folder/'baseline'/name;old=orig/'variants/q25'/phase/day/'e30_tp20_sl10_h15'/name
   require(sha(new)==sha(old),'baseline exact original q25 ledger');baseline[name]=sha(new)
  accepted[day]={'status':'PASS','scenarios':len(policyids),'cycles':sum(x['cycles'] for x in a['scenarios']),'orders':sum(x['orders'] for x in a['scenarios']),'native_decisions':a['decisions'],'baseline_exact_prior_q25_csv_sha256':baseline,'launch_started_utc':launch['started_utc'],'completed_utc':status['completed_utc']}
  for x in a['scenarios']:
   audit=light['scenarios'][x['id']];require(audit['status']=='PASS' and audit['funding_status']=='VERIFIED_NO_SETTLEMENT' and x['unresolved_qty']==0,'accounting/funding/residual')
   for name,digest in audit['source_sha256'].items():require(sha(folder/x['id']/name)==digest,'audited CSV current')
   for key in ('entry_bps','tp_bps','sl_bps','hard_bps','hold_ms'):require(x[key]==byid[x['id']][key],'fixed core policy')
   if phase=='search':metrics.setdefault(x['id'],[]).append({'net':D(x['net_before_funding']),'dd':D(x['complete_mark_max_drawdown_usdt']),'cycles':x['cycles']})
  for file in ('aggregate.json','perfect-audit.json','perfect-deep-audit.json','perfect-deep-quotes.json'):bind[str(folder/file)]=sha(folder/file)
  bind[str(folder.with_suffix('.launch.json'))]=sha(folder.with_suffix('.launch.json'))
 scored={i:{'worst':min(x['net'] for x in rows),'total':sum(x['net'] for x in rows),'dd':sum(x['dd'] for x in rows),'minimum_cycles':min(x['cycles'] for x in rows)} for i,rows in metrics.items()}
 eligible=[]
 for policy in p['policies']:
  if policy['role']!='candidate':continue
  identifier=policy['id'];v=scored[identifier];base=scored['baseline'];control='joint_coverage' if policy['require_basis'] and policy['require_cooling'] else 'basis_coverage' if policy['require_basis'] else 'cooling_coverage'
  if v['minimum_cycles']>=p['selection']['minimum_cycles_each_fit_day'] and v['worst']>base['worst'] and v['total']>base['total'] and v['total']>scored[control]['total']:eligible.append(identifier)
 eligible.sort(key=lambda i:(-scored[i]['worst'],-scored[i]['total'],scored[i]['dd'],i));selected=eligible[0] if eligible else 'baseline'
 require(frozen['selected_id']==selected and frozen['eligible_candidates']==len(eligible),'independent frozen selection')
 result={'status':'PASS','execution_enabled':False,'reviewed_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'review_source_sha256':sha(__file__),'scope':'All45 day-policies including explicit missing-data coverage controls; new code only entry gate and diagnostics, original sizing/fee/cash/risk paths unchanged','resolved_findings':[{'issue':'feature acceptance previously only checked status','resolution':'Freeze now validates offline marker, protocol/manifest hashes, all dates gate/tape/feature hashes, and nonempty independently rehashed audit sources; three regression tests passed'}],'unresolved_findings':[],'days':accepted,'feature_gate_acceptance_sha256':sha(root/'feature-gate-acceptance.json'),'source_sha256':bind,'selection_independently_recomputed':{'selected_id':selected,'eligible_count':len(eligible),'frozen_utc':frozen['frozen_utc'],'rules':'Each fitday>=10cycles,strict improvements in worst/total vsbaseline andtotal vscorrespondingcoverage control; October excluded fromselection'},'limitations':['Zero-trade/missing-feature days are not profitability improvements; coverage controls separate abstention','Volatility-to-cost ratio is a fluctuation scale, not expected return or mean-reversion forecast','All dates previously researched; exploratory internal validation only','Idealzero-added-latency synchronous fullL1 exits may exceed visibledepth; no impact or partialfills modeled','Raw/native identity inherited via prior audit hashes; compressed frame reparse not repeated']}
 with (root/'independent-review.json').open('x') as f:json.dump(result,f,ensure_ascii=False,indent=2);f.write('\n')
 return result

if __name__=='__main__':
 import sys
 print(json.dumps(review(Path(sys.argv[1]).resolve())))
