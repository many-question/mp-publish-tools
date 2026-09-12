"""Incremental JSONL projection and transactional outbox, private to one workspace."""
import base64
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from common import atomic, encode, inside, local_state
from projection import VERSION, check_text, project

MAX_RECORD = 64 * 1024 * 1024
CHUNK_BYTES = 256 * 1024
PUBLISHER_RELEASE = hashlib.sha256((Path(__file__).parent / 'release.json').read_bytes()).hexdigest()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def connect(root):
    state = local_state(root)
    state.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(inside(root, state / 'state.sqlite'))
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA synchronous=FULL')
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version not in {0, 1}:
        raise ValueError('Unsupported state version; no migration attempted')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources(
          id TEXT PRIMARY KEY, path TEXT NOT NULL, generation TEXT NOT NULL,
          identity TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
          snapshot_end INTEGER NOT NULL, baseline_done INTEGER NOT NULL DEFAULT 0,
          prefix_n INTEGER NOT NULL DEFAULT 0, prefix_hash TEXT, boundary_hash TEXT,
          observed_size INTEGER NOT NULL, error TEXT);
        CREATE TABLE IF NOT EXISTS outbox(
          id TEXT PRIMARY KEY, data BLOB NOT NULL, sha256 TEXT NOT NULL,
          exported INTEGER NOT NULL DEFAULT 0, published INTEGER NOT NULL DEFAULT 0);
        PRAGMA user_version=1;
    ''')
    return db


def identity(stat):
    return json.dumps([stat.st_dev, stat.st_ino])


def source_path(root, cfg_source):
    path = Path(cfg_source['path']).resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != '.jsonl':
        raise ValueError('Source must be an explicitly bound JSONL file')
    # A source may be a native transcript outside the workspace, explicitly bound
    # by the operator. It must never be our own generated state/output.
    for excluded in (local_state(root), Path(root) / 'share'):
        if path.is_relative_to(excluded.resolve()):
            raise ValueError('Cannot collect publisher state or share as a transcript')
    return path


def queue(db, events):
    parts, current, length = [], [], 0
    for event in events:
        event['publisher_release'] = PUBLISHER_RELEASE
        raw = encode(event)
        if len(raw) + 1 > CHUNK_BYTES:
            # User text remains recoverable exactly; no semantic shortening.
            width = 96 * 1024
            count = (len(raw) + width - 1) // width
            records = [encode({'schema': 'mp-event-fragment/1', 'event_sha256': sha(raw),
                       'part': n, 'parts': count, 'encoding': 'base64',
                       'data': base64.b64encode(raw[n*width:(n+1)*width]).decode('ascii')})
                       for n in range(count)]
        else:
            records = [raw]
        for record in records:
            if current and length + len(record) + 1 > CHUNK_BYTES:
                parts.append(b''.join(current))
                current, length = [], 0
            current.append(record + b'\n')
            length += len(record) + 1
    if current:
        parts.append(b''.join(current))
    for raw in parts:
        cid = uuid.uuid4().hex
        db.execute('INSERT INTO outbox(id,data,sha256) VALUES (?,?,?)', (cid, raw, sha(raw)))


def initialize(root, cfg, db):
    binding = encode({'workspace': cfg['workspace'], 'publisher_id': cfg['publisher_id'],
                      'run_id': cfg['run_id'], 'framework': cfg['framework']}).decode()
    prior = db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()
    if prior and prior[0] != binding:
        raise ValueError('State belongs to another workspace/run; refusing shared state')
    with db:
        db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('binding', binding))
        for src in cfg['sources']:
            old = db.execute('SELECT * FROM sources WHERE id=?', (src['id'],)).fetchone()
            if old:
                if old['path'] != src['path']:
                    raise ValueError('Existing source binding changed; add a new binding explicitly')
                continue
            path = source_path(root, src)
            stat = path.stat()
            generation = uuid.uuid4().hex
            db.execute('INSERT INTO sources(id,path,generation,identity,snapshot_end,observed_size) VALUES (?,?,?,?,?,?)',
                       (src['id'], str(path), generation, identity(stat), stat.st_size, stat.st_size))
            queue(db, [{'schema': 'mp-source-observation/1', 'run_id': cfg['run_id'],
                'publisher_id': cfg['publisher_id'], 'source_id': src['id'], 'generation': generation,
                'source_name': path.name,
                'observation': 'initial_snapshot', 'observed_bytes': stat.st_size,
                'snapshot_end': stat.st_size, 'collected_at': time.time(), 'projection_version': VERSION}])


def restart_backfill(root, cfg, db):
    if db.execute('SELECT count(*) FROM outbox WHERE published=0').fetchone()[0]:
        raise ValueError('Finish pending delivery with --backfill before restarting')
    snapshots = [(db.execute('SELECT * FROM sources WHERE id=?', (s['id'],)).fetchone(),
                  source_path(root, s).stat()) for s in cfg['sources']]
    with db:
        for src, stat in snapshots:
            generation = uuid.uuid4().hex
            queue(db, [{'schema': 'mp-source-observation/1', 'run_id': cfg['run_id'],
                'publisher_id': cfg['publisher_id'], 'source_id': src['id'],
                'generation': generation, 'previous_generation': src['generation'],
                'observation': 'operator_backfill_restart', 'previous_cursor': src['cursor'],
                'observed_bytes': stat.st_size, 'snapshot_end': stat.st_size,
                'collected_at': time.time(), 'projection_version': VERSION}])
            db.execute('''UPDATE sources SET generation=?,identity=?,cursor=0,snapshot_end=?,
                observed_size=?,baseline_done=0,prefix_n=0,prefix_hash=NULL,boundary_hash=NULL,
                error=NULL WHERE id=?''',
                (generation, identity(stat), stat.st_size, stat.st_size, src['id']))
        db.execute("DELETE FROM meta WHERE key='bootstrapped'")


def fingerprints(fh, cursor, prefix_n=None):
    prefix_n = min(cursor, 256) if prefix_n is None else prefix_n
    fh.seek(0)
    prefix = sha(fh.read(prefix_n))
    fh.seek(max(0, cursor - 256))
    boundary = sha(fh.read(min(256, cursor)))
    return prefix_n, prefix, boundary


def reset_changed(db, cfg, src, stat, reason):
    generation = uuid.uuid4().hex
    with db:
        queue(db, [{'schema': 'mp-source-observation/1', 'run_id': cfg['run_id'],
            'publisher_id': cfg['publisher_id'], 'source_id': src['id'],
            'generation': generation, 'previous_generation': src['generation'],
            'observation': reason, 'previous_cursor': src['cursor'], 'observed_bytes': stat.st_size,
            'snapshot_end': stat.st_size, 'collected_at': time.time(), 'projection_version': VERSION}])
        db.execute('''UPDATE sources SET generation=?,identity=?,cursor=0,snapshot_end=?,
                      observed_size=?,baseline_done=0,prefix_n=0,prefix_hash=NULL,boundary_hash=NULL,
                      error='source changed; operator backfill required' WHERE id=?''',
                   (generation, identity(stat), stat.st_size, stat.st_size, src['id']))


def collect(root, cfg, db, backfill, seconds=3, byte_budget=4*1024*1024):
    deadline = time.monotonic() + seconds
    read_bytes = 0
    bindings = cfg['sources']
    rotation = db.execute("SELECT value FROM meta WHERE key='next_source'").fetchone()
    offset = int(rotation[0]) % len(bindings) if rotation and not backfill else 0
    ordered = bindings[offset:] + bindings[:offset]
    for bound in ordered:
        src = db.execute('SELECT * FROM sources WHERE id=?', (bound['id'],)).fetchone()
        if src is None:
            raise ValueError('New source requires --backfill')
        if backfill and src['baseline_done']:
            continue
        if not backfill and not src['baseline_done']:
            continue
        if time.monotonic() >= deadline or read_bytes >= byte_budget:
            break
        if not backfill:
            with db:
                db.execute("INSERT OR REPLACE INTO meta VALUES ('next_source',?)", (str((bindings.index(bound)+1) % len(bindings)),))
        try:
            path = source_path(root, bound)
            stat = path.stat()
            with path.open('rb') as fh:
                changed = identity(stat) != src['identity'] or stat.st_size < src['observed_size']
                if src['cursor'] and not changed:
                    _, prefix, boundary = fingerprints(fh, src['cursor'], src['prefix_n'])
                    changed = prefix != src['prefix_hash'] or boundary != src['boundary_hash']
                if changed:
                    reset_changed(db, cfg, src, stat, 'file_identity_or_boundary_changed')
                    continue
                end = min(stat.st_size, src['snapshot_end']) if backfill else stat.st_size
                cursor = src['cursor']
                events = []
                tail = False
                fh.seek(cursor)
                while cursor < end and read_bytes < byte_budget and time.monotonic() < deadline:
                    start = cursor
                    raw = fh.readline(min(MAX_RECORD + 1, end - cursor))
                    if len(raw) > MAX_RECORD:
                        raise ValueError('A JSONL record exceeds the 64 MiB limit; no bytes skipped')
                    if not raw.endswith(b'\n'):
                        tail = True
                        break
                    native = json.loads(raw.decode('utf-8'))
                    event = project(native)
                    event.update(schema='mp-session-event/1', run_id=cfg['run_id'],
                                 publisher_id=cfg['publisher_id'], framework=cfg['framework'],
                                 source_id=src['id'], generation=src['generation'],
                                 byte_start=start, byte_end=start+len(raw), collected_at=time.time())
                    text = encode(event).decode('utf-8')
                    from transport import SECRET_PATTERNS
                    check_text(text, SECRET_PATTERNS)
                    events.append(event)
                    cursor += len(raw)
                    read_bytes += len(raw)
                done = backfill and (cursor >= end or tail)
                prefix_n, prefix, boundary = fingerprints(fh, cursor)
                # Detect replacement/truncation during the read before committing a cursor.
                after = path.stat()
                if identity(after) != identity(stat) or after.st_size < cursor:
                    raise ValueError('Source changed during read; batch was not committed')
                if done:
                    events.append({'schema': 'mp-source-observation/1', 'run_id': cfg['run_id'],
                        'publisher_id': cfg['publisher_id'], 'source_id': src['id'], 'generation': src['generation'],
                        'observation': 'snapshot_scan_finished', 'snapshot_end': src['snapshot_end'],
                        'complete_record_end': cursor, 'unframed_tail_bytes': end-cursor,
                        'collected_at': time.time(), 'projection_version': VERSION})
                elif not backfill:
                    events.append({'schema': 'mp-source-observation/1', 'run_id': cfg['run_id'],
                        'publisher_id': cfg['publisher_id'], 'source_id': src['id'], 'generation': src['generation'],
                        'observation': 'incremental_scan', 'read_start': src['cursor'],
                        'complete_record_end': cursor, 'observed_bytes': after.st_size,
                        'unframed_tail_observed': tail, 'collected_at': time.time(), 'projection_version': VERSION})
                with db:
                    queue(db, events)
                    db.execute('''UPDATE sources SET cursor=?,prefix_n=?,prefix_hash=?,boundary_hash=?,
                               observed_size=?,baseline_done=?,error=NULL WHERE id=?''',
                               (cursor, prefix_n, prefix, boundary, after.st_size,
                                int(done or src['baseline_done']), src['id']))
        except (OSError, ValueError, RecursionError) as exc:
            # No invalid record or secret is printed, exported, or counted as consumed.
            error = type(exc).__name__ + ': ' + (str(exc) if not isinstance(exc, json.JSONDecodeError) else 'invalid JSON record')
            with db:
                if error[:240] != src['error']:
                    queue(db, [{'schema': 'mp-source-observation/1', 'run_id': cfg['run_id'],
                        'publisher_id': cfg['publisher_id'], 'source_id': src['id'],
                        'generation': src['generation'], 'observation': 'read_or_projection_error',
                        'committed_cursor': src['cursor'], 'error_type': type(exc).__name__,
                        'error_message': error[:240],
                        'collected_at': time.time(), 'projection_version': VERSION}])
                db.execute('UPDATE sources SET error=? WHERE id=?', (error[:240], src['id']))
    return read_bytes


def status(db, cfg):
    rows = [dict(r) for r in db.execute('SELECT id,generation,cursor,snapshot_end,baseline_done,observed_size,error FROM sources')]
    known = {r['id'] for r in rows}
    missing = [s['id'] for s in cfg['sources'] if s['id'] not in known]
    pending = db.execute('SELECT count(*) FROM outbox WHERE published=0').fetchone()[0]
    ready = bool(rows) and not missing and all(r['baseline_done'] and not r['error'] for r in rows)
    return {'sources': rows, 'uninitialized_sources': missing, 'snapshot_scan_done': ready,
            'pending_chunks': pending, 'backfill_published': ready and pending == 0}


def chunk_path(root, cfg, cid):
    folder = inside(root, Path(root) / 'share' / 'telemetry' / cfg['publisher_id'])
    legacy = inside(root, folder / (cid + '.jsonl'))
    compressed = inside(root, folder / (cid + '.jsonl.gz'))
    if legacy.exists() and compressed.exists():
        raise ValueError('A chunk has conflicting plain and compressed delivery paths')
    return legacy if legacy.exists() else compressed


def delivery_bytes(dest, raw):
    return gzip.compress(raw, compresslevel=6, mtime=0) if dest.name.endswith('.gz') else raw


def export_batch(root, cfg, db, max_bytes=1024*1024):
    used, plan = 0, []
    for row in db.execute('SELECT * FROM outbox WHERE published=0 ORDER BY rowid'):
        if plan and used + len(row['data']) > max_bytes:
            break
        if sha(row['data']) != row['sha256']:
            raise ValueError('Outbox checksum mismatch')
        dest = chunk_path(root, cfg, row['id'])
        data = delivery_bytes(dest, row['data'])
        if dest.exists():
            if dest.read_bytes() != data:
                raise ValueError('Published chunk filename has conflicting content')
        plan.append((row, dest))
        used += len(row['data'])
    from transport import check_sizes
    files = [dest for row, dest in plan if dest.exists()]
    if check_sizes(files):
        raise ValueError('share file size check failed; pending chunks retained locally')
    for row, dest in plan:
        if not dest.exists():
            atomic(dest, delivery_bytes(dest, row['data']))
    ids = [row['id'] for row, _ in plan]
    with db:
        db.executemany('UPDATE outbox SET exported=1 WHERE id=?', [(cid,) for cid in ids])
    return ids


def acknowledge(db, ids):
    with db:
        db.executemany('UPDATE outbox SET published=1 WHERE id=?', [(cid,) for cid in ids])
