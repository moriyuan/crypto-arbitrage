"""Predeclared gating selection. Never receives evaluation results."""
from decimal import Decimal

def choose(rows, fit_dates, policies, minimum_cycles=10):
    if len(fit_dates)!=2 or len(set(fit_dates))!=2:
        raise ValueError('two distinct fit dates required')
    names=[r['id'] for r in rows]
    if len(set(names))!=len(names) or set(names)!={p['id'] for p in policies}:
        raise ValueError('policy identity/count mismatch')
    by_id={r['id']:r for r in rows}; metrics={}
    for row in rows:
        if set(row['days'])!=set(fit_dates):
            raise ValueError('selection may consume only declared fit dates')
        ds=[row['days'][d] for d in fit_dates]
        nets=[Decimal(str(d['net'])) for d in ds]
        dds=[Decimal(str(d['drawdown'])) for d in ds]
        if not all(v.is_finite() for v in nets+dds) or any(v<0 for v in dds):
            raise ValueError('nonfinite or invalid metric')
        metrics[row['id']]={'worst_date_net':min(nets),'sum_net':sum(nets),'sum_drawdown':sum(dds)}
    base=metrics['baseline']; scored=[]
    for p in policies:
        name=p['id']; row=by_id[name];m=metrics[name];reasons=[];control=None
        if p['role']!='candidate':reasons.append('not_candidate')
        else:
            control=p['control_id']
            if control not in metrics or next(q['role'] for q in policies if q['id']==control) not in ('baseline','coverage_control'):
                raise ValueError('invalid explicit coverage control')
            ds=[row['days'][d] for d in fit_dates]
            if not all(d['cycles']>=minimum_cycles for d in ds):reasons.append('minimum_cycles_each_fit_day')
            if not all(d['unresolved_qty']==0 and d['funding_status']=='VERIFIED_NO_SETTLEMENT' and d['accounting_pass'] is True for d in ds):reasons.append('audit_funding_residual')
            if m['worst_date_net']<=base['worst_date_net']:reasons.append('worst_day_not_improved')
            if m['sum_net']<=base['sum_net']:reasons.append('fit_total_not_improved')
            if m['sum_net']<=metrics[control]['sum_net']:reasons.append('coverage_control_not_improved')
        scored.append({**row,**{k:str(v) for k,v in m.items()},'coverage_control':control,'eligible':not reasons,'rejection_reasons':reasons})
    ranked=sorted([r for r in scored if r['eligible']],key=lambda r:(-Decimal(r['worst_date_net']),-Decimal(r['sum_net']),Decimal(r['sum_drawdown']),r['id']))
    return (ranked[0] if ranked else None),scored
