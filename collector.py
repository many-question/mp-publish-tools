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
from projection import (NOTE_TEXT_BYTES, NOTE_TEXT_SUFFIXES, VERSION, check_text, mask, project, project_note,
                        project_registry, real, within, codex_header)

MAX_RECORD = 64 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
CHUNK_BYTES = 2 * 1024 * 1024  # compressed output watermark, not input bytes
FRAGMENT_BYTES = 128 * 1024
PUBLISHER_RELEASE = hashlib.sha256((Path(__file__).parent / 'release.json').read_bytes()).hexdigest()
# 普通 publish 里新发现的家族日志自动回填的时间预算（没给 --max-seconds 时）；没做完的下次接着做。
FAMILY_SECONDS = 60
NOTE_FILES = 1000
MISSING = 'source missing'
# A family source that changed is collected again by the next publish; not an error that needs anyone.
CHANGED_AGAIN = 'source changed; the next publish collects it again'
# 3.3: sources found through the session family live in their own table. 3.2 never reads it, so a
# fallback to a 3.2 runtime keeps collecting the operator-bound sources (state_version stays 2).
FAMILY_TABLE = '''CREATE TABLE IF NOT EXISTS family_sources(
  id TEXT PRIMARY KEY, path TEXT NOT NULL, generation TEXT NOT NULL,
  identity TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
  snapshot_end INTEGER NOT NULL, baseline_done INTEGER NOT NULL DEFAULT 0,
  prefix_n INTEGER NOT NULL DEFAULT 0, prefix_hash TEXT, boundary_hash TEXT,
  observed_size INTEGER NOT NULL, error TEXT)'''
SOURCE_COLUMNS = 'id,path,generation,identity,cursor,snapshot_end,baseline_done,prefix_n,prefix_hash,boundary_hash,observed_size,error'


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
        db.execute(FAMILY_TABLE)
        return db
    try:
        db.execute("BEGIN IMMEDIATE")
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute(FAMILY_TABLE)
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
                 observation=kind, collected_at=time.time(), projection_version=VERSION,
                 **({'framework': 'codex-desktop'} if cfg['framework'] == 'codex' else {}), **extra)
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
            if db.execute('SELECT 1 FROM family_sources WHERE id=?', (src['id'],)).fetchone():
                # The operator now binds a log the family already collects: move it with its cursor, never twice.
                db.execute('INSERT INTO sources(' + SOURCE_COLUMNS + ') SELECT ' + SOURCE_COLUMNS + ' FROM family_sources WHERE id=?',
                           (src['id'],))
                db.execute('DELETE FROM family_sources WHERE id=?', (src['id'],))
                continue
            path = source_path(root, src)
            stat = path.stat()
            generation = uuid.uuid4().hex
            db.execute('INSERT INTO sources(id,path,generation,identity,snapshot_end,observed_size) VALUES (?,?,?,?,?,?)',
                       (src['id'], str(path), generation, identity(stat), stat.st_size, stat.st_size))
            header = codex_header(path) if cfg['framework'] == 'codex' else None
            observe(db, cfg, dict(src, generation=generation), 'initial_snapshot',
                    source_name=path.name, observed_bytes=stat.st_size, snapshot_end=stat.st_size,
                    **({'cli_session_id': header['id']} if header else {}))


def source_finished(row, framework):
    """Codex 迁走的显式来源已结束等待，保留缺失观察而不堵住整个 run。"""
    return bool(row['baseline_done'] and not row['error'] or framework == 'codex' and row['error'] == MISSING)


def mark_missing_sources(root, cfg, db):
    """普通 publish 在首次回填门限之前也要发现显式 Codex 来源消失。"""
    if cfg['framework'] != 'codex':
        return
    explicit = {s['id'] for s in cfg['sources']}
    with db:
        for row in db.execute('SELECT * FROM sources').fetchall():
            if row['id'] not in explicit or row['error'] == MISSING or os.path.exists(row['path']):
                continue
            extra = dict(last_observed_size=row['observed_size'])
            if row['cursor'] < row['observed_size']:
                extra['reason'] = 'codex_migration_suspected'
            observe(db, cfg, row, 'source_missing', committed_cursor=row['cursor'], **extra)
            db.execute('UPDATE sources SET error=? WHERE id=?', (MISSING, row['id']))


def restart_backfill(root, cfg, db):
    with db:
        rows = {r['id']: dict(r, _table='sources') for r in db.execute('SELECT * FROM sources ORDER BY rowid')}
        explicit = {s['id'] for s in cfg['sources']}
        targets = [(rows[s['id']], s) for s in cfg['sources']
                   if cfg['framework'] != 'codex' or os.path.exists(s['path'])]
        # 家族发现的来源一并重采集：操作者已经绑定的不重复（否则留下孤儿代次），日志已经不在的跳过。
        targets += [(old, old) for old in family_rows(db) if old['id'] not in explicit and os.path.exists(old['path'])]
        for old, src in targets:
            stat = source_path(root, src).stat()
            generation = uuid.uuid4().hex
            observe(db, cfg, dict(src, generation=generation), 'operator_backfill_restart',
                    previous_generation=old['generation'], previous_cursor=old['cursor'],
                    observed_bytes=stat.st_size, snapshot_end=stat.st_size)
            db.execute('UPDATE ' + old['_table'] + ''' SET generation=?,identity=?,cursor=0,snapshot_end=?,
                observed_size=?,baseline_done=0,prefix_n=0,prefix_hash=NULL,boundary_hash=NULL,error=NULL WHERE id=?''',
                (generation, identity(stat), stat.st_size, stat.st_size, src['id']))
        db.execute("DELETE FROM meta WHERE key='bootstrapped'")


def family_rows(db):
    """家族发现的来源：family_sources 表（3.2 不读它）；还没有这张表时为空。"""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='family_sources'").fetchone():
        return []
    return [dict(r, _table='family_sources') for r in db.execute('SELECT * FROM family_sources ORDER BY rowid')]


def family_state(db):
    """家族采集的本地记录（meta 表的 family 键）：调用 publish 时成为根的主持、发现的来源属于哪个成员、
    已排队上传的登记与笔记版本、被秘密检查拦下的版本、笔记文件的大小与修改时间、已记过的工作区外成员，
    以及记过观察的问题：登记目录不在、读不了的登记、笔记超过上限。"""
    row = db.execute("SELECT value FROM meta WHERE key='family'").fetchone()
    state = json.loads(row[0]) if row else {}
    for key, kind in (('roots', list), ('sources', dict), ('sent', dict), ('blocked', dict), ('seen', dict),
                      ('outside', list), ('unavailable', bool), ('unreadable', list), ('truncated', list)):
        if not isinstance(state.get(key), kind):
            state[key] = kind()
    return state


def saved_roots(root):
    """本地状态里记下的根，只读；还没有状态时为空。"""
    path = inside(root, local_state(root) / 'state.sqlite')
    if not path.exists():
        return []
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        return family_state(db)['roots']
    except sqlite3.Error:
        return []
    finally:
        db.close()


def shown(key, patterns):
    """被拦下的登记或笔记在输出里的写法：笔记的文件名本身命中秘密检查时不原样打印。"""
    if any(pattern.search(key) for pattern, _ in patterns):
        return key.split('/', 1)[0] + '/(file name matched the secret check)'
    return key


def note_files(folder, root):
    """笔记目录里的普通文件（含子目录）：([(相对路径, 路径)], 超过上限, 读目录出错)。
    不跟随链接与目录联接，解析之后要在笔记根目录 root 里面。"""
    if not folder.exists():
        return [], False, False
    if not folder.is_dir() or not within(folder, root):
        return [], False, True
    out, errors = [], []
    for base, dirs, files in os.walk(folder, onerror=errors.append):
        dirs[:] = sorted(d for d in dirs if within(Path(base) / d, root))
        for name in sorted(files):
            path = Path(base) / name
            if not within(path, root) or not path.is_file():
                continue
            if len(out) >= NOTE_FILES:
                return out, True, bool(errors)
            out.append((path.relative_to(folder).as_posix(), path))
    return out, False, bool(errors)


def read_note(path, size):
    """(文件 sha256, 文本文件不超过 1 MiB 时的全部字节)。"""
    digest, parts = hashlib.sha256(), []
    keep = size <= NOTE_TEXT_BYTES and path.name.lower().endswith(NOTE_TEXT_SUFFIXES)
    with path.open('rb') as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
            if keep:
                parts.append(chunk)
    return digest.hexdigest(), b''.join(parts) if keep else None


def discover(root, cfg, db, desktop):
    """家族发现（docs/multi-session.md 4.1–4.4），在一个事务里：新成员的日志登记为来源（随后在预算内
    自动回填），登记与共享笔记有新版本就排进待发送队列。只收工作区里的成员；工作区外的只记一条观察。

    秘密检查：登记里的文字就地遮盖、照常发送；笔记正文命中只发清单。遮盖之后仍有命中（例如笔记的
    文件名）的那一条才留在本地，列进 blocked。意外的错误不挡住交付：记一条观察，这一次跳过家族发现。"""
    try:
        if getattr(desktop, 'discovery_error', None):
            with db:
                observe(db, cfg, dict(id='registry:', generation=None), 'family_discovery_failed',
                        error_type=desktop.discovery_error)
            return dict(error=desktop.discovery_error)
        return _discover(root, cfg, db, desktop)
    except sqlite3.Error:
        raise
    except Exception as exc:  # 文件刚被删、没有权限、结构太深……
        with db:
            db.execute('INSERT INTO observations(data) VALUES (?)', (encode(dict(
                schema='mp-source-observation/1', run_id=cfg['run_id'], publisher_id=cfg['publisher_id'],
                source_id='registry:', generation=None, observation='family_discovery_failed',
                error_type=type(exc).__name__, collected_at=time.time(), projection_version=VERSION,
                **({'framework': 'codex-desktop'} if cfg['framework'] == 'codex' else {}))),))
        return dict(error=type(exc).__name__)


def _discover(root, cfg, db, desktop):
    from transport import SECRET_PATTERNS
    state = family_state(db)
    roots = [(r, 'bound_root') for r in desktop.bound]
    roots += [(r, 'caller_root') for r in state['roots'] if r not in desktop.bound]
    role, caller = desktop.classify({r for r, _ in roots})
    if role == 'new_root':
        roots.append((caller, 'caller_root'))
        state['roots'].append(caller)
    rows = db.execute('SELECT id, path FROM sources').fetchall() + [dict(id=r['id'], path=r['path']) for r in family_rows(db)]
    ids = {r['id'] for r in rows} | {s['id'] for s in cfg['sources']}
    known = {os.path.normcase(p) for p in [r['path'] for r in rows] + [s['path'] for s in cfg['sources']]}
    old_blocked, blocked, now = state['blocked'], {}, time.time()
    envelope = dict(run_id=cfg['run_id'], publisher_id=cfg['publisher_id'], framework=getattr(desktop, 'framework', 'claude-desktop'), observed_at=now)
    summary = dict(roots=[r for r, _ in roots], members=0, outside=0, new_sources=0, records=0, masked=0)

    def notice(source_id, kind, **extra):
        db.execute('INSERT INTO observations(data) VALUES (?)', (encode(dict(
            schema='mp-source-observation/1', run_id=cfg['run_id'], publisher_id=cfg['publisher_id'],
            source_id=source_id, generation=None, observation=kind, collected_at=now, projection_version=VERSION,
            **({'framework': 'codex-desktop'} if cfg['framework'] == 'codex' else {}), **extra)),))

    def queue(key, digest, event):
        raw = encode(event)
        try:
            check_text(raw.decode('utf-8'), SECRET_PATTERNS)
        except ValueError:
            # 遮盖之后还有命中：这一条留在本地，内容变了再试。
            blocked[key] = digest
            return
        db.execute('INSERT INTO observations(data) VALUES (?)', (raw,))
        state['sent'][key] = digest
        summary['records'] += 1

    with db:
        if roots:
            # 约定第 3 节：遇到不认识的结构要计数并报告，不能静默当成没有。
            if not desktop.registry_ok and not state['unavailable']:
                notice('registry:', 'registry_unavailable')
            state['unavailable'] = not desktop.registry_ok
            unreadable = sorted(desktop.unreadable)
            if unreadable and unreadable != state['unreadable']:
                notice('registry:', 'registry_unreadable', count=len(unreadable))
            state['unreadable'] = unreadable
        for lid, relation, parent in desktop.members(roots):
            if lid not in desktop.entries:
                if getattr(desktop, 'include_outside', False):
                    key = 'unavailable:' + lid
                    if key not in state['seen']:
                        notice('registry:' + lid, 'family_member_unavailable', desktop_session=lid,
                               parent_session=parent, relation=relation, reason='not_local_or_migrated')
                        state['seen'][key] = True
                continue
            if not desktop.inside(lid) and not getattr(desktop, 'include_outside', False):
                summary['outside'] += 1
                if lid not in state['outside']:
                    state['outside'].append(lid)
                    notice('registry:' + lid, 'family_member_outside_workspace', desktop_session=lid, relation=relation,
                           parent_session=parent, reason='cwd_outside_workspace')
                continue
            if lid in state['outside']:
                state['outside'].remove(lid)
            summary['members'] += 1
            if not desktop.inside(lid):
                summary['outside'] += 1
            for kind, cid, path in desktop.logs(lid, [desktop.caller_cli] if lid == caller and desktop.caller_cli else []):
                try:
                    path = source_path(root, {'path': str(path)})
                except (OSError, ValueError):
                    continue
                # 家族的日志必须在 Claude Code 的日志根目录里（不跟随指到外面的链接与目录联接）。
                if not real(path).is_relative_to(desktop.projects_root):
                    continue
                sid = hashlib.sha256(str(path).encode()).hexdigest()[:24]
                if sid in ids or os.path.normcase(str(path)) in known:
                    continue
                stat = path.stat()
                generation = uuid.uuid4().hex
                db.execute('INSERT INTO family_sources(id,path,generation,identity,snapshot_end,observed_size) VALUES (?,?,?,?,?,?)',
                           (sid, str(path), generation, identity(stat), stat.st_size, stat.st_size))
                parent_id = (desktop.parents(cid)[:1] or [('', lid)])[0][1] if kind == 'subagent' else parent
                if kind == 'subagent' and getattr(desktop, 'include_outside', False):
                    parent_id = desktop.owner(cid)  # 多级子任务的观察指向所属道，原始直接父仍在会话头中。
                observe(db, cfg, dict(id=sid, generation=generation), 'family_source_discovered',
                        source_name=path.name, observed_bytes=stat.st_size, snapshot_end=stat.st_size,
                        desktop_session=lid, relation='thread_spawn.parent_thread_id' if kind == 'subagent' else relation,
                        parent_session=parent_id, log_kind=kind, cli_session_id=cid,
                        **({'outside_workspace': True} if getattr(desktop, 'include_outside', False) and not desktop.inside(cid) else {}))
                ids.add(sid)
                known.add(os.path.normcase(str(path)))
                state['sources'][sid] = lid
                summary['new_sources'] += 1
            registry, unrecognized = project_registry(desktop.entries[lid], codex=getattr(desktop, 'include_outside', False))
            redaction = []
            # 关系字段不能因为一段文字整条丢掉：命中的文字像 assistant 正文那样就地遮盖。
            registry = mask(registry, SECRET_PATTERNS, '/registry', redaction)
            key, digest = 'registry:' + lid, sha(encode(registry))
            if state['sent'].get(key) != digest:
                event = dict(schema='mp-session-registry/1', source_id=key, content_sha256=digest, registry=registry, **envelope)
                if unrecognized:
                    event['unrecognized'] = unrecognized
                if redaction:
                    event['redaction'] = redaction
                    summary['masked'] += 1
                queue(key, digest, event)
        if getattr(desktop, 'include_outside', False):
            for name in ('unknown', 'mismatched'):
                count = len(getattr(desktop, name, ()))
                key = 'codex_' + name
                if count and state['seen'].get(key) != count:
                    notice('registry:', key + '_structure', count=count)
                state['seen'][key] = count
            for name, count in desktop.known_results.items():
                key = 'codex_' + name
                mark = sha(encode(sorted(desktop.pending_continuations))) if name == 'continuation_pending' else count
                if count and state['seen'].get(key) != mark:
                    notice('registry:', key, count=count)
                state['seen'][key] = mark
        for lid, _ in ([] if getattr(desktop, 'include_outside', False) else roots):
            prefix = 'notes:' + lid + '/'
            present = set()
            files, truncated, unreadable = note_files(desktop.notes_folder(lid), desktop.notes_root)
            if truncated and lid not in state['truncated']:
                notice('notes:' + lid, 'notes_truncated', limit=NOTE_FILES)
            state['truncated'] = [r for r in state['truncated'] if r != lid] + ([lid] if truncated else [])
            for name, path in files:
                key = prefix + name
                present.add(key)
                try:
                    stat = path.stat()
                    mark = [stat.st_size, stat.st_mtime_ns]
                    if state['seen'].get(key) == mark:
                        if key in old_blocked:
                            blocked[key] = old_blocked[key]
                        continue
                    file_digest, data = read_note(path, stat.st_size)
                except OSError:
                    continue
                note = project_note(name, stat.st_size, stat.st_mtime, file_digest, data, SECRET_PATTERNS)
                digest = sha(encode(note))
                state['seen'][key] = mark
                if state['sent'].get(key) != digest:
                    queue(key, digest, dict(schema='mp-shared-note/1', source_id='notes:' + lid, content_sha256=digest,
                                            note=note, **envelope))
            if truncated or unreadable:
                # 没看全目录时不做删除比对，免得误发 deleted。
                blocked.update({k: v for k, v in old_blocked.items() if k.startswith(prefix) and k not in present})
                continue
            for key in [k for k in state['seen'] if k.startswith(prefix) and k not in present]:
                del state['seen'][key]
                note = {'name': key[len(prefix):], 'deleted': True}
                if key in state['sent']:
                    queue(key, sha(encode(note)), dict(schema='mp-shared-note/1', source_id='notes:' + lid,
                                                       content_sha256=sha(encode(note)), note=note, **envelope))
        state['blocked'] = blocked
        db.execute("INSERT OR REPLACE INTO meta VALUES ('family',?)", (json.dumps(state, sort_keys=True),))
    summary['blocked'] = sorted(shown(key, SECRET_PATTERNS) for key in blocked)
    return summary


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
        if self.cfg['framework'] == 'codex' and event.get('schema') == 'mp-source-observation/1':
            event['framework'] = 'codex-desktop'  # 包括扫描器直接产生的缺失、快照、增量、换代与错误观察。
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
                    self.db.execute('UPDATE ' + src.get('_table', 'sources') + ' SET generation=?,identity=?,cursor=?,snapshot_end=?,baseline_done=?,prefix_n=?,prefix_hash=?,boundary_hash=?,observed_size=?,error=? WHERE id=?',
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
            chunk_bytes=CHUNK_BYTES, checkpoint_seconds=60, family_seconds=FAMILY_SECONDS):
    """Scan frozen ends continuously; checkpoints never invoke Git/network.

    Sources found through the session family (not in cfg) need no operator: an
    ordinary call snapshot-scans new ones within its own budget, after all increments.
    """
    from transport import SECRET_PATTERNS
    started = time.perf_counter()
    deadline = started + seconds if seconds else float('inf')
    materialize(root, db)
    explicit = [s['id'] for s in cfg['sources']]
    # Rows of `sources` that are not bound any more stay ignored, as in 3.2.
    states = {s['id']: dict(s, _table='sources') for s in db.execute('SELECT * FROM sources ORDER BY rowid') if s['id'] in explicit}
    if any(sid not in states for sid in explicit):
        raise ValueError('New source requires --backfill')
    for row in family_rows(db):
        states.setdefault(row['id'], row)
    found = [sid for sid in states if sid not in explicit]
    ends = {}
    for sid in explicit + found:
        # Fix all incremental ends at invocation, not after another source finishes.
        try:
            ends[sid] = source_path(root, states[sid]).stat().st_size
        except OSError:
            ends[sid] = None
    if backfill:
        plan = [(sid, deadline) for sid in explicit + found if not states[sid]['baseline_done']]
    else:
        plan = [(sid, deadline) for sid in explicit + found if states[sid]['baseline_done']]
        auto = min(deadline, started + (seconds or family_seconds))
        plan += [(sid, auto) for sid in found if not states[sid]['baseline_done']]
    stream = Stream(root, cfg, db, states, chunk_bytes, checkpoint_seconds)
    read_bytes = 0
    budget_end = family_end = False
    def missing_source(src):
        if src['error'] != MISSING:
            extra = {}
            if cfg['framework'] == 'codex':
                src['observed_size'] = max(src['observed_size'], ends.get(src['id']) or 0)
                extra['last_observed_size'] = src['observed_size']
                if src['cursor'] < src['observed_size']:
                    extra['reason'] = 'codex_migration_suspected'
            src['error'] = MISSING
            stream.dirty = True
            stream.event(dict(schema='mp-source-observation/1', run_id=cfg['run_id'],
                publisher_id=cfg['publisher_id'], source_id=src['id'], generation=src['generation'],
                observation='source_missing', committed_cursor=src['cursor'],
                collected_at=time.time(), projection_version=VERSION, **extra))

    try:
        for row in db.execute('SELECT * FROM observations ORDER BY id').fetchall():
            stream.event(json.loads(row['data']))
            stream.observation_ids.append(row['id'])
            stream.maybe_checkpoint()
        for sid, limit in plan:
            src = states[sid]
            snapshot = not src['baseline_done']
            automatic = snapshot and not backfill
            if (sid in found or cfg['framework'] == 'codex') and not os.path.exists(src['path']):
                missing_source(src)
                continue
            if time.perf_counter() >= limit:
                if automatic:
                    family_end = True
                else:
                    budget_end = True
                break
            stopped = False
            try:
                path = source_path(root, src)
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
                                   error='source changed; operator backfill required' if sid in explicit else CHANGED_AGAIN)
                        stream.event(dict(schema='mp-source-observation/1', run_id=cfg['run_id'],
                            publisher_id=cfg['publisher_id'], source_id=src['id'], generation=src['generation'],
                            previous_generation=previous, observation='file_identity_or_boundary_changed',
                            snapshot_end=stat.st_size, collected_at=time.time(), projection_version=VERSION))
                        stream.dirty = True
                        continue
                    if ends[src['id']] is None:
                        raise ValueError('Source was unavailable at scan start; retry required')
                    if cfg['framework'] == 'codex':
                        src['observed_size'] = stat.st_size
                    end = min(stat.st_size, src['snapshot_end']) if snapshot else min(stat.st_size, ends[src['id']])
                    start_cursor = cursor = src['cursor']
                    fh.seek(0)
                    initial_prefix = fh.read(256)
                    fh.seek(max(0, cursor-256))
                    boundary_bytes = fh.read(min(cursor, 256))
                    fh.seek(cursor)
                    tail = False
                    while cursor < end:
                        if time.perf_counter() >= limit:
                            stopped = True
                            break
                        raw = fh.readline(min(MAX_RECORD+1, end-cursor))
                        if len(raw) > MAX_RECORD:
                            raise ValueError('A JSONL record exceeds 64 MiB; no bytes skipped')
                        if not raw.endswith(b'\n'):
                            tail = True
                            break
                        native = json.loads(raw.decode('utf-8'))
                        # Assistant text and tool heads are masked in place here;
                        # a hit in user text still blocks the record below.
                        event = project(native, SECRET_PATTERNS)
                        event.update(schema='mp-session-event/1', run_id=cfg['run_id'],
                            publisher_id=cfg['publisher_id'], framework=cfg['framework'],
                            source_id=src['id'], generation=src['generation'],
                            byte_start=cursor, byte_end=cursor+len(raw), collected_at=time.time())
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
                    if snapshot and done:
                        src['baseline_done'] = 1
                    stream.dirty = True
                    if snapshot and done or not snapshot and cursor != start_cursor:
                        stream.event(dict(schema='mp-source-observation/1', run_id=cfg['run_id'],
                            publisher_id=cfg['publisher_id'], source_id=src['id'], generation=src['generation'],
                            observation='snapshot_scan_finished' if snapshot else 'incremental_scan',
                            snapshot_end=src['snapshot_end'], complete_record_end=cursor,
                            unframed_tail_bytes=end-cursor if tail else 0,
                            collected_at=time.time(), projection_version=VERSION))
                    if stopped:
                        if automatic:
                            family_end = True
                        else:
                            budget_end = True
                        break
            except (OSError, ValueError, RecursionError) as exc:
                if cfg['framework'] == 'codex' and isinstance(exc, FileNotFoundError):
                    missing_source(src)
                    continue
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
    # The operator's first backfill covers the explicitly bound sources; family ones follow by themselves.
    ready = bool(explicit) and all(source_finished(states[sid], cfg['framework']) for sid in explicit)
    if ready:
        with db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('bootstrapped','1')")
    return dict(raw_bytes=read_bytes, chunks=stream.files, compressed_bytes=stream.compressed,
                expanded_bytes=stream.expanded_total, scan_seconds=time.perf_counter()-started,
                budget_exhausted=budget_end, family_budget_exhausted=family_end,
                family_pending=sum(1 for sid in found if not states[sid]['baseline_done'] and states[sid]['error'] != MISSING))


def status(db, cfg, detail=False):
    """sources 和 3.2 一样列出 sources 表；家族发现的来源（family_sources 表）汇总在 family 里，detail 时列出路径。
    能自己好的情况（日志被删、换了文件等下次 publish 重收）不算 errors；被删的不再等它扫完。"""
    from transport import SECRET_PATTERNS
    rows = [dict(r) for r in db.execute('SELECT id,path,generation,cursor,snapshot_end,baseline_done,observed_size,error FROM sources ORDER BY rowid')]
    explicit = [s['id'] for s in cfg['sources']]
    found = [r for r in family_rows(db) if r['id'] not in explicit]
    missing = [sid for sid in explicit if sid not in {r['id'] for r in rows}]
    version = db.execute('PRAGMA user_version').fetchone()[0]
    pending = db.execute('SELECT count(*) FROM ' + ('local_files' if version == 2 else 'outbox WHERE published=0')).fetchone()[0]
    state = family_state(db)
    family = dict(roots=list(dict.fromkeys([*((cfg.get('family') or {}).get('roots') or []), *state['roots']])),
                  sources=len(found), pending=sum(1 for r in found if not r['baseline_done'] and r['error'] != MISSING),
                  missing=sum(1 for r in found if r['error'] == MISSING),
                  errors=[dict(id=r['id'], path=r['path'], error=r['error']) for r in found if r['error'] not in (None, MISSING, CHANGED_AGAIN)],
                  blocked=sorted(shown(key, SECRET_PATTERNS) for key in state['blocked']),
                  registry=dict(available=not state['unavailable'], unreadable=len(state['unreadable'])))
    if cfg['framework'] == 'codex':
        family['missing_sources'] = [dict(id=r['id'], path=r['path'], cursor=r['cursor'],
            last_observed_size=r['observed_size'], reason='codex_migration_suspected')
            for r in rows + found if r['error'] == MISSING and r['cursor'] < r['observed_size']]
        family['missing'] = len(family['missing_sources'])
    if detail:
        family['collected'] = [r['path'] for r in found if r['error'] != MISSING]
    settled = all(source_finished(r, cfg['framework']) for r in rows) and \
        all(r['error'] == MISSING or r['baseline_done'] and not r['error'] for r in found)
    return dict(sources=[{k: v for k, v in r.items() if k != 'path'} for r in rows], uninitialized_sources=missing,
                state_version=version, migration_required=version==1, local_pending_chunks=pending,
                snapshot_scan_done=bool(rows) and not missing and settled, family=family)
