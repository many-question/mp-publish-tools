#!/usr/bin/env python3
"""Private runtime. The launcher passes its fixed workspace as argv[1]."""
import argparse
import hashlib
import json
import os
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
from projection import ENV_HOST, ENV_SESSION, LOCAL_ID, PLAIN_ID, CODEX_ENV, VERSION, Desktop, CodexDesktop, mask, project, project_registry

# docs/multi-session.md 4.5：正式汇报只由主持 session 发。
HOST_ONLY = '正式汇报只由主持 session 发：请把结果交回主持。'
FAMILY_DIRS = ('registry_dir', 'projects_dir', 'notes_dir', 'codex_dir')


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
        paths, roots = args.source, args.family_root
    else:
        cfg = read_config(root)
        if not cfg:
            raise ValueError('Configure this workspace with --init first')
        paths, roots = args.add_source, args.add_family_root
    for name in paths:
        source = Path(name).resolve(strict=True)
        item = {'id': hashlib.sha256(str(source).encode()).hexdigest()[:24], 'path': str(source)}
        collector.source_path(root, item)
        if not any(s['path'] == item['path'] for s in cfg['sources']):
            cfg['sources'].append(item)
    family = dict(cfg.get('family') or {}, roots=list((cfg.get('family') or {}).get('roots') or []))
    for key in FAMILY_DIRS:
        if getattr(args, key):
            family[key] = str(Path(getattr(args, key)).resolve(strict=True))
    if roots or any(key in family for key in FAMILY_DIRS):
        desktop = (CodexDesktop if cfg['framework'] == 'codex' else Desktop)(root, family, os.environ, roots)
        for lid in roots:
            if not (PLAIN_ID if cfg['framework'] == 'codex' else LOCAL_ID).fullmatch(lid):
                raise ValueError('Codex 家族根必须是线程编号' if cfg['framework'] == 'codex' else 'A family root is a desktop session ID (local_...)')
            if lid not in desktop.entries:
                raise ValueError(('找不到 Codex 线程 ' + lid + ' 的会话记录；请检查 --codex-dir 或 CODEX_HOME') if cfg['framework'] == 'codex'
                                 else 'No desktop session registry for ' + lid + '; give --registry-dir')
            if not desktop.at_workspace(lid) or isinstance(desktop, CodexDesktop) and desktop.subagent(lid):
                raise ValueError(lid + ' does not run in this workspace; it cannot be a family root')
            if lid not in family['roots']:
                family['roots'].append(lid)
        # Pin the registry found now, so an operator terminal without desktop variables finds it later.
        directory = 'codex_dir' if cfg['framework'] == 'codex' else 'registry_dir'
        if roots and not family.get(directory):
            family[directory] = str(desktop.registry_dir)
        cfg['family'] = family
    atomic(path, encode(cfg))
    if args.add_family_root and not args.init:
        print('已绑定桌面版家族：之后每次 publish 自己发现主持开出的 session、分叉和子任务，在预算内自动回填，不需要再 --backfill。')
    else:
        print('已保存本工作区的独立绑定。由操作者执行 --backfill，完成后再使用普通 publish。')
    return 0


def stray_worktree(root):
    """执行 session 的 git worktree：没有本工作区的 publish 配置和 share/，或者提交进去的配置属于别的工作区。"""
    if not (root / '.git').is_file():
        return False
    config = local_state(root) / 'config.json'
    if not config.exists():
        return not (root / 'share' / '.git').exists()
    try:
        return Path(json.loads(config.read_text(encoding='utf-8')).get('workspace', '')).resolve() != root
    except (OSError, ValueError, AttributeError):
        return False


def family_view(root, cfg):
    """桌面版的 session 图。没有绑定家族、也不是从 Claude Code 里调用时为 None，行为与 3.2 相同。"""
    family = (cfg or {}).get('family') or {}
    saved = collector.saved_roots(root)
    if not (family.get('roots') or saved or os.environ.get(ENV_HOST) or os.environ.get(ENV_SESSION) or cfg and cfg['framework'] == 'codex' and os.environ.get(CODEX_ENV)):
        return None
    try:
        return (CodexDesktop if cfg and cfg['framework'] == 'codex' else Desktop)(root, family, os.environ, saved)
    except Exception as exc:
        # 发现不认识的类型或本机目录故障也不能阻断已有科研交付物。
        if cfg and cfg['framework'] == 'codex':
            from types import SimpleNamespace
            return SimpleNamespace(discovery_error=type(exc).__name__)
        if isinstance(exc, (OSError, ValueError, RecursionError)):
            return None
        raise


def current_status(db, cfg, detail=False):
    current = collector.status(db, cfg, detail)
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
        native = project({'type':'user','turnOrigin':'peer','promptSource':'system','message':{'role':'user','content':'x'},
                          'origin':{'kind':'peer','from':'local_a','hostInjected':True,'detail':{'x':1}}})['native']
        assert (native['turnOrigin'], native['promptSource']) == ('peer', 'system'), 'message provenance must survive'
        assert native['origin'] == {'kind':'peer','from':'local_a','hostInjected':True}, 'origin keeps only scalar keys'
        title = {'type':'custom-title','customTitle':'lane','title':'t','sessionId':'s'}
        assert project(title)['native'] == title, 'lane titles must survive'
        registry, _ = project_registry({'sessionId':'local_a','spawnedFrom':{'sessionId':'local_b','title':'t','prompt':'p'},
                                        'permissions':{'mode':'auto'},'sideSessionNotes':[{'report':'r','extra':1}]})
        assert registry == {'sessionId':'local_a','spawnedFrom':{'sessionId':'local_b','title':'t'},
                            'sideSessionNotes':[{'report':'r'}]}, 'registry keeps only the listed fields'
        redaction = []
        masked = mask({'title': 'key sk-' + 'a'*24, 'sessionId': 'local_a'}, transport.SECRET_PATTERNS, '/registry', redaction)
        assert masked['title']['masked'] and masked['sessionId'] == 'local_a', 'registry text is masked in place'
        assert redaction == [{'path': '/registry/title', 'reason': 'secret_check'}]
        delivery = {'type':'response_item','payload':{'type':'function_call_output','namespace':'codex_app',
            'name':'create_thread','output':'<codex_delegation>' + '全文'*200 + '</codex_delegation>'}}
        assert project(delivery)['native'] == delivery, 'Codex delivery must survive in full'
        secret = dict(delivery['payload'], output='sk-' + 'a'*24)
        assert project(secret, transport.SECRET_PATTERNS)['native']['output']['masked'], 'Codex delivery secrets are masked'
        message = {'type':'agent_message','author':'/root/a','recipient':'/root','content':'交回'}
        assert project(message)['native'] == message, 'Codex subagent addresses must survive'
        metadata = {'content_item_kinds':['user.text'], 'turn_id':'t',
                    'content_item_metadata':[{'provenance':{'type':'user','namespace':'n','hook':'h','private':1}}]}
        value = project({'type':'message','role':'user','internal_chat_message_metadata_passthrough':metadata})['native']
        assert value['internal_chat_message_metadata_passthrough']['content_item_metadata'][0]['provenance'] == {'type':'user','namespace':'n','hook':'h'}
        code = 'x' * 300 + ';tools.mcp__codex_app__create_thread({});tools.mcp__codex_app__fork_thread({});'
        call = project({'type':'custom_tool_call','name':'exec','input':code})['native']
        assert call['codex_app_calls'] == ['create_thread', 'fork_thread'], 'Codex calls survive outside the input head'
        output = [{'type':'input_text','text':'Script completed\nOutput:\n'}, {'type':'input_text','text':json.dumps(
            {'content':[{'type':'text','text':json.dumps({'threadId':'fixture-thread','hostId':'local'})}]})}]
        result = project({'type':'custom_tool_call_output','output':output})['native']
        assert result['codex_app_results'] == [{'threadId':'fixture-thread'}], 'Codex thread results survive list output'
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
    modes.add_argument('--add-family-root', action='append', default=[], help='追加绑定一个桌面版主持 session（local_…）')
    ap.add_argument('--family-root', action='append', default=[],
                    help='和 --init 一起：桌面版主持 session（local_…），它和它开出的 session 一起采集')
    ap.add_argument('--codex-dir', help='Codex 会话记录目录（缺省 $CODEX_HOME 或 ~/.codex）')
    ap.add_argument('--registry-dir', help='桌面版会话登记目录（缺省：标准位置）')
    ap.add_argument('--projects-dir', help='Claude Code 日志根目录（缺省 ~/.claude/projects）')
    ap.add_argument('--notes-dir', help='共享笔记根目录（缺省：桌面版的 side-session-notes）')
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
    if stray_worktree(root):
        print(HOST_ONLY + '（这里是一个 git worktree，没有本工作区的 publish 配置和 share/。）', file=sys.stderr)
        return 2
    cfg = read_config(root)
    desktop = None if args.status or args.check else family_view(root, cfg)
    if desktop is not None and not getattr(desktop, 'discovery_error', None):
        role, caller = desktop.classify(set(desktop.bound) | set(collector.saved_roots(root)))
        if role == 'member':
            print(HOST_ONLY + '（' + caller + ' 是本工作区主持开出来的 session，不是主持本身。）', file=sys.stderr)
            return 2
    transport.configure(root)
    if args.init or args.add_source or args.add_family_root:
        return bind(root, args)
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
            status = current_status(db, cfg, detail=True)
            print(json.dumps(status, ensure_ascii=False, indent=2))
            if status['family'].get('missing_sources'):
                print('这份会话记录在采完之前消失，可能被 Codex 迁进了历史库；之后的内容收不到。', file=sys.stderr)
            return 0
        collector.mark_missing_sources(root, cfg, db)
        before = collector.status(db, cfg)
        bootstrapped = db.execute("SELECT value FROM meta WHERE key='bootstrapped'").fetchone()
        # Only operator-bound sources gate collection; family sources are backfilled automatically.
        source_ready = not before['uninitialized_sources'] and all(s['baseline_done'] or cfg['framework'] == 'codex' and s['error'] == collector.MISSING for s in before['sources'])
        ended_missing = cfg['framework'] == 'codex' and any(s['error'] == collector.MISSING for s in before['sources'])
        if not args.backfill and ((not bootstrapped and not ended_missing) or not source_ready):
            print('首次回填/来源重建尚未完成；请操作者继续 --backfill。')
            return transport.publish(args.message)
        family = collector.discover(root, cfg, db, desktop) if desktop is not None else None
        stats = collector.collect(root, cfg, db, args.backfill, seconds=args.max_seconds,
                                  chunk_bytes=int(args.chunk_mb*1024*1024),
                                  checkpoint_seconds=args.checkpoint_seconds, family_seconds=collector.FAMILY_SECONDS)
        if family is not None:
            stats['family'] = family
            if family.get('error'):
                print('这一次没有做家族发现（' + family['error'] + '），已记一条采集观察；已有的会话记录照常采集，交付照常。', file=sys.stderr)
        print(json.dumps({'collection': stats}, ensure_ascii=False), flush=True)
        if stats['budget_exhausted']:
            print('扫描预算结束，数据和游标已在本地保存；再次运行继续，扫描完成后统一推送。')
            return 3
        if stats['family_budget_exhausted']:
            print('新发现的家族会话记录本次没有回填完（已收的部分和游标已保存），下次 publish 接着收。')
        started = time.monotonic()
        result = transport.publish(args.message or ('session backfill' if args.backfill else 'share update'),
                                   telemetry_only=args.backfill)
        current = current_status(db, cfg)
        current['publish_seconds'] = time.monotonic()-started
        atomic(inside(root, local_state(root) / 'status.json'), encode(current))
        print(json.dumps(current, ensure_ascii=False, indent=2))
        if current['family'].get('missing_sources'):
            print('这份会话记录在采完之前消失，可能被 Codex 迁进了历史库；之后的内容收不到。', file=sys.stderr)
        if current['family']['blocked']:
            print('会话登记或共享笔记命中秘密检查，留在本地没有上传：' + '、'.join(current['family']['blocked']), file=sys.stderr)
        if result:
            return result
        return 2 if any(s['error'] and not (cfg['framework'] == 'codex' and s['error'] == collector.MISSING) for s in current['sources']) or current['family']['errors'] or current['family']['blocked'] else 0
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
