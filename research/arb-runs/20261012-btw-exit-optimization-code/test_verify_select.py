"""Small contract, selection-boundary and synthetic end-to-end tests."""
import copy
import csv
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from decimal import Decimal as D
import verify_select as v

DAY = 1790812800000000000


def cycle(index, hour, net):
    ns=DAY+hour*3600*1_000_000_000
    return {'cycle':str(index),'entry_ns':str(ns),'exit_ns':str(ns+1000000000),
            'net_before_funding':str(net),'gross_pnl':str(D(net)+D('.01')),
            'total_fees':'.01','exit_reason':'take_profit' if D(net)>0 else 'stop_loss'}


def write_csv(path, data):
    with path.open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(data[0]));writer.writeheader();writer.writerows(data)


def baseline_fixture(root):
    screen=root/'screen';base=screen/v.BASELINE;base.mkdir(parents=True)
    reference=root/'reference';reference.mkdir()
    oldorders=[];neworders=[];oldcycles=[];newcycles=[];snapshots=[]
    cash={'bybit':D(1000),'bitget':D(1000)}
    for index,hour in enumerate((1,13,19),1):
        ns=DAY+hour*3600*1_000_000_000;oldid='random-'+str(index)
        prices={'bybit':D('1'),'bitget':D('1.003')};xp={'bybit':D('.99999'),'bitget':D('1.00301')}
        gross={};fees={}
        for intent in ('entry','exit'):
            for venue in v.VENUES:
                price=(prices if intent=='entry' else xp)[venue];f=price*10*D('.0003')
                lng=venue=='bybit';buy=lng==(intent=='entry');n=ns+(intent=='exit')*1_000_000_000
                if intent=='entry':
                    gross[venue]=D(0);fees[venue]=f;cash[venue]-=f
                else:
                    gross[venue]=10*(price-prices[venue])*(1 if lng else -1)
                    fees[venue]+=f;cash[venue]+=gross[venue]-f
                oldorders.append(dict(cycle_id=oldid,decision_cursor_ns=n,timestamp_ms=n//1_000_000,
                    venue=venue,side='buy' if buy else 'sell',intent=intent,qty=10,price=price,fee=f,
                    symbol='BTWUSDT',pair='BTW/USDT:USDT',fee_rate='.0003',position_side='long' if lng else 'short'))
                neworders.append(dict(cycle=index,ns=n,timestamp_ms=n//1_000_000,venue=venue,
                    side='buy' if buy else 'sell',intent=intent,qty=10,price=price,fee=f,
                    cash=cash[venue],exit_reason='stop_loss' if intent=='exit' else ''))
        g=sum(gross.values());f=sum(fees.values());net=g-f
        common=dict(qty=10,long_venue='bybit',short_venue='bitget',entry_timestamp_ms=ns//1_000_000,
                    exit_timestamp_ms=ns//1_000_000+1000,holding_ms=1000,long_entry_price='1',short_entry_price='1.003',
                    gross_pnl=g,total_fees=f,net_before_funding=net,bybit_net_before_funding=gross['bybit']-fees['bybit'],
                    bitget_net_before_funding=gross['bitget']-fees['bitget'],exit_reason='stop_loss',exit_parts=1)
        oldcycles.append(dict(cycle_id=oldid,decision_cursor_ns=ns+1_000_000_000,**common))
        newcycles.append(dict(cycle=index,entry_ns=ns,exit_ns=ns+1_000_000_000,first_trigger_ns=ns+1_000_000_000,**common))
        snapshots.append(dict(cycle_id=index,entry_ns=ns,long='bybit',qty=10,
            entry_prices={'bybit':100000,'bitget':100300},entry_price_tick_size='0.00001',price_units='1e-8',
            after_entry_consumption=True,sides=[[dict(price_units_1e8=p,displayed_qty=40,remaining_quarter=remain)]
             for p,remain in [(100000000,0),(99999000,40),(100301000,40),(100300000,0)]]))
    write_csv(base/'orders.csv',neworders);write_csv(base/'cycles.csv',newcycles)
    write_csv(reference/'orders.csv',oldorders);write_csv(reference/'cycles.csv',oldcycles)
    (base/'entry_budgets.jsonl').write_text(''.join(json.dumps(s)+'\n' for s in snapshots))
    scenario=dict(name=v.BASELINE,mode='absolute_net',tp_bps=2,sl_bps=2,hard_cap_bps=0,
                  unresolved_qty=0,orders=12,cycles=3,cash={k:str(x) for k,x in cash.items()},
                  gross_pnl=str(sum(r['gross_pnl'] for r in newcycles)),fees=str(sum(r['total_fees'] for r in newcycles)),
                  net_before_funding=str(sum(r['net_before_funding'] for r in newcycles)))
    aggregate=dict(status='COMPLETED_SCREEN_ONLY',execution_enabled=False,tape_format='BTWTP002',scenarios=[scenario])
    (screen/'aggregate.json').write_text(json.dumps(aggregate))
    return screen,reference,aggregate


class Tests(unittest.TestCase):
    def test_protocol_identity_and_no_threshold_drift(self):
        p=v.jread(v.PROTOCOL);v.validate_protocol(p)
        bad=copy.deepcopy(p);bad['split_utc']['training']=['00:00','13:00']
        with self.assertRaises(ValueError):v.validate_protocol(bad)
        bad=copy.deepcopy(p);bad['selection']['minimum_training_cycles']=14
        with self.assertRaises(ValueError):v.validate_protocol(bad)

    def test_partition_boundary_is_purged_and_start_is_inclusive(self):
        self.assertEqual(v.partition(cycle(1,12,'.1'),DAY),'validation')
        row=cycle(1,11,'.1');row['exit_ns']=str(DAY+12*3600*1_000_000_000)
        self.assertEqual(v.partition(row,DAY),'boundary_crossing')
        row=cycle(1,17,'.1');row['exit_ns']=str(DAY+18*3600*1_000_000_000)
        self.assertEqual(v.partition(row,DAY),'boundary_crossing')
        self.assertEqual(v.partition(cycle(1,18,'.1'),DAY),'internal_holdout')

    def test_score_uses_exit_chronology_and_closed_drawdown(self):
        x=v.statistics([cycle(3,3,'-1'),cycle(1,1,'2'),cycle(2,2,'-3')])
        self.assertEqual(x['net'],D('-2'))
        self.assertEqual(x['realized_max_drawdown'],D(4))
        self.assertEqual(x['score'],D('-4'))

    def test_selection_minimum_ties_validation_gates_and_holdout_ignored(self):
        def item(name,mode,tp,sl,train,validation):
            return dict(name=name,mode=mode,tp_bps=tp,sl_bps=sl,hard_cap_bps=100 if mode=='entry_deterioration' else 0,
                        training=dict(cycles=train[0],score=D(train[1])),
                        validation=dict(score=D(validation[0]),net=D(validation[1]),realized_max_drawdown=D(validation[2])),
                        internal_holdout={'net':'999999'})
        data={x['name']:x for x in [item(v.BASELINE,'absolute_net',2,2,(20,0),(0,0,1)),
              item('absolute_net_tp2_sl25','absolute_net',2,25,(15,2),(1,1,1)),
              item('absolute_net_tp5_sl50','absolute_net',5,50,(15,2),(1,1,1)),
              item('absolute_net_tp5_sl25','absolute_net',5,25,(15,2),(1,1,1)),
              item('entry_deterioration_tp2_sl10','entry_deterioration',2,10,(14,999),(999,999,0))]}
        selected=v.choose(data)
        self.assertEqual(selected['selected_name'],'absolute_net_tp5_sl25')
        self.assertEqual(selected['training_finalists'],['absolute_net_tp5_sl25'])
        data['absolute_net_tp5_sl25']['validation']['realized_max_drawdown']=D(2)
        fallback=v.choose(data)
        self.assertEqual(fallback['selected_name'],'entry_deterioration_tp2_sl10')
        self.assertEqual(fallback['selection_role'],'preregistered_mechanism_reference_not_winner')
        for row in data.values():row['internal_holdout']['net']='-999999999999'
        self.assertEqual(v.choose(data),fallback)

    def test_baseline_equivalence_fails_on_ns_or_fee_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            screen,ref,_=baseline_fixture(Path(tmp))
            good=v.verify_baseline(screen,ref,screen/'pass.json')
            self.assertEqual(good['status'],'PASS',good)
            order=v.rows(screen/v.BASELINE/'orders.csv');order[0]['ns']=str(int(order[0]['ns'])+1)
            order[1]['fee']='.000000001';write_csv(screen/v.BASELINE/'orders.csv',order)
            bad=v.verify_baseline(screen,ref,screen/'fail.json')
            self.assertEqual(bad['status'],'FAIL')
            self.assertTrue(any('ns' in error for error in bad['errors']))
            self.assertTrue(any('fee' in error for error in bad['errors']))
            with self.assertRaises(FileExistsError):v.verify_baseline(screen,ref,screen/'pass.json')

    def test_end_to_end_freeze_precedes_holdout_calculation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);screen,ref,aggregate=baseline_fixture(root)
            eq=screen/'baseline-equivalence.json'
            self.assertEqual(v.verify_baseline(screen,ref,eq)['status'],'PASS')
            p=v.jread(v.PROTOCOL)
            for mode,grid in p['candidate_grid'].items():
                for tp in grid['take_profit_bps']:
                    for sl in grid['stop_loss_bps']:
                        name=f'{mode}_tp{tp}_sl{sl}'
                        shutil.copytree(screen/v.BASELINE,screen/name)
                        config=dict(aggregate['scenarios'][0],name=name,mode=mode,tp_bps=tp,sl_bps=sl,hard_cap_bps=grid.get('hard_stop_loss_bps',0))
                        aggregate['scenarios'].append(config)
            (screen/'aggregate.json').write_text(json.dumps(aggregate))
            output=root/'selection';original=v.statistics;saw_holdout=[]
            def monitored(data):
                if any(int(row['entry_ns'])>=DAY+18*3600*1_000_000_000 for row in data):
                    self.assertTrue((output/'selection-frozen.json').exists())
                    saw_holdout.append(True)
                return original(data)
            with patch.object(v,'statistics',monitored):
                result=v.select(screen,v.PROTOCOL,eq,output)
            self.assertTrue(saw_holdout)
            self.assertEqual(result['selection']['selection_role'],'preregistered_mechanism_reference_not_winner')
            self.assertFalse(result['internal_holdout_comparison']['validated_improvement_claim'])
            frozen=v.jread(output/'selection-frozen.json')
            self.assertNotIn('internal_holdout',frozen['training_validation'][v.BASELINE])
            with self.assertRaises(FileExistsError):v.select(screen,v.PROTOCOL,eq,output)


if __name__=='__main__':
    unittest.main(verbosity=2)
