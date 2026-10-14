"""Freeze activity percentiles from 00–12 only; no outcome-based selection."""
import argparse,csv,json
from pathlib import Path
import numpy as np
from day_history import start_ms,write,sha
FIELDS=['ns','count_bybit','count_bitget','turnover_bybit_1e8','turnover_bitget_1e8','last_trade_age_bybit_ns','last_trade_age_bitget_ns','warm_valid_mask']
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--date',required=True);ap.add_argument('--source',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);a=ap.parse_args()
    with np.load(a.source,allow_pickle=False) as z:
        if z['fields'].tolist()!=FIELDS:raise ValueError('activity fields differ')
        x=z['features']
    start=start_ms(a.date)*10**6;cut=start+43200*10**9
    if x.dtype!=np.int64 or x.shape!=(86400,8) or x[0,0]!=start or not np.all(np.diff(x[:,0])==10**9):raise ValueError('wrong daily grid')
    train=x[(x[:,0]<cut)&(x[:,7]==3)]
    if not len(train):raise ValueError('no complete warm training observations')
    rows=[]
    for q in (.1,.25):
        values=np.quantile(train[:,1:5],q,axis=0,method='higher').astype(np.int64)
        if np.any(values<0):raise ValueError('negative activity threshold')
        rows.append([f'q{round(100*q)}',*map(int,values),5*10**9])
    with a.output.open('x',newline='') as f:
        w=csv.writer(f);w.writerow(['name','countA','countB','turnoverAunits','turnoverBunits','maxAgeNs']);w.writerows(rows)
    write(a.output.with_suffix('.calibration.json'),{'date':a.date,'train_start_ns':start,'train_end_ns_exclusive':cut,'first_eligible_entry_ns':cut,'training_samples':len(train),'method':'higher','q25_frozen_for_comparison':True,'q10':'compatibility diagnostic only, not considered for selection','calibrated_from_pnl':False,'source':str(a.source),'source_sha256':sha(a.source),'thresholds_sha256':sha(a.output),'rows':rows,'code_sha256':sha(__file__),'execution_enabled':False})
    print(json.dumps({'date':a.date,'training_samples':len(train),'rows':rows}))
if __name__=='__main__':main()
