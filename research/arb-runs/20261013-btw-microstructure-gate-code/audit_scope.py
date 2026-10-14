"""Independent user-scope and effective-result audit; never reruns or edits replay."""
import csv,datetime as dt,json,sys
from decimal import Decimal as D
from pathlib import Path
from microstructure_oracle import read,sha,require

def audit(root):
 p=read(root/'protocol.json');a=read(root/'scope-amendment.json');prior=read(root/'independent-review.json');scores=read(root/'grid-scores.json');frozen=read(root/'selection-frozen.json')
 active=[x for x in p['policies'] if x['min_spread_persistence_ms']==0];excluded=[x['id'] for x in p['policies'] if x['min_spread_persistence_ms']>0];ids=[x['id'] for x in active];byid={x['id']:x for x in active}
 require(len(ids)==10 and len(excluded)==4 and len([x for x in active if x['role']=='candidate'])==6,'scope sizes')
 require(a['active_policy_ids']==ids and a['excluded_policy_ids']==excluded,'numeric scope partition')
 require(a['spread_persistence_entry_filter_enabled'] is False and a['volatility_entry_filters_enabled'] is False and a['execution_enabled'] is False,'disabled filters and offline')
 require(a['applied_after_replay_results_available'] is True,'post-result change acknowledged')
 for path,digest in {**a['source_sha256'],**a['code_sha256'],**prior['source_sha256']}.items():require(sha(path)==digest,'frozen evidence unchanged '+path)
 rows={x['id']:x for x in scores if x['id'] in ids};scored={}
 require(set(rows)==set(ids),'original fit score identities')
 for i,r in rows.items():
  require(set(r['days'])==set(p['fit_dates']),'fit dates only')
  ds=list(r['days'].values());net=[D(x['net']) for x in ds]
  scored[i]={'worst':min(net),'sum':sum(net),'dd':sum(D(x['drawdown']) for x in ds),'eligible':False}
 eligible=[]
 for i,r in rows.items():
  policy=byid[i]
  if policy['role']!='candidate':continue
  s=scored[i];control=scored[policy['control_id']];ds=list(r['days'].values())
  ok=all(x['cycles']>=10 and x['unresolved_qty']==0 and x['funding_status']=='VERIFIED_NO_SETTLEMENT' and x['accounting_pass'] is True for x in ds) and s['worst']>scored['baseline']['worst'] and s['sum']>scored['baseline']['sum'] and s['sum']>control['sum']
  scored[i]['eligible']=ok
  if ok:eligible.append(i)
 eligible.sort(key=lambda i:(-scored[i]['worst'],-scored[i]['sum'],scored[i]['dd'],i));selected=eligible[0] if eligible else 'baseline'
 require(a['selected_id']==selected==frozen['selected_id']=='baseline' and a['eligible_candidates']==len(eligible)==0,'independent unchanged selection')
 require(len(a['scored_active_policies'])==10,'active scores length')
 for r in a['scored_active_policies']:
  i=r['id'];require(r['days']==rows[i]['days'],'amended fit values unchanged')
  require(r['eligible']==scored[i]['eligible'] and D(r['sum_net'])==scored[i]['sum'] and D(r['worst_date_net'])==scored[i]['worst'],'amended score arithmetic')
 comparison=read(root/'comparison.json');attribution=read(root/'attribution.json')
 for result in (comparison,attribution):
  require(result['spread_persistence_entry_filter_enabled'] is False and result['volatility_entry_filters_enabled'] is False and result['scope_amendment_after_results_available'] is True,'output effective scope')
  require(result['selected_id']==selected and result['excluded_archived_policy_ids']==excluded,'output exclusions/selection')
  for path,digest in {**result['source_sha256'],**result['code_sha256']}.items():require(sha(path)==digest,'effective summary source binding')
 require([x['id'] for x in comparison['policies']]==ids and len(comparison['rows'])==30,'30 day-policy rows')
 require(set(attribution['by_policy'])==set(ids) and len(attribution['cycles'])==1441 and attribution['identity_verified_cycles']==1441,'effective attribution cycles')
 require(all(x['policy_id'] in ids for x in attribution['cycles']),'no persistence cycle in effective attribution')
 expected_totals={};seen=set();source_hashes={};total_orders=0
 for day in p['fit_dates']+[p['evaluation_date']]:
  phase='search' if day in p['fit_dates'] else 'evaluation';folder=root/phase/day
  status=read(folder.with_suffix('.status.json'))
  require(dt.datetime.fromisoformat(a['created_utc'])>dt.datetime.fromisoformat(status['completed_utc']),'amendment actual post-result chronology')
  agg={x['id']:x for x in read(folder/'aggregate.json')['scenarios']}
  for control in ('book_coverage','trade_coverage','flow_coverage'):
   for name in ('orders.csv','cycles.csv','equity.csv'):require(sha(folder/control/name)==sha(folder/'baseline'/name),'coverage-only original account path unchanged')
  require(set(comparison['entry_admission'][day])==set(ids),'effective entry admission retains baseline self-control and excludes persistence')
  expected_totals[day]={'cycles':sum(agg[i]['cycles'] for i in ids),'orders':sum(agg[i]['orders'] for i in ids)};total_orders+=expected_totals[day]['orders']
  for r in [x for x in comparison['rows'] if x['date']==day]:
   i=r['policy_id'];require(i in ids and (day,i) not in seen,'effective scenario identity');seen.add((day,i));s=agg[i]
   require(r['cycles']==s['cycles'] and r['orders']==s['orders'] and D(r['net_before_funding_usdt'])==D(s['net_before_funding']),'effective metrics equal original ledger')
   records=[x for x in attribution['cycles'] if x['date']==day and x['policy_id']==i]
   require(len(records)==s['cycles'] and sum(D(x['net_before_funding_usdt']) for x in records)==D(s['net_before_funding']),'effective cycle totals')
   for x in records:require(D(x['mid_basis_convergence_usdt'])-D(x['bid_ask_crossing_cost_usdt'])-D(x['fees_usdt'])==D(x['net_before_funding_usdt']),'cycle exact economic identity')
  source_hashes[str(folder/'aggregate.json')]=sha(folder/'aggregate.json')
 require(len(seen)==30 and total_orders==5764,'effective scope totals')
 with (root/'comparison.csv').open(newline='') as f:csvrows=list(csv.DictReader(f))
 require(len(csvrows)==30 and {(x['date'],x['policy_id']) for x in csvrows}==seen,'CSV scope')
 report=(root/'report.md').read_text();require(all('|'+i+'|' not in report for i in excluded),'report no persistence result rows')
 require('更正在结果已经可见之后' in report and '价差持续时间不作为入场条件' in report,'report chronology and disabled filter explanation')
 enrichment=read(root/'report-enrichment.json');summary_log=read(root/'summarize.log')
 require(enrichment['final_report_sha256']==sha(root/'report.md'),'final report enrichment hash')
 require(enrichment['automatically_generated_report_sha256']==summary_log['outputs'][str(root/'report.md')],'automatic report evidence retained')
 for path,digest in enrichment['source_sha256'].items():require(sha(path)==digest,'report enrichment evidence')
 for name in ('scope-amendment.json','independent-review.json','comparison.json','comparison.csv','attribution.json','report.md','report-enrichment.json','scope-amendment-audit-attempt1.json'):source_hashes[str(root/name)]=sha(root/name)
 out={'status':'PASS','execution_enabled':False,'audited_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'auditor_sha256':sha(__file__),'active_policy_ids':ids,'excluded_policy_ids':excluded,'effective_day_scenarios':30,'effective_cycles':1441,'effective_orders':5764,'days':expected_totals,'independent_selected_id':selected,'eligible_candidates':len(eligible),'original42_scenario_evidence_unchanged':True,'post_result_user_scope_change_explicit':True,'source_sha256':source_hashes,'unresolved_findings':[],'auditor_attempt_resolution':'Attempt1 incorrectly excluded baseline self-control from admission schema; inspected actual schema and corrected expected keys to all10 active policies. Failure evidence preserved, no replay output changed.','limits':['This is a user-directed scope correction made after results were available, not a newly preregistered experiment.','Removed scenarios remain archived and must not be treated as current entry logic.']}
 with (root/'scope-amendment-audit.json').open('x') as f:json.dump(out,f,ensure_ascii=False,indent=2);f.write('\n')
 return out
if __name__=='__main__':print(json.dumps(audit(Path(sys.argv[1]).resolve())))
