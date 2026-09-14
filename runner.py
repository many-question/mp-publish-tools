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
from projection import VERSION, project


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


def current_status(db, cfg):
    current = collector.status(db, cfg)
    current.update(transport.sync_status())
    current['pending_chunks'] += current['local_pending_chunks']
    current['backfill_published'] = current['snapshot_scan_done'] and not current['pending_chunks'] and current['remote_contains_head']
    current['runtime_version'] = json.loads((Path(__file__).parent / 'release.json').read_text(encoding='utf-8'))['version']
    current['runtime_release_sha256'] = collector.PUBLISHER_RELEASE
    # The projection rules actually loaded, not the manifest of the bin baseline.
    current['projection_version'] = VERSION
    return current


def main():
    if sys.argv[1:] == ['--self-test']:
        assert project({'type':'user','message':{'role':'user','content':'fixture'}})['native']['message']['content'] == 'fixture'
        blocks = project({'role':'assistant','content':[
            {'type':'text','text':'visible'},
            {'type':'thinking','thinking':'hidden','signature':'sig'},
            {'type':'tool_use','id':'t1','name':'Bash','input':{'command':'ls -la','description':'d'}}]})['native']['content']
        assert blocks[0]['text'] == 'visible', 'assistant visible text must survive'
        assert blocks[1] == {'type': 'thinking'}, 'hidden reasoning must not survive'
        assert blocks[2]['input_head'] == 'ls -la' and 'input' not in blocks[2]
        assert len(blocks[2]['input_sha256']) == 64 and blocks[2]['input_bytes'] > 0
        print('runtime self-test OK (' + VERSION + ')')
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
    ap.add_argument('--batch-bytes', type=int, default=4*1024*1024, help=argparse.SUPPRESS)
    ap.add_argument('--chunk-mb', type=float, default=2, help='压缩后分块水位，0.0625 至 4 MiB')
    ap.add_argument('--checkpoint-seconds', type=float, default=60, help='本地扫描检查点间隔')
    ap.add_argument('-m', '--message', default='')
    args = ap.parse_args(sys.argv[2:])
    if args.restart_backfill and not args.backfill:
        ap.error('--restart-backfill requires --backfill')
    if args.batch_bytes < 1 or args.max_seconds < 0:
        raise ValueError('Budgets must be positive (max-seconds may be zero)')
    if not 0.0625 <= args.chunk_mb <= 4 or args.checkpoint_seconds <= 0:
        raise ValueError('Invalid compressed watermark or checkpoint interval')
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
    db = collector.connect(root, cfg, read_only=args.status)
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
            print(json.dumps(current_status(db, cfg), ensure_ascii=False, indent=2))
            return 0
        before = collector.status(db, cfg)
        bootstrapped = db.execute("SELECT value FROM meta WHERE key='bootstrapped'").fetchone()
        source_ready = not before['uninitialized_sources'] and all(s['baseline_done'] for s in before['sources'])
        if not args.backfill and (not bootstrapped or not source_ready):
            print('首次回填/来源重建尚未完成；请操作者继续 --backfill。')
            return transport.publish(args.message)
        stats = collector.collect(root, cfg, db, args.backfill, seconds=args.max_seconds,
                                  chunk_bytes=int(args.chunk_mb*1024*1024),
                                  checkpoint_seconds=args.checkpoint_seconds)
        print(json.dumps({'collection': stats}, ensure_ascii=False), flush=True)
        if stats['budget_exhausted']:
            print('扫描预算结束，数据和游标已在本地保存；再次运行继续，扫描完成后统一推送。')
            return 3
        started = time.monotonic()
        result = transport.publish(args.message or ('session backfill' if args.backfill else 'share update'),
                                   telemetry_only=args.backfill)
        current = current_status(db, cfg)
        current['publish_seconds'] = time.monotonic()-started
        atomic(inside(root, local_state(root) / 'status.json'), encode(current))
        print(json.dumps(current, ensure_ascii=False, indent=2))
        if result:
            return result
        return 2 if any(s['error'] for s in current['sources']) else 0
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
