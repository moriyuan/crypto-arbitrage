import importlib.util
import io
from pathlib import Path
import sys

import pytest

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent/'20261012-btw-as-code'))
from day_adapter import NativeEvent, EventBatch, StartupBatch, iter_batches

SPEC=importlib.util.spec_from_file_location('extract_book_events',HERE/'extract_book_events.py')
M=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def event(venue,ns,seq,kind='book',snapshot=False,a=None,b=None):
    data={'a':a or [],'b':b or []}
    native=None if kind=='disconnect' else ({'type':'snapshot' if snapshot else 'delta','data':data}
            if venue=='bybit' else {'action':'snapshot' if snapshot else 'update','data':[data]})
    return NativeEvent(venue,'bybit' if venue=='bybit' else 'bitget-futures',kind,
                       None if kind=='disconnect' else 'native',None if kind=='disconnect' else ns,
                       ns,'preceding_local_timestamp_ordering_cursor_only' if kind=='disconnect' else 'native_local_timestamp',
                       native,Path('/tmp/fixture.gz'),0,seq,seq,b'fixture\n')


def run(batches):
    out=io.BytesIO();stats=M.emit_batches(batches,out)
    data=out.getvalue();assert data[:8]==b'BTWBOOK1'
    return list(M.ROW.iter_unpack(data[8:])),stats,data


def test_snapshot_not_updates_and_zero_delta_retained():
    e=[event('bybit',100,1,snapshot=True,a=[['1','30']],b=[['.9','10']]),
       event('bybit',200,2),event('bybit',300,3,a=[['1','0'],['2','7']],b=[['.9','2']])]
    rows,stats,_=run(iter_batches(streams={'bybit':e,'bitget':[]}))
    assert rows==[(100,0,0,0),(200,0,2,0),(300,0,2,3)]
    assert stats['accepted_counts']['bybit']['snapshot_frames']==1
    assert stats['accepted_counts']['bybit']['delta_frames']==2
    assert stats['accepted_counts']['bybit']['delta_level_entries']==3
    assert stats['accepted_counts']['bybit']['zero_level_delta_frames']==1


def test_bitget_counts_all_payload_price_rows_including_zero_quantity():
    e=event('bitget',100,1,a=[['1','0']],b=[['.9','1']])
    e.native['data'].append({'a':[['2','4'],['3','0']],'b':[]})
    assert M.delta_level_count(e)==4
    bad=event('bitget',100,1);bad.native['data']={'a':[],'b':[]}
    with pytest.raises(ValueError,match='Bitget'):
        M.delta_level_count(bad)


def test_same_cursor_disconnect_suppresses_earlier_book_and_then_recovers():
    stream=[event('bybit',100,1,snapshot=True),event('bybit',200,2,a=[['1','2']]),
            event('bybit',200,3,kind='disconnect'),event('bybit',200,4,snapshot=True),
            event('bybit',200,5,b=[['.9','0']])]
    rows,stats,_=run(iter_batches(streams={'bybit':stream,'bitget':[]}))
    assert rows==[(100,0,0,0),(200,0,1,0),(200,0,0,0),(200,0,2,1)]
    assert stats['suppressed_counts']['bybit:book']==1
    assert stats['raw_counts']['kind:bybit:book']==4
    assert stats['accepted_counts']['bybit']['untimed_disconnect_frames']==1


def test_exact_startup_omission_is_preserved():
    orphan=event('bybit',100,1,a=[['1','2']])
    batches=[StartupBatch(100,(orphan,),(('bybit',1),)),
             EventBatch(200,(event('bybit',200,2,snapshot=True),)),
             EventBatch(300,(event('bybit',300,3,a=[['1','2']]),))]
    rows,stats,_=run(batches)
    assert rows==[(200,0,0,0),(300,0,2,1)]
    assert stats['raw_counts']['batches']==3
    assert stats['suppressed_counts']['bybit:book']==1


def test_reconstruction_event_order_is_not_resorted():
    b=EventBatch(100,(event('bybit',100,1,snapshot=True),event('bitget',100,1,snapshot=True),
                     event('bybit',100,2,a=[['1','2']]),event('bitget',100,2,b=[['.8','0']])))
    rows,_,_=run([b])
    assert [r[1] for r in rows]==[0,1,0,1]


def test_delta_before_snapshot_fails_closed():
    with pytest.raises(ValueError,match='snapshot'):
        run([EventBatch(100,(event('bybit',100,1),))])


def test_stream_revalidation_crc_and_counts(tmp_path):
    rows,stats,data=run([EventBatch(100,(event('bybit',100,1,snapshot=True),))])
    path=tmp_path/'events.bin';path.write_bytes(data)
    check=M.validate_sidecar(path,stats)
    assert check['status']=='PASS' and check['rows']==1
    assert check['crc32']==stats['output_crc32']
    path.write_bytes(data[:-1])
    with pytest.raises(ValueError,match='truncated'):
        M.validate_sidecar(path,stats)
