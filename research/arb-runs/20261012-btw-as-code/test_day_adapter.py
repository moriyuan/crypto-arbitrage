from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as N
import pytest
import day_adapter as a


def evidence():
    return {'status':'EXACT_STARTUP_ORPHAN_PROVEN','date':'2026-08-01','execution_enabled':False,
            'orphan':{'line':2,'receipt_timestamp':'2026-08-01T00:00:00.0708039Z','source_message':{'topic':'orderbook.50.BTWUSDT','type':'delta','data':{'s':'BTWUSDT','u':2,'seq':2}}},
            'first_snapshot':{'line':3,'receipt_timestamp':'2026-08-01T00:00:00.1954482Z','source_message':{'topic':'orderbook.50.BTWUSDT','type':'snapshot','data':{'s':'BTWUSDT','u':1,'seq':1}}},
            'post_snapshot_identical_delta':{'line':12,'receipt_timestamp':'2026-08-01T00:00:00.1954512Z','source_message':{'topic':'orderbook.50.BTWUSDT','type':'delta','data':{'s':'BTWUSDT','u':2,'seq':2}}},
            'earliest_permitted_bybit_activity_from_initial_snapshot_ns':1785542460195448200}


def test_only_exact_one_startup_orphan_audit_can_be_permitted():
    e=evidence();venues=[{'venue':'bybit','counts':{'book_updates_while_uninitialized':1},'examples':[], 'status':'REVIEW'}, {'venue':'bitget-futures','counts':{},'examples':[], 'status':'PASS'}]
    a.validate_startup_scope(e,venues,'2026-08-01')
    for venue,key,val in [(0,'book_updates_while_uninitialized',2),(1,'book_updates_while_uninitialized',1),(0,'bybit_nonincreasing_seq',1)]:
        wrong=json.loads(json.dumps(venues));wrong[venue]['counts'][key]=val
        with pytest.raises(ValueError):a.validate_startup_scope(e,wrong,'2026-08-01')
    with pytest.raises(ValueError):a.validate_startup_scope(e,venues,'2026-09-01')
    wrong=json.loads(json.dumps(e));wrong['orphan']['line']=4
    with pytest.raises(ValueError):a.validate_startup_scope(wrong,venues,'2026-08-01')


def event(row, sequence=None):
    ns=a.parse_local_ns(row['receipt_timestamp'])
    return a.NativeEvent('bybit','bybit','book',row['receipt_timestamp'],ns,ns,'native_local_timestamp',row['source_message'],Path('/archive/bybit/offset0000.jsonl.gz'),0,row['line'],sequence or row['line'],b'raw')


def test_filter_preserves_raw_events_but_never_initializes_from_orphan():
    e=evidence();events=[event(e[x]) for x in ('orphan','first_snapshot','post_snapshot_identical_delta')]
    batches=list(a.apply_startup_guard(a.iter_batches(streams={'bybit':events}),e))
    assert batches[0].events==(events[0],) and batches[0].reconstruction_events==()
    assert batches[1].reconstruction_events==(events[1],)
    assert batches[2].reconstruction_events==(events[2],)
    with pytest.raises(ValueError,match='not observed'):
        list(a.apply_startup_guard(a.iter_batches(streams={'bybit':events[1:]}),e))


def test_wrong_native_or_midday_orphan_is_not_silently_ignored():
    e=evidence();events=[event(e[x]) for x in ('orphan','first_snapshot','post_snapshot_identical_delta')]
    wrong=replace(events[0],native={'type':'delta','data':{'u':3}})
    with pytest.raises(ValueError,match='mismatch'):
        list(a.apply_startup_guard(a.iter_batches(streams={'bybit':[wrong,*events[1:]]}),e))
    late=replace(events[0],line_number=50,source_sequence=50,ordering_ns=events[-1].ordering_ns+100,local_ns=events[-1].ordering_ns+100)
    batches=list(a.apply_startup_guard(a.iter_batches(streams={'bybit':[*events,late]}),e))
    assert batches[-1].reconstruction_events==(late,)


def test_startup_history_and_activity_boundaries():
    e=evidence();snapshot=a.parse_local_ns(e['first_snapshot']['receipt_timestamp'])
    archive=N(startup_exception=e)
    assert not a.startup_activity_ready(archive,'bybit',snapshot-1)
    assert a.startup_activity_ready(archive,'bybit',snapshot)
    assert a.startup_activity_ready(archive,'bitget',snapshot-1)
    cutoff=snapshot+60_000_000_000
    assert not a.startup_entry_ready(archive,cutoff-1)
    assert a.startup_entry_ready(archive,cutoff)
    assert a.startup_entry_ready(N(),0)
