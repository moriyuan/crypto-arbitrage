"""Read-only, streaming native Tardis adapter; no orders or trading decisions.

Preflight binds the completed capture, independent audit and readiness evidence.
Seven-digit receiver timestamps are represented exactly as integer nanoseconds.
Untimed disconnect markers keep local_ns=None and use an explicitly separate
ordering cursor, never an invented historical receiver timestamp.
"""
from __future__ import annotations

import calendar
import gzip
import hashlib
import heapq
import json
import re
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping


EXCHANGES = {'bybit': 'bybit', 'bitget': 'bitget-futures'}
CHANNELS = {'bybit': ['orderbook.50', 'publicTrade'], 'bitget-futures': ['books', 'publicTrade']}
CRITICAL_COUNTS = (
    'local_timestamp_regressions', 'invalid_source_record', 'empty_book',
    'crossed_or_locked_book', 'bitget_broken_pseq_chain', 'bybit_nonincreasing_delta_u',
    'bybit_nonincreasing_seq', 'bitget_nonincreasing_seq', 'conflicting_trade_id',
    'service_reset_without_snapshot', 'book_updates_while_uninitialized',
)


class ArchiveError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ArchiveError(message)


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    def invalid_constant(value):
        raise ArchiveError('Nonfinite JSON constant: ' + value)
    value = json.loads(path.read_text(), parse_constant=invalid_constant)
    require(isinstance(value, dict), f'Expected JSON object in {path}')
    return value


def _signature(path: Path) -> tuple[int, int, int, int]:
    s = path.stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns


@lru_cache(maxsize=4096)
def _second(text: str) -> int:
    try:
        return calendar.timegm(datetime.strptime(text, '%Y-%m-%dT%H:%M:%S').timetuple())
    except ValueError as exc:
        raise ArchiveError('Invalid local timestamp calendar fields') from exc


def parse_local_ns(text: str) -> int:
    require(isinstance(text, str) and re.fullmatch(
        r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{1,9}Z', text) is not None,
        'Expected exact UTC receiver timestamp with 1-9 fractional digits')
    return _second(text[:19]) * 10**9 + int(text[20:-1].ljust(9, '0'))


@dataclass(frozen=True)
class VerifiedFile:
    path: Path
    exchange: str
    offset: int
    bytes: int
    sha256: str
    body_sha256: str
    line_count: int
    signature: tuple[int, int, int, int]


@dataclass(frozen=True)
class Archive:
    root: Path
    symbol: str
    date: str
    files: Mapping[str, tuple[VerifiedFile, ...]]
    evidence_sha256: Mapping[str, str]
    startup_exception: Mapping[str, Any] | None = None


def preflight(root: Path | str, *, expected_symbol: str = 'BTWUSDT',
              expected_date: str = '2026-10-01', startup_evidence: Path | str | None = None) -> Archive:
    """Validate evidence bindings and every compressed input before any decision.

The audit's REVIEW is retained: known capture interruptions require segmented
state, while independent raw integrity is PASS. This is not a strategy gate.
    """
    root = Path(root).resolve()
    limited = startup_evidence is not None
    names = ('config.json', 'manifest.json', 'audit.json',
             'startup-replay-readiness.json' if limited else 'replay-readiness.json',
             'startup-continuity-audit.json' if limited else 'continuity-audit.json', 'status.json')
    evidence = {name: _sha(root / name) for name in names}
    documents = {name: _json(root / name) for name in names}
    config, manifest, audit, ready, continuity, status = (documents[name] for name in names)
    require(audit.get('manifest_sha256') == evidence['manifest.json'], 'Audit/manifest SHA binding mismatch')
    require(ready.get('audit_sha256') == evidence['audit.json'], 'Readiness/audit SHA binding mismatch')
    require(continuity.get('audit_sha256') == evidence['audit.json'], 'Continuity/audit SHA binding mismatch')
    require(ready.get('continuity_audit_sha256') == evidence[names[4]], 'Readiness/continuity SHA binding mismatch')
    coverage = audit.get('coverage', {})
    require(coverage.get('config_sha256') == evidence['config.json'], 'Audit/config SHA binding mismatch')
    require(coverage.get('status_sha256') == evidence['status.json'], 'Audit/status SHA binding mismatch')
    startup = None
    if limited:
        startup = _json(Path(startup_evidence))
        validate_startup_scope(startup, audit.get('venues', []), expected_date)
        require(ready.get('raw_integrity_status') == continuity.get('raw_integrity_status') == 'PASS_WITH_EXACT_STARTUP_ORPHAN', 'Missing limited raw acceptance')
        require(ready.get('status') == continuity.get('status') == 'STARTUP_ORPHAN_LIMITED_ACCEPTANCE', 'Wrong limited acceptance status')
        for document in (ready, continuity):
            require(document.get('startup_orphan_evidence_sha256') == _sha(Path(startup_evidence)), 'Startup evidence hash mismatch')
        for path, digest in startup['files_sha256'].items():
            require(Path(path).resolve().is_relative_to(root) and _sha(Path(path)) == digest, 'Startup original evidence hash mismatch')
        require(audit.get('status') in ('REVIEW','FAILED','FAIL') and startup.get('old_strict_preflight_status') == 'FAILED_PRESERVED', 'Original strict failure evidence was not preserved')
    else:
        require(ready.get('raw_integrity_status') == continuity.get('raw_integrity_status') == 'PASS', 'Raw integrity did not pass')
        require(audit.get('status') in ('PASS', 'REVIEW'), 'Unexpected audit state')
    require(audit.get('execution_enabled') is False, 'Unexpected audit execution state')
    require(config.get('symbol') == audit.get('symbol') == expected_symbol, 'Symbol mismatch')
    require(config.get('date') == audit.get('date_utc') == expected_date, 'Date mismatch')
    require(config.get('api_key_used') is False and config.get('trust_env') is False, 'Unexpected capture authorization configuration')
    require(config.get('slice_minutes') == 10 and config.get('offsets') == list(range(0, 1440, 10)), 'Incomplete day offsets')
    require(config.get('exchanges') == CHANNELS, 'Unexpected channels')
    require(manifest.get('state') == status.get('state') == 'DOWNLOADED_AWAITING_INDEPENDENT_AUDIT', 'Capture is not in its independently audited completed state')
    require(coverage.get('full_day') is True and coverage.get('complete_file_set') is True, 'Audit does not cover full day')
    require(coverage.get('all_compressed_and_body_hashes_match_manifest') is True and
            coverage.get('all_fragment_local_timestamps_inside_requested_ranges') is True, 'Audit file checks did not pass')
    expected = {f'{exchange}/offset{offset:04d}.jsonl.gz' for exchange in CHANNELS for offset in config['offsets']}
    actual = {str(p.relative_to(root)) for exchange in CHANNELS for p in (root/exchange).glob('*.jsonl.gz')}
    require(actual == expected == set(manifest.get('files', {})), 'Missing or unexpected source fragments')
    require(not list(root.rglob('*.part')) and not list(root.rglob('*.partial')), 'Incomplete download files remain')
    require(manifest.get('complete_files') == manifest.get('expected_complete_files') == coverage.get('actual_files') == 288, 'Capture file count mismatch')
    audited_files = {}
    venues = audit.get('venues', [])
    require({v.get('venue') for v in venues} == set(CHANNELS) and len(venues) == 2, 'Invalid audit venue coverage')
    for venue in venues:
        if not limited:
            require(not venue.get('examples') and not any(venue.get('counts', {}).get(k, 0) for k in CRITICAL_COUNTS), 'Audit contains unresolved structural errors')
            require(venue.get('status') in ('PASS', 'REVIEW'), 'Audit venue failed')
        for row in venue['files']:
            recorded = Path(row['path'])
            if recorded.is_absolute():
                path = recorded.resolve()
            else:
                require('..' not in recorded.parts and len(recorded.parts) >= 3 and
                        recorded.parts[-3] == root.name, 'Relative audit file has wrong archive root')
                path = (root / recorded.parts[-2] / recorded.parts[-1]).resolve()
            require(path.is_relative_to(root), 'Audited file belongs to a different root')
            name = str(path.relative_to(root))
            require(name not in audited_files, 'Duplicate audited file')
            audited_files[name] = row
    require(set(audited_files) == expected, 'Incomplete audit file coverage')
    files = {}
    for venue, exchange in EXCHANGES.items():
        selected = []
        for offset in config['offsets']:
            name = f'{exchange}/offset{offset:04d}.jsonl.gz'
            row, checked = manifest['files'][name], audited_files[name]
            require(row.get('state') == 'COMPLETE' and row.get('status_code') == 200 and row.get('file') == name, 'Incomplete fragment metadata')
            require(row.get('exchange') == exchange and row.get('offset') == offset, 'Fragment identity mismatch')
            params = row.get('params', {})
            require(params.get('from') == expected_date and params.get('offset') == offset and
                    params.get('sliceSize') == 10 and params.get('compression') == 'gzip', 'Fragment request mismatch')
            require(json.loads(params['filters']) == [{'channel':c, 'symbols':[expected_symbol]} for c in CHANNELS[exchange]], 'Fragment filter mismatch')
            for field in ('bytes','sha256','body_sha256'):
                require(row[field] == checked[field], f'Manifest/audit {field} mismatch')
            require(row['line_count'] == checked['records'] and checked['gzip_crc'] == 'PASS', 'Fragment line count/CRC audit mismatch')
            path = root/name
            signature = _signature(path)
            require(signature[2] == row['bytes'] and _sha(path) == row['sha256'], 'Actual fragment SHA/size mismatch: ' + name)
            require(_signature(path) == signature, 'Source changed during preflight: ' + name)
            selected.append(VerifiedFile(path, exchange, offset, row['bytes'], row['sha256'],
                                         row['body_sha256'], row['line_count'], signature))
        files[venue] = tuple(selected)
    require(sum(x.bytes for items in files.values() for x in items) == coverage.get('total_compressed_bytes'), 'Compressed byte total mismatch')
    for name, digest in evidence.items():
        require(_sha(root/name) == digest, 'Evidence changed during preflight: ' + name)
    if startup is not None:
        evidence['startup_orphan_evidence_sha256'] = _sha(Path(startup_evidence))
    return Archive(root, expected_symbol, expected_date, files, evidence, startup)


@dataclass(frozen=True)
class NativeEvent:
    venue: str
    exchange: str
    kind: str
    local_timestamp: str | None
    local_ns: int | None
    ordering_ns: int
    ordering_basis: str
    native: dict[str, Any] | None
    path: Path
    offset: int
    line_number: int
    source_sequence: int
    raw_line: bytes

    @property
    def is_snapshot(self) -> bool:
        return self.kind == 'book' and bool(self.native) and (
            self.native.get('type') == 'snapshot' if self.venue == 'bybit'
            else self.native.get('action') == 'snapshot')


def _classify(native: dict[str, Any], venue: str, symbol: str) -> str:
    if venue == 'bybit':
        topic = native.get('topic')
        if topic is None:
            require(native.get('op') in ('subscribe','ping','pong') and native.get('success') is not False, 'Unexpected Bybit control frame')
            return 'control'
        require(topic in (f'orderbook.50.{symbol}', f'publicTrade.{symbol}'), 'Unexpected Bybit symbol/channel')
        if topic.startswith('orderbook.'):
            require(native.get('data', {}).get('s') == symbol and native.get('type') in ('snapshot','delta'), 'Invalid Bybit book identity')
            return 'book'
        require(all(x.get('s') == symbol for x in native['data']), 'Unexpected Bybit trade symbol')
        return 'trade'
    arg = native.get('arg', {})
    require(arg.get('instType') == 'usdt-futures' and arg.get('symbol') == symbol and
            arg.get('topic') in ('books','publicTrade'), 'Unexpected Bitget symbol/channel')
    if 'event' in native:
        require(native['event'] == 'subscribe', 'Bitget control error')
        return 'control'
    require(native.get('action') in ('snapshot','update'), 'Invalid Bitget data action')
    if arg['topic'] == 'books':
        return 'book'
    return 'historical_trade' if native['action'] == 'snapshot' else 'trade'


def iter_raw_file_events(path: Path | str, *, venue: str, exchange: str, date: str,
                         offset: int, symbol: str, previous_ns: int | None = None,
                         start_sequence: int = 0, verified: VerifiedFile | None = None) -> Iterator[NativeEvent]:
    """Low-level reader; production callers should obtain files from preflight."""
    path = Path(path)
    require(EXCHANGES.get(venue) == exchange, 'Venue/exchange mismatch')
    signature = _signature(path)
    if verified is not None:
        require(signature == verified.signature, 'Source changed after preflight')
    start_ns = parse_local_ns(date+'T00:00:00.0000000Z') + offset * 60 * 10**9
    end_ns = start_ns + 10 * 60 * 10**9
    body_hash = hashlib.sha256()
    count = 0
    with gzip.open(path, 'rb') as source:
        for count, raw_line in enumerate(source, 1):
            body_hash.update(raw_line)
            text = raw_line.decode('utf-8').rstrip('\r\n')
            stamp, separator, message = text.partition(' ')
            sequence = start_sequence + count
            if not text:
                require(previous_ns is not None, 'Untimed leading disconnect has no ordering cursor')
                yield NativeEvent(venue, exchange, 'disconnect', None, None, previous_ns,
                                  'preceding_local_timestamp_ordering_cursor_only', None,
                                  path, offset, count, sequence, raw_line)
                continue
            ns = parse_local_ns(stamp)
            require(start_ns <= ns < end_ns, 'Receiver timestamp outside requested fragment')
            require(previous_ns is None or ns >= previous_ns, 'Receiver timestamp regression')
            previous_ns = ns
            if not separator or not message:
                yield NativeEvent(venue, exchange, 'disconnect', stamp, ns, ns,
                                  'native_timestamp_only_disconnect', None,
                                  path, offset, count, sequence, raw_line)
                continue
            native = json.loads(message, parse_constant=lambda x: (_ for _ in ()).throw(ArchiveError('Nonfinite JSON')))
            require(isinstance(native, dict), 'Native frame must be an object')
            kind = _classify(native, venue, symbol)
            yield NativeEvent(venue, exchange, kind, stamp, ns, ns, 'native_local_timestamp',
                              native, path, offset, count, sequence, raw_line)
    require(_signature(path) == signature, 'Source changed during streaming')
    if verified is not None:
        require(count == verified.line_count and body_hash.hexdigest() == verified.body_sha256, 'Streamed body no longer matches independent audit')


def iter_venue(archive: Archive, venue: str) -> Iterator[NativeEvent]:
    require(venue in EXCHANGES, 'Unsupported canonical venue')
    previous_ns = None
    source_sequence = 0
    for file in archive.files[venue]:
        for event in iter_raw_file_events(file.path, venue=venue, exchange=file.exchange,
                                         date=archive.date, offset=file.offset, symbol=archive.symbol,
                                         previous_ns=previous_ns, start_sequence=source_sequence, verified=file):
            if event.local_ns is not None:
                previous_ns = event.local_ns
            source_sequence = event.source_sequence
            yield event


@dataclass(frozen=True)
class EventBatch:
    ordering_ns: int
    events: tuple[NativeEvent, ...]

    @property
    def reconstruction_events(self) -> tuple[NativeEvent, ...]:
        """All disconnects first; suppress same-cursor frames that preceded them.

All originals remain in events. A post-marker snapshot at the exact same
receiver timestamp may recover. A pre-marker snapshot may not revive the book.
        """
        cutoff = {venue: max(e.source_sequence for e in self.events
                             if e.venue == venue and e.kind == 'disconnect')
                  for venue in {e.venue for e in self.events if e.kind == 'disconnect'}}
        return tuple(e for e in self.events if e.kind == 'disconnect' or
                     e.source_sequence > cutoff.get(e.venue, -1))


def _iter_batches(archive: Archive | None = None, *,
                 streams: Mapping[str, Iterable[NativeEvent]] | None = None) -> Iterator[EventBatch]:
    """Merge monotonic venue streams and expose one decision boundary per ns."""
    require((archive is None) != (streams is None), 'Provide archive or explicit test streams')
    streams = {v: iter_venue(archive,v) for v in EXCHANGES} if archive is not None else streams
    iterators = {v: iter(events) for v, events in streams.items()}
    heap = []
    last_by_venue = {}
    def advance(venue):
        event = next(iterators[venue], None)
        if event is None:
            return
        require(event.venue == venue, 'Merged stream venue mismatch')
        require(event.ordering_ns >= last_by_venue.get(venue, -1), 'Merged stream ordering cursor regressed')
        last_by_venue[venue] = event.ordering_ns
        heapq.heappush(heap, (event.ordering_ns, venue, event.source_sequence, event))
    for venue in iterators:
        advance(venue)
    while heap:
        cursor = heap[0][0]
        batch = []
        while heap and heap[0][0] == cursor:
            _, venue, _, event = heapq.heappop(heap)
            batch.append(event)
            advance(venue)
        batch.sort(key=lambda e: (e.kind != 'disconnect', e.venue, e.source_sequence))
        yield EventBatch(cursor, tuple(batch))


def assert_not_future(quote_local_ns: int, decision_ns: int, *, source_timestamp_ms: int | None = None) -> None:
    require(type(quote_local_ns) is int and type(decision_ns) is int and quote_local_ns <= decision_ns,
            'Future receiver-time quote is forbidden')
    if source_timestamp_ms is not None:
        require(type(source_timestamp_ms) is int and source_timestamp_ms * 10**6 <= decision_ns,
                'Future exchange-time quote is forbidden')


class BookValidity:
    """Connection readiness only; the runner must still validate native books."""
    def __init__(self):
        self.valid = dict.fromkeys(EXCHANGES, False)
        self.last_book_ns = dict.fromkeys(EXCHANGES)
        self.generation = dict.fromkeys(EXCHANGES, 0)

    def observe_batch(self, batch: EventBatch) -> None:
        for event in batch.reconstruction_events:
            if event.kind == 'disconnect':
                self.valid[event.venue] = False
                self.last_book_ns[event.venue] = None
                self.generation[event.venue] += 1
            elif event.kind == 'book':
                require(event.local_ns is not None, 'Book lacks native receiver time')
                assert_not_future(event.local_ns, batch.ordering_ns)
                require(event.is_snapshot or self.valid[event.venue], 'Book delta before recovery snapshot')
                self.valid[event.venue] = True
                self.last_book_ns[event.venue] = event.local_ns


def validate_startup_scope(startup, venues, expected_date):
    require(startup.get('status') == 'EXACT_STARTUP_ORPHAN_PROVEN' and startup.get('execution_enabled') is False, 'Invalid startup evidence')
    require(startup.get('date') == expected_date == '2026-08-01', 'Startup exception is limited to verified Aug1 source')
    require(len(venues) == 2 and {v['venue'] for v in venues} == set(CHANNELS), 'Invalid startup audit venues')
    for venue in venues:
        require(not venue.get('examples'), 'Unresolved startup audit examples')
        for key in CRITICAL_COUNTS:
            expected = int(venue['venue'] == 'bybit' and key == 'book_updates_while_uninitialized')
            require(venue.get('counts', {}).get(key, 0) == expected, 'Additional structural error outside startup exception')
    orphan, snapshot, repeat = (startup[k] for k in ('orphan','first_snapshot','post_snapshot_identical_delta'))
    require((orphan['line'], snapshot['line'], repeat['line']) == (2,3,12), 'Startup original line mismatch')
    require(orphan['receipt_timestamp'] == '2026-08-01T00:00:00.0708039Z' and snapshot['receipt_timestamp'] == '2026-08-01T00:00:00.1954482Z' and repeat['receipt_timestamp'] == '2026-08-01T00:00:00.1954512Z', 'Startup source time mismatch')
    require(orphan['source_message'] == repeat['source_message'] and orphan['source_message']['type'] == 'delta' and snapshot['source_message']['type'] == 'snapshot', 'Startup replay proof mismatch')
    require(startup['earliest_permitted_bybit_activity_from_initial_snapshot_ns'] == parse_local_ns(snapshot['receipt_timestamp']) + 60_000_000_000, 'Startup warmup mismatch')


@dataclass(frozen=True)
class StartupBatch(EventBatch):
    omitted: tuple[tuple[str, int], ...] = ()
    @property
    def reconstruction_events(self):
        return tuple(e for e in super().reconstruction_events if (e.venue, e.source_sequence) not in self.omitted)


def apply_startup_guard(batches, evidence):
    """Retain all raw records/counts; discard only the exact proven orphan."""
    expected={evidence[label]['line']:(label,evidence[label]) for label in ('orphan','first_snapshot','post_snapshot_identical_delta')}
    seen=set()
    for batch in batches:
        omitted=[]
        for event in batch.events:
            if event.venue != 'bybit' or event.offset != 0 or event.line_number not in expected:continue
            label,row=expected[event.line_number]
            require(label not in seen and event.kind == 'book' and event.local_timestamp == row['receipt_timestamp'] and event.native == row['source_message'], 'Startup exact source row mismatch')
            seen.add(label)
            if label == 'orphan':omitted.append((event.venue,event.source_sequence))
        yield StartupBatch(batch.ordering_ns,batch.events,tuple(omitted))
    require(seen == {'orphan','first_snapshot','post_snapshot_identical_delta'}, 'Proven startup rows not observed in full source')


def iter_batches(archive=None, *, streams=None):
    batches=_iter_batches(archive,streams=streams)
    startup=getattr(archive,'startup_exception',None)
    yield from apply_startup_guard(batches,startup) if startup else batches


def startup_activity_ready(archive,venue,ns):
    startup=getattr(archive,'startup_exception',None)
    return not startup or venue != 'bybit' or ns >= parse_local_ns(startup['first_snapshot']['receipt_timestamp'])


def startup_entry_ready(archive,ns):
    startup=getattr(archive,'startup_exception',None)
    return not startup or ns >= startup['earliest_permitted_bybit_activity_from_initial_snapshot_ns']
