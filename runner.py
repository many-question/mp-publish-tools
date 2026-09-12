#!/usr/bin/env python3
"""Private runtime. The launcher passes its fixed workspace as argv[1]."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import uuid

# -I ignores PYTHONPATH; import code exclusively from this verified runtime.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import atomic, encode, inside, local_state, read_config
import collector
import transport
from projection import project


def bind(root, args):
    state = local_state(root)
    path = inside(root, state / 'config.json')
    if args.init:
        if path.exists():
            raise ValueError('Configuration exists; --init never overwrites it')
        if not args.run_id or not re.fullmatch('[A-Za-z0-9_-]+', args.run_id):
            raise ValueError('--run-id must contain letters, digits, underscores or hyphens')
        if not args.framework or not args.source:
            raise ValueError('--init requires --framework and one or more --source JSONL files')
        cfg = {'schema': 'mp-publish-config/1', 'workspace': str(root), 'run_id': args.run_id,
               'publisher_id': uuid.uuid4().hex, 'framework': args.framework, 'sources': [],
               'update': {'url': args.update_url, 'ref': args.update_ref, 'timeout_seconds': 8}}
        paths = args.source
    else:
        cfg = read_config(root)
        if not cfg:
            raise ValueError('Configure this workspace with --init first')
        paths = args.add_source
    for name in paths:
        source = Path(name).resolve(strict=True)
        item = {'id': hashlib.sha256(str(source).encode()).hexdigest()[:24], 'path': str(source)}
        collector.source_path(root, item)
        if not any(s['path'] == item['path'] for s in cfg['sources']):
            cfg['sources'].append(item)
    atomic(path, encode(cfg))
    print('已保存本工作区的独立绑定。由操作者执行 --backfill，完成后再使用普通 publish。')
    return 0


def pending_before_new_export():
    upstream = transport.git('rev-parse', '--verify', '@{upstream}', check=False)
    if upstream.returncode:
        return bool(transport.git('rev-parse', '--verify', 'HEAD', check=False).returncode == 0)
    return bool(transport.git('rev-list', '@{upstream}..HEAD').stdout.strip())


def flush(root, cfg, db, message, telemetry_only=False):
    # Retry an already committed batch before exporting another one. Git is not a
    # byte-resumable protocol; bounded application batches keep retries bounded.
    if pending_before_new_export():
        # Do not stage newly accumulated files into the pending retry commit.
        branch = transport.git('rev-parse', '--abbrev-ref', 'HEAD').stdout.strip()
        for problem in transport.check_key():
            print(problem, file=sys.stderr)
            return 1
        if transport.git('push', '-u', 'origin', branch, check=False).returncode:
            print('先前提交仍未推送成功，保留待发送数据。', file=sys.stderr)
            return 1
    ids = collector.export_batch(root, cfg, db)
    result = transport.publish(message, telemetry_only=telemetry_only)
    if result == 0:
        for cid in ids:
            dest = collector.chunk_path(root, cfg, cid)
            rel = dest.relative_to(root / 'share').as_posix()
            actual = transport.git('show', 'HEAD:' + rel, check=False, binary=True)
            expected = db.execute('SELECT data FROM outbox WHERE id=?', (cid,)).fetchone()[0]
            if actual.returncode or actual.stdout != collector.delivery_bytes(dest, expected):
                raise ValueError('A telemetry chunk was not committed verbatim (check share ignores/filters)')
        collector.acknowledge(db, ids)
    return result


def main():
    if sys.argv[1:] == ['--self-test']:
        assert project({'type':'user','message':{'role':'user','content':'fixture'}})['native']['message']['content'] == 'fixture'
        assert 'text' not in project({'role':'assistant','content':[{'type':'text','text':'private'}]})['native']['content'][0]
        print('runtime self-test OK')
        return 0
    root = Path(sys.argv[1]).resolve(strict=True)
    ap = argparse.ArgumentParser(description='每个工作区独立配置；首次 --backfill，此后普通调用只增量处理。')
    modes = ap.add_mutually_exclusive_group()
    modes.add_argument('--init', action='store_true')
    modes.add_argument('--backfill', action='store_true')
    modes.add_argument('--status', action='store_true')
    modes.add_argument('--check', action='store_true')
    modes.add_argument('--add-source', action='append', default=[])
    ap.add_argument('--run-id')
    ap.add_argument('--restart-backfill', action='store_true', help='操作者全量重采集；需同时 --backfill；中断后只用 --backfill 续传')
    ap.add_argument('--framework', choices=['codex','claude-code'])
    ap.add_argument('--source', action='append', default=[])
    ap.add_argument('--update-url', default='')
    ap.add_argument('--update-ref', default='refs/heads/stable')
    ap.add_argument('--share-limit-mb', type=int, help=argparse.SUPPRESS)  # old callers; ignored
    ap.add_argument('--max-seconds', type=float, default=0, help='回填总时间预算；0 表示持续至本次快照结束')
    ap.add_argument('--batch-bytes', type=int, default=4*1024*1024, help='单批原始读取预算；完整大记录可跨预算')
    ap.add_argument('-m', '--message', default='')
    args = ap.parse_args(sys.argv[2:])
    if args.restart_backfill and not args.backfill:
        ap.error('--restart-backfill requires --backfill')
    if args.batch_bytes < 1 or args.max_seconds < 0:
        raise ValueError('Budgets must be positive (max-seconds may be zero)')
    transport.configure(root)
    if args.init or args.add_source:
        return bind(root, args)
    cfg = read_config(root)
    if args.check:
        return transport.publish(check=True)
    if not cfg:
        if args.backfill or args.status:
            print('本工作区尚未配置遥测；需要操作者先执行 --init。')
            return 2
        print('遥测未配置，仅发布科研交付物。')
        return transport.publish(args.message)
    statefile = inside(root, local_state(root) / 'state.sqlite')
    if not statefile.exists() and not args.backfill:
        print('尚未首次回填，普通调用不会扫描历史。请操作者运行 --backfill。')
        return 2 if args.status else transport.publish(args.message)
    db = collector.connect(root)
    try:
        if args.backfill:
            collector.initialize(root, cfg, db)
        # Validate binding even during ordinary calls, without registering new files.
        prior = db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()
        expected = encode({'workspace': cfg['workspace'], 'publisher_id': cfg['publisher_id'],
                          'run_id': cfg['run_id'], 'framework': cfg['framework']}).decode()
        if not prior or prior[0] != expected:
            raise ValueError('State is not bound to this workspace')
        if args.restart_backfill:
            collector.restart_backfill(root, cfg, db)
        if args.status:
            print(json.dumps(collector.status(db, cfg), ensure_ascii=False, indent=2))
            return 0
        before = collector.status(db, cfg)
        bootstrapped = db.execute("SELECT value FROM meta WHERE key='bootstrapped'").fetchone()
        source_ready = not before['uninitialized_sources'] and all(s['baseline_done'] for s in before['sources'])
        if not args.backfill and (not bootstrapped or not source_ready):
            print('首次回填/来源重建尚未完成；请操作者继续 --backfill。')
            return transport.publish(args.message)
        deadline = time.monotonic() + args.max_seconds if args.max_seconds else float('inf')
        while True:
            if args.backfill and time.monotonic() >= deadline:
                print('回填预算结束；再次执行 --backfill 从断点继续。')
                return 3
            # Backlog from a failed push is sent before collecting additional bytes.
            if db.execute('SELECT count(*) FROM outbox WHERE published=0').fetchone()[0] == 0:
                collector.collect(root, cfg, db, args.backfill, seconds=3, byte_budget=args.batch_bytes)
            try:
                result = flush(root, cfg, db, args.message or ('session backfill' if args.backfill else 'share update'), telemetry_only=args.backfill)
            except ValueError as exc:
                if args.backfill:
                    raise
                print('遥测待发送数据已保留：' + str(exc), file=sys.stderr)
                return transport.publish(args.message)
            if result:
                return result
            current = collector.status(db, cfg)
            atomic(inside(root, local_state(root) / 'status.json'), encode(current))
            if not args.backfill:
                for src in current['sources']:
                    if src['error']:
                        print('遥测待处理：' + src['id'] + ' ' + src['error'], file=sys.stderr)
                return 0
            print('回填：已处理字节 ' + str(sum(s['cursor'] for s in current['sources'])) +
                  '；待发分块 ' + str(current['pending_chunks']), flush=True)
            if current['backfill_published']:
                with db:
                    db.execute("INSERT OR REPLACE INTO meta VALUES ('bootstrapped','1')")
                print('首次快照的完整记录已回填并推送；后续普通 publish 只处理新增记录。')
                return 0
            if any(src['error'] for src in current['sources']):
                print(json.dumps(current, ensure_ascii=False, indent=2))
                return 2
    finally:
        db.close()


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print('publish runtime: ' + str(exc), file=sys.stderr)
        raise SystemExit(2)
