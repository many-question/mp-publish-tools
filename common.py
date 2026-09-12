"""Workspace-local persistence. No user-home configuration or shared daemon."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import tempfile


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def inside(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError('Path must stay inside this workspace: ' + str(path))
    return path


def local_state(root):
    return inside(root, Path(root) / '.mp-publish')


def read_config(root):
    state = local_state(root)
    path = inside(root, state / 'config.json')
    if not path.exists():
        return None
    cfg = json.loads(path.read_text(encoding='utf-8'))
    if cfg.get('schema') != 'mp-publish-config/1' or Path(cfg['workspace']).resolve() != Path(root).resolve():
        raise ValueError('Configuration belongs to another workspace; rebind explicitly')
    if cfg.get('framework') not in {'codex', 'claude-code'}:
        raise ValueError('Unsupported framework')
    if not re.fullmatch('[a-f0-9]{32}', cfg.get('publisher_id', '')):
        raise ValueError('Invalid workspace publisher identity')
    if not isinstance(cfg.get('sources'), list) or not cfg['sources']:
        raise ValueError('At least one explicitly bound source is required')
    # Legacy share_total_mb is ignored: aggregate quota removed by operator policy.
    ids = [s['id'] for s in cfg['sources']]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate source binding IDs')
    return cfg
