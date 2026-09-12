"""Streaming projection with durable local checkpoints; Git owns delivery state."""
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
import zlib

from common import atomic, encode, inside, local_state
from projection import VERSION, check_text, project

MAX_RECORD = 64 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
CHUNK_BYTES = 2 * 1024 * 1024  # compressed output watermark, not input bytes
FRAGMENT_BYTES = 128 * 1024
PUBLISHER_RELEASE = hashlib.sha256((Path(__file__).parent / 'release.json').read_bytes()).hexdigest()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def binding(cfg):
    return encode({k: cfg[k] for k in ('workspace', 'publisher_id', 'run_id', 'framework')}).decode()


def connect(root, cfg=None, read_only=False):
    state = local_state(root)
    state.mkdir(parents=True, exist_ok=True)
    path = inside(root, state / 'state.sqlite')
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) if read_only else sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version not in (0, 1, 2):
        db.close()
        raise ValueError('Unsupported state version')
    if version and cfg:
        old = db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()
        empty = not old and version == 2 and all(db.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0
                                                for table in ('sources', 'local_files', 'observations'))
        if not empty and (not old or old[0] != binding(cfg)):
            db.close()
            raise ValueError('State belongs to another workspace/run')
    if read_only:
        return db
    db.execute('PRAGMA synchronous=FULL')
    if version == 1:
        # A complete SQLite backup precedes the one-way migration. Old runtimes
        # reject user_version=2 rather than silently consuming the new state.
        backup = inside(root, state / ('state-v1-' + uuid.uuid4().hex + '.sqlite'))
        saved = sqlite3.connect(backup)
        try:
            db.backup(saved)
        finally:
            saved.close()
    if version == 2:
        return db
    try:
        db.execute("BEGIN IMMEDIATE")
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS sources(
              id TEXT PRIMARY KEY, path TEXT NOT NULL, generation TEXT NOT NULL,
              identity TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
              snapshot_end INTEGER NOT NULL, baseline_done INTEGER NOT NULL DEFAULT 0,
              prefix_n INTEGER NOT NULL DEFAULT 0, prefix_hash TEXT, boundary_hash TEXT,
              observed_size INTEGER NOT NULL, error TEXT)''')
            db.execute('CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY, data BLOB NOT NULL)')
            # This is ONLY a local file-install journal. No exported/published flags.
            db.execute('CREATE TABLE IF NOT EXISTS local_files(path TEXT PRIMARY KEY, data BLOB NOT NULL, sha256 TEXT NOT NULL)')
            if version == 1:
                if cfg is None:
                    raise ValueError('Migration requires the verified workspace binding')
                for row in db.execute('SELECT * FROM outbox WHERE published=0'):
                    if sha(row['data']) != row['sha256']:
                        raise ValueError('Legacy outbox checksum mismatch')
                    folder = Path(root) / 'share/telemetry' / cfg['publisher_id']
                    plain = inside(root, folder / (row['id'] + '.jsonl'))
                    gz = inside(root, folder / (row['id'] + '.jsonl.gz'))
                    if plain.exists() and gz.exists():
                        raise ValueError('Conflicting legacy delivery paths')
                    dest = plain if plain.exists() else gz
                    data = row['data'] if dest == plain else gzip.compress(row['data'], compresslevel=6, mtime=0)
                    if dest.exists():
                        existing = dest.read_bytes()
                        if dest == plain:
                            restored = existing
                        else:
                            with gzip.open(dest, 'rb') as fh:
                                restored = fh.read(len(row['data']) + 1)
                        if restored != row['data']:
                            raise ValueError('Legacy delivery file changed')
                        data = existing
                    db.execute('INSERT INTO local_files VALUES (?,?,?)',
                               (dest.relative_to(Path(root) / 'share').as_posix(), data, sha(data)))
                db.execute('DROP TABLE outbox')
            db.execute('PRAGMA user_version=2')
    except BaseException:
        db.close()
        raise
    return db


def identity(stat):
    return json.dumps([stat.st_dev, stat.st_ino])


def source_path(root, source):
    path = Path(source['path']).resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != '.jsonl':
        raise ValueError('Source must be an explicitly bound JSONL file')
    for excluded in (local_state(root), Path(root) / 'share'):
        if path.is_relative_to(excluded.resolve()):
            raise ValueError('Cannot collect publisher state or share as a transcript')
    return path


def observe(db, cfg, src, kind, **extra):
    event = dict(schema='mp-source-observation/1', run_id=cfg['run_id'],
                 publisher_id=cfg['publisher_id'], source_id=src['id'], generation=src['generation'],
                 observation=kind, collected_at=time.time(), projection_version=VERSION, **extra)
    db.execute('INSERT INTO observations(data) VALUES (?)', (encode(event),))


def initialize(root, cfg, db):
    with db:
        prior = db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()
        if prior and prior[0] != binding(cfg):
            raise ValueError('State belongs to another workspace/run')
        db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('binding', binding(cfg)))
        for src in cfg['sources']:
            old = db.execute('SELECT * FROM sources WHERE id=?', (src['id'],)).fetchone()
            if old:
                if old['path'] != src['path']:
                    raise ValueError('Source binding changed; add a new source explicitly')
                continue
            path = source_path(root, src)
            stat = path.stat()
            generation = uuid.uuid4().hex
            db.execute('INSERT INTO sources(id,path,generation,identity,snapshot_end,observed_size) VALUES (?,?,?,?,?,?)',
                       (src['id'], str(path), generation, identity(stat), stat.st_size, stat.st_size))
            observe(db, cfg, dict(src, generation=generation), 'initial_snapshot',
                    source_name=path.name, observed_bytes=stat.st_size, snapshot_end=stat.st_size)


def restart_backfill(root, cfg, db):
    with db:
        for src in cfg['sources']:
            old = db.execute('SELECT * FROM sources WHERE id=?', (src['id'],)).fetchone()
            stat = source_path(root, src).stat()
            generation = uuid.uuid4().hex
            observe(db, cfg, dict(src, generation=generation), 'operator_backfill_restart',
                    previous_generation=old['generation'], previous_cursor=old['cursor'],
                    observed_bytes=stat.st_size, snapshot_end=stat.st_size)
            db.execute('''UPDATE sources SET generation=?,identity=?,cursor=0,snapshot_end=?,
                observed_size=?,baseline_done=0,prefix_n=0,prefix_hash=NULL,boundary_hash=NULL,error=NULL WHERE id=?''',
                (generation, identity(stat), stat.st_size, stat.st_size, src['id']))
        db.execute("DELETE FROM meta WHERE key='bootstrapped'")


def fingerprints(fh, cursor, prefix_n=None):
    pos = fh.tell()
    n = min(cursor, 256) if prefix_n is None else prefix_n
    fh.seek(0)
    prefix = sha(fh.read(n))
    fh.seek(max(0, cursor - 256))
    boundary = sha(fh.read(min(256, cursor)))
    fh.seek(pos)
    return n, prefix, boundary


def materialize(root, db):
    """Idempotent local recovery. Cursor already has durable compressed bytes in SQLite."""
    while True:
        row = db.execute('SELECT * FROM local_files LIMIT 1').fetchone()
        if row is None:
            break
        path = inside(root, Path(root) / 'share' / row['path'])
        if not path.is_relative_to((Path(root) / 'share/telemetry').resolve()):
            raise ValueError('Local file journal escaped telemetry')
        if sha(row['data']) != row['sha256']:
            raise ValueError('Local checkpoint checksum mismatch')
        if path.exists():
            if path.read_bytes() != row['data']:
                raise ValueError('Local checkpoint has conflicting file contents')
        else:
            atomic(path, row['data'])
        with db:
            db.execute('DELETE FROM local_files WHERE path=?', (row['path'],))


class Stream:
    def __init__(self, root, cfg, db, states, watermark, checkpoint_seconds):
        self.root, self.cfg, self.db, self.states = root, cfg, db, states
        self.watermark, self.checkpoint_seconds = watermark, checkpoint_seconds
        self.file = None
        self.sealed = []
        self.in_event = False
        self.dirty = False
        self.observation_ids = []
        self.files = self.compressed = self.expanded_total = 0
        self.last_checkpoint = time.perf_counter()
        self.temp = inside(root, local_state(root) / 'scanning.tmp.gz')
        # A leftover temp was never committed with a cursor; it can be overwritten.
        if self.temp.exists():
            self.temp.unlink()

    def open(self):
        if self.file is None:
            self.file = self.temp.open('wb')
            self.encoder = zlib.compressobj(6, zlib.DEFLATED, 31)
            self.expanded = 0

    def line(self, raw):
        if self.file and self.expanded + len(raw) > MAX_EXPANDED:
            self.checkpoint()
        self.open()
        self.file.write(self.encoder.compress(raw))
        self.expanded += len(raw)

    def event(self, event):
        event['publisher_release'] = PUBLISHER_RELEASE
        raw = encode(event)
        if len(raw) > FRAGMENT_BYTES * 1024:
            raise ValueError('Projected record exceeds the central fragment safety limit; no bytes skipped')
        self.in_event = True
        try:
            if len(raw) <= FRAGMENT_BYTES:
                self.line(raw + b'\n')
            else:
                count = (len(raw) + FRAGMENT_BYTES - 1) // FRAGMENT_BYTES
                digest = sha(raw)
                for n in range(count):
                    fragment = dict(schema='mp-event-fragment/1', event_sha256=digest,
                                    part=n, parts=count, encoding='base64',
                                    data=base64.b64encode(raw[n*FRAGMENT_BYTES:(n+1)*FRAGMENT_BYTES]).decode('ascii'))
                    self.line(encode(fragment) + b'\n')
                    if self.file.tell() >= self.watermark:
                        self.checkpoint()
        finally:
            self.in_event = False

    def maybe_checkpoint(self):
        if self.sealed or self.file and (self.file.tell() >= self.watermark or
                          time.perf_counter()-self.last_checkpoint >= self.checkpoint_seconds):
            self.checkpoint()

    def checkpoint(self):
        if self.file:
            self.file.write(self.encoder.flush())
            self.file.flush()
            os.fsync(self.file.fileno())
            self.file.close()
            self.file = None
            raw = self.temp.read_bytes()
            if len(raw) > 5*1024*1024:
                raise ValueError('Compressed chunk exceeded 5 MiB file limit')
            self.sealed.append((raw, self.expanded))
        # Never commit half an original record. All its fragments and its cursor
        # enter the same SQLite transaction, even if it spans several gzip files.
        if self.in_event:
            return
        with self.db:
            for raw, expanded in self.sealed:
                name = 'telemetry/' + self.cfg['publisher_id'] + '/' + uuid.uuid4().hex + '.jsonl.gz'
                self.db.execute('INSERT INTO local_files VALUES (?,?,?)', (name, raw, sha(raw)))
            if self.dirty:
                for src in self.states.values():
                    self.db.execute('UPDATE sources SET generation=?,identity=?,cursor=?,snapshot_end=?,baseline_done=?,prefix_n=?,prefix_hash=?,boundary_hash=?,observed_size=?,error=? WHERE id=?',
                        tuple(src[k] for k in ('generation','identity','cursor','snapshot_end','baseline_done',
                                              'prefix_n','prefix_hash','boundary_hash','observed_size','error','id')))
            self.db.executemany('DELETE FROM observations WHERE id=?', [(n,) for n in self.observation_ids])
        for raw, expanded in self.sealed:
            self.files += 1
            self.compressed += len(raw)
            self.expanded_total += expanded
        self.sealed = []
        self.dirty = False
        self.observation_ids = []
        materialize(self.root, self.db)
        if self.temp.exists():
            self.temp.unlink()
        self.last_checkpoint = time.perf_counter()

    def close(self):
        if self.file:
            self.file.close()
            self.file = None


def collect(root, cfg, db, backfill, seconds=0, byte_budget=None,
            chunk_bytes=CHUNK_BYTES, checkpoint_seconds=60):
    """Scan frozen ends continuously; checkpoints never invoke Git/network."""
    started = time.perf_counter()
    deadline = started + seconds if seconds else float('inf')
    materialize(root, db)
    states = {s['id']: dict(s) for s in db.execute('SELECT * FROM sources')}
    ends = {}
    for bound in cfg['sources']:
        src = states.get(bound['id'])
        if src is None:
            raise ValueError('New source requires --backfill')
        # Fix all incremental ends at invocation, not after another source finishes.
        try:
            ends[src['id']] = source_path(root, bound).stat().st_size
        except OSError:
            ends[src['id']] = None
    stream = Stream(root, cfg, db, states, chunk_bytes, checkpoint_seconds)
    read_bytes = 0
    budget_end = False
    try:
        for row in db.execute('SELECT * FROM observations ORDER BY id').fetchall():
            stream.event(json.loads(row['data']))
            stream.observation_ids.append(row['id'])
            stream.maybe_checkpoint()
        for bound in cfg['sources']:
            src = states[bound['id']]
            if backfill and src['baseline_done']:
                continue
            if not backfill and not src['baseline_done']:
                continue
            if time.perf_counter() >= deadline:
                budget_end = True
                break
            try:
                path = source_path(root, bound)
                stat = path.stat()
                with path.open('rb') as fh:
                    changed = identity(stat) != src['identity'] or stat.st_size < src['observed_size']
                    if src['cursor'] and not changed:
                        _, prefix, boundary = fingerprints(fh, src['cursor'], src['prefix_n'])
                        changed = prefix != src['prefix_hash'] or boundary != src['boundary_hash']
                    if changed:
                        previous = src['generation']
                        src.update(generation=uuid.uuid4().hex, identity=identity(stat), cursor=0,
                                   snapshot_end=stat.st_size, observed_size=stat.st_size, baseline_done=0,
                                   prefix_n=0, prefix_hash=None, boundary_hash=None,
                                   error='source changed; operator backfill required')
                        stream.event(dict(schema='mp-source-observation/1', run_id=cfg['run_id'],
                            publisher_id=cfg['publisher_id'], source_id=src['id'], generation=src['generation'],
                            previous_generation=previous, observation='file_identity_or_boundary_changed',
                            snapshot_end=stat.st_size, collected_at=time.time(), projection_version=VERSION))
                        stream.dirty = True
                        continue
                    if ends[src['id']] is None:
                        raise ValueError('Source was unavailable at scan start; retry required')
                    end = min(stat.st_size, src['snapshot_end']) if backfill else min(stat.st_size, ends[src['id']])
                    start_cursor = cursor = src['cursor']
                    fh.seek(0)
                    initial_prefix = fh.read(256)
                    fh.seek(max(0, cursor-256))
                    boundary_bytes = fh.read(min(cursor, 256))
                    fh.seek(cursor)
                    tail = False
                    while cursor < end:
                        if time.perf_counter() >= deadline:
                            budget_end = True
                            break
                        raw = fh.readline(min(MAX_RECORD+1, end-cursor))
                        if len(raw) > MAX_RECORD:
                            raise ValueError('A JSONL record exceeds 64 MiB; no bytes skipped')
                        if not raw.endswith(b'\n'):
                            tail = True
                            break
                        native = json.loads(raw.decode('utf-8'))
                        event = project(native)
                        event.update(schema='mp-session-event/1', run_id=cfg['run_id'],
                            publisher_id=cfg['publisher_id'], framework=cfg['framework'],
                            source_id=src['id'], generation=src['generation'],
                            byte_start=cursor, byte_end=cursor+len(raw), collected_at=time.time())
                        from transport import SECRET_PATTERNS
                        check_text(encode(event).decode('utf-8'), SECRET_PATTERNS)
                        stream.event(event)
                        cursor += len(raw)
                        read_bytes += len(raw)
                        src['cursor'] = cursor
                        # Keep fingerprints correct at any compressed/time checkpoint.
                        boundary_bytes = raw[-256:] if len(raw) >= 256 else (boundary_bytes + raw)[-256:]
                        src['prefix_n'] = min(cursor, 256)
                        src['prefix_hash'] = sha(initial_prefix[:src['prefix_n']])
                        src['boundary_hash'] = sha(boundary_bytes)
                        stream.dirty = True
                        stream.maybe_checkpoint()
                    after = path.stat()
                    if identity(after) != src['identity'] or after.st_size < cursor:
                        raise ValueError('Source changed during scan; operator must retry')
                    done = cursor >= end or tail
                    src['observed_size'] = after.st_size
                    src['error'] = None
                    if backfill and done:
                        src['baseline_done'] = 1
                    stream.dirty = True
                    if backfill and done or not backfill and cursor != start_cursor:
                        stream.event(dict(schema='mp-source-observation/1', run_id=cfg['run_id'],
                            publisher_id=cfg['publisher_id'], source_id=src['id'], generation=src['generation'],
                            observation='snapshot_scan_finished' if backfill else 'incremental_scan',
                            snapshot_end=src['snapshot_end'], complete_record_end=cursor,
                            unframed_tail_bytes=end-cursor if tail else 0,
                            collected_at=time.time(), projection_version=VERSION))
                    if budget_end:
                        break
            except (OSError, ValueError, RecursionError) as exc:
                error = 'invalid JSON record' if isinstance(exc, json.JSONDecodeError) else str(exc)
                src['error'] = type(exc).__name__ + ': ' + error[:240]
                stream.dirty = True
                stream.event(dict(schema='mp-source-observation/1', run_id=cfg['run_id'],
                    publisher_id=cfg['publisher_id'], source_id=src['id'], generation=src['generation'],
                    observation='read_or_projection_error', committed_cursor=src['cursor'],
                    error_type=type(exc).__name__, collected_at=time.time(), projection_version=VERSION))
        stream.checkpoint()
    finally:
        stream.close()
    ready = bool(states) and all(s['baseline_done'] and not s['error'] for s in states.values())
    if ready:
        with db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('bootstrapped','1')")
    return dict(raw_bytes=read_bytes, chunks=stream.files, compressed_bytes=stream.compressed,
                expanded_bytes=stream.expanded_total, scan_seconds=time.perf_counter()-started,
                budget_exhausted=budget_end)


def status(db, cfg):
    rows = [dict(r) for r in db.execute('SELECT id,generation,cursor,snapshot_end,baseline_done,observed_size,error FROM sources')]
    missing = [s['id'] for s in cfg['sources'] if s['id'] not in {r['id'] for r in rows}]
    version = db.execute('PRAGMA user_version').fetchone()[0]
    pending = db.execute('SELECT count(*) FROM ' + ('local_files' if version == 2 else 'outbox WHERE published=0')).fetchone()[0]
    return dict(sources=rows, uninitialized_sources=missing, state_version=version,
                migration_required=version==1, local_pending_chunks=pending,
                snapshot_scan_done=bool(rows) and not missing and all(s['baseline_done'] and not s['error'] for s in rows))
