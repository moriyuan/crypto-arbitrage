"""Training-only, censored public trade-reach intensity proxy for AS quotes.

Independent hypothetical 1s anchors do not establish real queue fill rates.
The fixed protocol pools buy/sell rates and fits log(lambda)=log(A)-k*distance.
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
from decimal import Decimal as D,ROUND_FLOOR,ROUND_CEILING
import datetime as dt
import heapq
import json
import math
from pathlib import Path
import sys
import time
import numpy as np

OLD=Path('/Users/mori/code/crypto/state/arb-runs/20261012-btw-activity-maker-code')
sys.path.insert(0,str(OLD))
import maker_engine as base
from execution_adapter import PriceSchema

NS=base.NS
BP_GRID=(0,1,2,5,10,20,50)
TICK=D('0.00001');UNIT=D(10)
GRID_COLUMNS=('ns','bybit_mid_ticks_x2','bitget_mid_ticks_x2','validmask','segmentA','segmentB')


def load_grid(path):
    with np.load(path,allow_pickle=False) as z:
        if 'data' in z:
            x=z['data']
            if 'columns' in z and tuple(z['columns'].tolist())!=GRID_COLUMNS:raise ValueError('mid grid column order mismatch')
        elif 'grid' in z:x=z['grid']
        else:x=np.column_stack([z[name] for name in GRID_COLUMNS])
    if x.dtype!=np.int64 or x.ndim!=2 or x.shape[1]!=6:raise ValueError('grid must be int64[n,6]')
    if len(x) and (np.any(x[:,0]%NS) or np.any(np.diff(x[:,0])!=NS)):raise ValueError('grid must be contiguous 1s local-receive observations')
    if np.any((x[:,3]<0)|(x[:,3]>3)):raise ValueError('invalid grid validity mask')
    for v in (0,1):
        good=(x[:,3]&(1<<v))!=0
        if np.any(x[good,1+v]<=0) or np.any(x[good,4+v]<=0):raise ValueError('valid grid quote requires positive mid and segment')
    return x


def raw_book_records(path):
    with open(path,'rb') as f:
        if f.read(8) not in (b'BTWTP002',b'BTWTP003'):raise ValueError('book magic mismatch')
        previous=-1
        while raw:=f.read(base.HEADER.size):
            if len(raw)!=base.HEADER.size:raise ValueError('truncated header')
            ns,valid,force,history,pad,n,*vals=base.HEADER.unpack(raw)
            if ns<=previous or valid not in (0,1) or n>1000 or (not valid and n) or pad:raise ValueError('bad book header')
            previous=ns
            if len(f.read(n*base.UPDATE.size))!=n*base.UPDATE.size:raise ValueError('truncated book updates')
            yield base.Tick(ns,bool(valid),force,bool(history),vals[:4],vals[4:],[])


@dataclass(slots=True)
class Window:
    id:int
    venue:int
    side:int # 0 buy maker, 1 sell maker
    bin:int
    quote:int
    anchor_ns:int
    arrival_ns:int
    end_ns:int
    first_hit_ns:int|None=None
    censored_reason:str='ttl'
    depth_key_scale:int=1000

    def hit(self,tr):
        if tr.venue!=self.venue or tr.flags or not self.arrival_ns<=tr.source_ns<self.end_ns:return
        if tr.aggressor!=(1 if self.side==0 else 0):return
        if not (tr.price8<self.quote*self.depth_key_scale if self.side==0 else tr.price8>self.quote*self.depth_key_scale):return
        quantum=10 if self.venue==0 else 1
        if tr.qty8//(4*base.PRICE8*quantum)<=0:return
        if self.first_hit_ns is None or tr.source_ns<self.first_hit_ns:self.first_hit_ns=tr.source_ns

    def exposure(self):
        end=self.end_ns if self.first_hit_ns is None else min(self.first_hit_ns,self.end_ns)
        return max(0,end-self.arrival_ns)


def blank_stats():return {'anchors':0,'arrived':0,'arrival_invalid':0,'post_only_rejected':0,'training_tail_excluded':0,'hits':0,'exposure_ns':0,'censored_ttl':0,'censored_invalid':0,'censored_train_end':0,'zero_exposure_windows':0}


class ReachCalibration:
    def __init__(self,median_lot_mid,train_end_ns,price_schema=None):
        self.schema=price_schema or PriceSchema(5,(1,1))
        self.medians=median_lot_mid;self.distances=[[m*D(bp)/10000 for bp in BP_GRID] for m in median_lot_mid]
        self.end=train_end_ns;self.exposure_end=train_end_ns-base.NOTIFICATION_GRACE
        self.pending=[];self.active={};self.heap=[];self.next_id=0;self.last_ns=-1
        self.stats=[[[blank_stats() for _ in BP_GRID] for _ in (0,1)] for _ in (0,1)]

    def advance(self,ns):
        if ns<self.last_ns:raise ValueError('calibration clock went backwards')
        self.last_ns=ns
        while self.heap and self.heap[0][0]<=ns:
            deadline,wid=heapq.heappop(self.heap);w=self.active.get(wid)
            if w is None or deadline!=w.end_ns+base.NOTIFICATION_GRACE:continue
            st=self.stats[w.venue][w.side][w.bin];duration=w.exposure()
            st['exposure_ns']+=duration;st['zero_exposure_windows']+=duration==0
            if w.first_hit_ns is not None and w.first_hit_ns<w.end_ns:st['hits']+=1
            else:st['censored_'+w.censored_reason]+=1
            del self.active[wid]

    def anchor(self,row):
        ns=int(row[0]);self.advance(ns)
        if ns+base.LATENCY>=self.exposure_end:return
        for venue in (0,1):
            if not int(row[3])&(1<<venue) or self.medians[venue]<=0:continue
            mid=D(int(row[venue+1]))*self.schema.price_unit*UNIT/2
            for side in (0,1):
                for b,delta in enumerate(self.distances[venue]):
                    step=self.schema.native_tick_units[venue]
                    raw=(mid-delta if side==0 else mid+delta)/UNIT/self.schema.price_unit/step
                    quote=int(raw.to_integral_value(rounding=ROUND_FLOOR if side==0 else ROUND_CEILING))*step
                    self.next_id+=1;self.stats[venue][side][b]['anchors']+=1
                    self.pending.append((self.next_id,venue,side,b,quote,ns))

    def book(self,t):
        self.advance(t.ns)
        if not t.valid:
            for w in self.active.values():
                if t.ns<w.end_ns:
                    w.end_ns=t.ns;w.censored_reason='invalid';heapq.heappush(self.heap,(w.end_ns+base.NOTIFICATION_GRACE,w.id))
        pending=[]
        for wid,v,side,b,quote,anchor in self.pending:
            if t.ns<anchor+base.LATENCY:
                pending.append((wid,v,side,b,quote,anchor));continue
            st=self.stats[v][side][b]
            if t.ns>=self.exposure_end:st['training_tail_excluded']+=1;continue
            if not t.valid:st['arrival_invalid']+=1;continue
            if quote<=0 or (quote>=t.price[2*v] if side==0 else quote<=t.price[2*v+1]):st['post_only_rejected']+=1;continue
            end=min(t.ns+base.TTL,self.exposure_end)
            w=Window(wid,v,side,b,quote,anchor,t.ns,end,censored_reason='train_end' if end==self.exposure_end else 'ttl',depth_key_scale=self.schema.depth_key_scale)
            self.active[wid]=w;st['arrived']+=1;heapq.heappush(self.heap,(end+base.NOTIFICATION_GRACE,wid))
        self.pending=pending

    def trade(self,tr):
        self.advance(tr.ns)
        for w in self.active.values():w.hit(tr)

    def finish(self):
        self.advance(self.end)
        if self.active:raise AssertionError('uncensored active windows at training end')
        for _,v,side,b,_,_ in self.pending:self.stats[v][side][b]['training_tail_excluded']+=1
        self.pending=[]


def fit_venue(distances,side_stats):
    bins=[]
    for b,d in enumerate(distances):
        hit=sum(side_stats[side][b]['hits'] for side in (0,1));exposure=sum(side_stats[side][b]['exposure_ns'] for side in (0,1))/NS
        rate=hit/exposure if exposure else None
        bins.append({'distance_bps':BP_GRID[b],'distance_usdt_per_lot':str(d),'hits':hit,'exposure_seconds':exposure,
                     'rate_per_second':rate,'usable':hit>=20 and exposure>=600,
                     'buy':side_stats[0][b],'sell':side_stats[1][b]})
    use=[x for x in bins if x['usable']]
    diagnostic={'bins':bins,'usable_distances':len(use),'fit_weight':'first-hit counts; approximate inverse variance of log rate',
      'right_censoring':'Unhit TTL/invalid/train-end windows contribute only actually live exposure; rejected arrivals contribute none',
      'distance_rounding':'Requested common absolute distances fitted; actual candidate quotes rounded buy-floor/sell-ceil to tick',
      'rate_interpretation':'Pooled public trade-reach proxy under hypothetical strict-through/participation, not true queue fill probability'}
    if len(use)<4:return {'accepted':False,'reason':'fewer_than_four_usable_distances','a_per_second':None,'k_per_usdt':None,'fit_diagnostics':diagnostic}
    x=np.asarray([float(b['distance_usdt_per_lot']) for b in use]);y=np.log([b['rate_per_second'] for b in use]);w=np.asarray([b['hits'] for b in use],dtype=float)
    xm=np.average(x,weights=w);ym=np.average(y,weights=w);den=float(np.sum(w*(x-xm)**2))
    if den<=0:return {'accepted':False,'reason':'no_distance_variation','a_per_second':None,'k_per_usdt':None,'fit_diagnostics':diagnostic}
    slope=float(np.sum(w*(x-xm)*(y-ym))/den);intercept=float(ym-slope*xm);k=-slope
    try:A=math.exp(intercept)
    except OverflowError:A=float('inf')
    residual=y-(intercept+slope*x);rmse=float(np.sqrt(np.average(residual**2,weights=w)))
    denom=float(np.sum(w*(y-ym)**2));r2=1-float(np.sum(w*residual**2))/denom if denom else None
    diagnostic.update(weighted_log_rmse=rmse,weighted_r_squared=r2,log_a=intercept,slope=slope)
    good=math.isfinite(A) and A>0 and math.isfinite(k) and k>0 and math.isfinite(rmse) and rmse<=1
    return {'accepted':good,'reason':'accepted' if good else 'invalid_slope_or_fit_error',
      'a_per_second':str(A) if math.isfinite(A) and A>0 else None,
      'k_per_usdt':str(k) if math.isfinite(k) and k>0 else None,'fit_diagnostics':diagnostic}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for arg in ('date','tape','trades','grid','protocol','output'):p.add_argument('--'+arg,required=True)
    args=p.parse_args();out=Path(args.output)
    if out.exists():raise FileExistsError('refusing existing calibration output')
    date=dt.datetime.fromisoformat(args.date).replace(tzinfo=dt.timezone.utc);start=int(date.timestamp()*NS);end=start+43200*NS;dayend=start+86400*NS
    schema=PriceSchema.from_tape(args.tape)
    grid=load_grid(args.grid)
    if len(grid)!=86400 or int(grid[0,0])!=start or int(grid[-1,0])!=dayend-NS:raise ValueError('grid must cover requested UTC day')
    json.loads(Path(args.protocol).read_text())
    medians=[]
    for v in (0,1):
        vals=sorted(int(x) for x in grid[(grid[:,0]<end)&((grid[:,3]&(1<<v))!=0),v+1])
        med=D(0) if not vals else (D(vals[(len(vals)-1)//2])+vals[len(vals)//2])/2*schema.price_unit*UNIT/2
        medians.append(med)
    fit=ReachCalibration(medians,end,schema);books=iter(raw_book_records(args.tape));trades=iter(base.trade_records(args.trades))
    b=next(books,None);tr=next(trades,None);gidx=0;bc=tc=0;started=time.time()
    # Receive-time information order: native trades, completed book batch, then
    # equal-second anchor, which already contains only receive<=anchor information.
    while True:
        times=[b.ns if b is not None else end,tr.ns if tr is not None else end,int(grid[gidx,0]) if gidx<len(grid) else end]
        now=min(times)
        if now>=end:break
        if tr is not None and tr.ns==now:
            fit.trade(tr);tc+=1;tr=next(trades,None)
        elif b is not None and b.ns==now:
            fit.book(b);bc+=1;b=next(books,None)
            if bc%250000==0:print(json.dumps({'training_books':bc,'training_trades':tc,'elapsed_seconds':round(time.time()-started,2)}),flush=True)
        else:fit.anchor(grid[gidx]);gidx+=1
    fit.finish();out.mkdir(parents=True)
    inputs={k:{'path':getattr(args,k),'sha256':base.digest(getattr(args,k))} for k in ('tape','trades','grid','protocol')}
    result={'schema_version':1,'status':'COMPLETED_TRAINING_ONLY_CALIBRATION','symbol':'BTWUSDT','date_utc':args.date,
      'execution_enabled':False,'training_receipt_start_ns':start,'training_receipt_end_ns_exclusive':end,
      'evaluation_start_ns':end,'evaluation_end_ns_exclusive':dayend,'source_books_used':bc,'source_live_trades_used':tc,
      'elapsed_seconds':time.time()-started,'quote_unit_coins':'10','inputs':inputs,'code_sha256':base.digest(__file__),
      'code_sha256_dependencies':{'as_calibrate.py':base.digest(__file__),'execution_adapter.py':base.digest(Path(__file__).with_name('execution_adapter.py')),'frozen_maker_engine.py':base.digest(base.__file__)},
      'inherited_execution_code_sha256':base.digest(base.__file__),'price_schema':schema.to_dict(),'venues':{}}
    for v,name in enumerate(('bybit','bitget')):
        f=fit_venue(fit.distances[v],fit.stats[v]);f.update(schema_version=1,symbol='BTWUSDT',venue=name,quote_unit_coins='10',
          trained_from_ns=start,trained_through_ns=end-1,valid_from_ns=end,valid_until_ns=dayend,
          training_median_mid_usdt_per_lot=str(medians[v]),source_sha256=[x['sha256'] for x in inputs.values()])
        result['venues'][name]=f
    (out/'calibration.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'status':result['status'],'venues':{v:{k:s[k] for k in ('accepted','reason','a_per_second','k_per_usdt')} for v,s in result['venues'].items()},'elapsed_seconds':result['elapsed_seconds']}),flush=True)

if __name__=='__main__':main()
