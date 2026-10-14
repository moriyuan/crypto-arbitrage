import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tardis_adapter import (
    ArchiveError, BookValidity, NativeEvent, assert_not_future, iter_batches,
    iter_raw_file_events, parse_local_ns, preflight,
)


def frame(stamp, venue, kind='snapshot', seq=1):
    if venue == 'bybit':
        data = {'topic': 'orderbook.50.BTWUSDT', 'type': kind, 'ts': 1790812800000,
                'cts': 1790812800000, 'data': {'s': 'BTWUSDT', 'u': seq, 'seq': seq,
                                             'b': [['1', '1']], 'a': [['2', '1']]}}
    else:
        data = {'action': kind, 'arg': {'instType': 'usdt-futures', 'topic': 'books', 'symbol': 'BTWUSDT'},
                'ts': 1790812800000, 'data': [{'seq': seq, 'pseq': seq-1,
                    'ts': '1790812800000', 'b': [['1', '1']], 'a': [['2', '1']]}]}
    return stamp + ' ' + json.dumps(data) + '\n'


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def events(self, venue, lines):
        path = self.root / (venue + '.gz')
        path.write_bytes(gzip.compress(''.join(lines).encode()))
        return iter_raw_file_events(path, venue=venue, exchange=('bitget-futures' if venue == 'bitget' else venue),
                                    date='2026-10-01', offset=0, symbol='BTWUSDT')

    def capture_fixture(self):
        def digest(data):return hashlib.sha256(data).hexdigest()
        def save(name,value):
            data=json.dumps(value).encode();(self.root/name).write_bytes(data);return digest(data)
        channels={'bybit':['orderbook.50','publicTrade'],'bitget-futures':['books','publicTrade']}
        config={'date':'2026-10-01','symbol':'BTWUSDT','slice_minutes':10,'offsets':list(range(0,1440,10)),
                'exchanges':channels,'api_key_used':False,'trust_env':False}
        config_hash=save('config.json',config)
        status_hash=save('status.json',{'state':'DOWNLOADED_AWAITING_INDEPENDENT_AUDIT'})
        files={};venues=[]
        for exchange in channels:
            (self.root/exchange).mkdir()
            checked=[]
            for offset in config['offsets']:
                venue='bitget' if exchange=='bitget-futures' else 'bybit'
                stamp=f'2026-10-01T{offset//60:02d}:{offset%60:02d}:00.1000000Z'
                body=frame(stamp,venue).encode();compressed=gzip.compress(body)
                name=f'{exchange}/offset{offset:04d}.jsonl.gz';(self.root/name).write_bytes(compressed)
                row={'state':'COMPLETE','status_code':200,'file':name,'exchange':exchange,'offset':offset,
                     'bytes':len(compressed),'sha256':digest(compressed),'body_sha256':digest(body),'line_count':1,
                     'params':{'from':'2026-10-01','offset':offset,'sliceSize':10,'compression':'gzip',
                               'filters':json.dumps([{'channel':c,'symbols':['BTWUSDT']} for c in channels[exchange]])}}
                files[name]=row
                checked.append({'path':str(Path(self.root.name)/name),'bytes':len(compressed),
                                'sha256':row['sha256'],'body_sha256':row['body_sha256'],'records':1,'gzip_crc':'PASS'})
            venues.append({'venue':exchange,'status':'PASS','counts':{},'examples':[],'files':checked})
        manifest_hash=save('manifest.json',{'state':'DOWNLOADED_AWAITING_INDEPENDENT_AUDIT',
                         'files':files,'complete_files':288,'expected_complete_files':288})
        audit_hash=save('audit.json',{'status':'PASS','execution_enabled':False,'symbol':'BTWUSDT','date_utc':'2026-10-01',
                                    'manifest_sha256':manifest_hash,'venues':venues,
                                    'coverage':{'config_sha256':config_hash,'status_sha256':status_hash,
                                                'full_day':True,'complete_file_set':True,'actual_files':288,
                                                'all_compressed_and_body_hashes_match_manifest':True,
                                                'all_fragment_local_timestamps_inside_requested_ranges':True,
                                                'total_compressed_bytes':sum(x['bytes'] for x in files.values())}})
        continuity_hash=save('continuity-audit.json',{'audit_sha256':audit_hash,'raw_integrity_status':'PASS'})
        save('replay-readiness.json',{'audit_sha256':audit_hash,'continuity_audit_sha256':continuity_hash,
                                    'raw_integrity_status':'PASS'})

    def test_exact_100ns_and_reject_bad_timestamp(self):
        self.assertEqual(parse_local_ns('2026-10-01T00:00:00.0000002Z') - parse_local_ns('2026-10-01T00:00:00.0000001Z'), 100)
        with self.assertRaises(ArchiveError):
            parse_local_ns('2026-10-01T00:00:00.1234567890Z')

    def test_heap_merge_same_ns_is_one_batch_before_decision(self):
        a = self.events('bybit', [frame('2026-10-01T00:00:00.0000001Z', 'bybit'),
                                  frame('2026-10-01T00:00:00.0000003Z', 'bybit', 'delta', 2)])
        b = self.events('bitget', [frame('2026-10-01T00:00:00.0000001Z', 'bitget'),
                                   frame('2026-10-01T00:00:00.0000002Z', 'bitget', 'update', 2)])
        batches = list(iter_batches(streams={'bybit':a, 'bitget':b}))
        self.assertEqual([len(x.events) for x in batches], [2,1,1])
        self.assertEqual(batches[1].ordering_ns-batches[0].ordering_ns,100)
        self.assertEqual({e.venue for e in batches[0].events},{'bybit','bitget'})

    def test_disconnect_suppresses_same_cursor_prior_snapshot(self):
        a = self.events('bybit', [frame('2026-10-01T00:00:00.1000000Z', 'bybit'), '\n',
                                  frame('2026-10-01T00:00:01.0000000Z', 'bybit', 'snapshot', 10)])
        batches = list(iter_batches(streams={'bybit':a}))
        self.assertEqual(batches[0].events[0].kind, 'disconnect')
        self.assertIsNone(batches[0].events[0].local_ns)
        self.assertIsNone(batches[0].events[0].local_timestamp)
        self.assertEqual([e.kind for e in batches[0].reconstruction_events], ['disconnect'])
        state=BookValidity()
        state.observe_batch(batches[0]);self.assertFalse(state.valid['bybit'])
        state.observe_batch(batches[1]);self.assertTrue(state.valid['bybit'])

    def test_same_ns_disconnect_then_new_snapshot_can_recover(self):
        stamp='2026-10-01T00:00:00.1000000Z'
        a=self.events('bybit',[frame(stamp,'bybit'), '\n', frame(stamp,'bybit','snapshot',10)])
        batch=next(iter_batches(streams={'bybit':a}));state=BookValidity();state.observe_batch(batch)
        self.assertTrue(state.valid['bybit'])
        self.assertEqual([e.kind for e in batch.reconstruction_events],['disconnect','book'])

    def test_tail_disconnect_keeps_book_invalid(self):
        a=self.events('bybit',[frame('2026-10-01T00:00:00.1000000Z','bybit'),
                               frame('2026-10-01T00:00:01.1000000Z','bybit','delta',2),'\n'])
        state=BookValidity()
        for batch in iter_batches(streams={'bybit':a}):state.observe_batch(batch)
        self.assertFalse(state.valid['bybit']);self.assertIsNone(state.last_book_ns['bybit'])

    def test_receive_reversal_and_delta_before_snapshot_rejected(self):
        a=self.events('bybit',[frame('2026-10-01T00:00:00.2000000Z','bybit'),
                               frame('2026-10-01T00:00:00.1000000Z','bybit','delta',2)])
        with self.assertRaises(ArchiveError):list(a)
        a=self.events('bitget',[frame('2026-10-01T00:00:00.2000000Z','bitget','update',2)])
        with self.assertRaises(ArchiveError):BookValidity().observe_batch(next(iter_batches(streams={'bitget':a})))

    def test_historical_trade_snapshot_not_current_and_native_order_preserved(self):
        data={'action':'snapshot','arg':{'instType':'usdt-futures','topic':'publicTrade','symbol':'BTWUSDT'},
              'data':[{'i':'2','T':'200','p':'2','v':'1','S':'buy'}, {'i':'1','T':'100','p':'1','v':'1','S':'sell'}]}
        e=list(self.events('bitget',['2026-10-01T00:00:00.1000000Z '+json.dumps(data)+'\n']))[0]
        self.assertEqual(e.kind,'historical_trade');self.assertEqual([x['T'] for x in e.native['data']],['200','100'])
        self.assertEqual(e.line_number,1);self.assertEqual(e.local_timestamp,'2026-10-01T00:00:00.1000000Z')

    def test_future_quote_forbidden_at_100ns_boundary(self):
        now=parse_local_ns('2026-10-01T00:00:00.1000000Z')
        assert_not_future(now,now,source_timestamp_ms=1790812800100)
        with self.assertRaises(ArchiveError):assert_not_future(now+100,now)
        with self.assertRaises(ArchiveError):assert_not_future(now,now,source_timestamp_ms=1790812800101)

    def test_leading_untimed_disconnect_is_not_given_fake_time(self):
        with self.assertRaises(ArchiveError):list(self.events('bybit',['\n']))

    def test_preflight_rejects_incorrect_hash_before_streaming(self):
        self.capture_fixture()
        audit=json.loads((self.root/'audit.json').read_text());audit['manifest_sha256']='0'*64
        (self.root/'audit.json').write_text(json.dumps(audit))
        with self.assertRaises(ArchiveError):preflight(self.root)

    def test_preflight_verifies_all_source_hashes(self):
        self.capture_fixture();archive=preflight(self.root)
        self.assertEqual(sum(len(x) for x in archive.files.values()),288)
        target=self.root/'bybit/offset0000.jsonl.gz';target.write_bytes(target.read_bytes()+b'changed')
        with self.assertRaisesRegex(ArchiveError,'SHA/size'):preflight(self.root)

    def test_missing_fragment_rejected(self):
        self.capture_fixture();(self.root/'bitget-futures/offset1430.jsonl.gz').unlink()
        with self.assertRaisesRegex(ArchiveError,'Missing'):preflight(self.root)

    def test_long_gap_does_not_generate_quotes_or_fake_events(self):
        stream=self.events('bybit',[frame('2026-10-01T00:00:01.0000000Z','bybit'),
                                    frame('2026-10-01T00:00:07.0000000Z','bybit','delta',2)])
        batches=list(iter_batches(streams={'bybit':stream}))
        self.assertEqual(len(batches),2)
        self.assertEqual(batches[1].ordering_ns-batches[0].ordering_ns,6_000_000_000)


if __name__=='__main__':unittest.main()
