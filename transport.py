#!/usr/bin/env python3
"""把 share/ 目录交给人类研究员。

用法:
    python bin/publish.py                     # 提交并推送 share/ 的当前内容
    python bin/publish.py -m "第 7 次汇报"     # 自定义说明
    python bin/publish.py --check             # 只做检查，不提交也不推送

只有 share/ 目录会被交出去；工作区其他内容始终留在本地。
推送前会检查文件体积、明显的密钥字样，以及 share/reports/ 下汇报的格式。
"""
from __future__ import annotations

import argparse
import datetime
import gzip
import zlib
import os
import pathlib
import re
import subprocess
import sys

MAX_FILE_MB = 5

SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "GitHub token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "API key"),
    (re.compile(r"(?i)\b(password|passwd|secret_key)\s*[:=]\s*\S{6,}"), "明文口令"),
]
TEXT_SUFFIXES = {
    ".md", ".txt", ".yaml", ".yml", ".json", ".csv", ".py", ".m", ".scs",
    ".sp", ".cir", ".va", ".v", ".sv", ".tcl", ".il", ".sh", ".cfg", ".ini",
    ".log", ".rst", ".toml", ".jsonl",
}

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHARE = ROOT / "share"


def git(*args, check=True, binary=False):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    for name in ('GIT_DIR','GIT_COMMON_DIR','GIT_WORK_TREE','GIT_INDEX_FILE','GIT_OBJECT_DIRECTORY','GIT_ALTERNATE_OBJECT_DIRECTORIES'):
        env.pop(name, None)
    try:
        proc = subprocess.run(
            ["git", "-C", str(SHARE), *args],
            capture_output=True, **({} if binary else dict(text=True, encoding='utf-8', errors='replace')),
            timeout=600 if args[0] == 'push' else 60,
            env=env,
        )
    except subprocess.TimeoutExpired:
        # Large pre-upgrade backlogs can exceed one minute. A timeout never
        # acknowledges data, and does not print command arguments or credentials.
        proc = subprocess.CompletedProcess(['git', args[0]], 124, b'' if binary else '', 'Git operation timed out; pending data retained')
    if check and proc.returncode != 0:
        print('share Git operation failed: ' + args[0], file=sys.stderr)
        raise SystemExit(1)
    return proc


def check_key() -> list[str]:
    """确认本 run 的专用密钥确实存在。

    ssh 在 -i 指定的文件不存在时会退回到默认身份（~/.ssh/id_*），那会用
    别人的身份连上服务器。必须在推送前拦住这种情况，让它失败而不是
    悄悄用错身份。
    """
    proc = git("config", "--get", "core.sshCommand", check=False)
    cmd = (proc.stdout or "").strip()
    if not cmd:
        return []  # 未配置专用密钥（例如自建服务用其他方式认证），不拦
    m = re.search(r'-i\s+"([^"]+)"|-i\s+(\S+)', cmd)
    if not m:
        return []
    key = pathlib.Path(m.group(1) or m.group(2))
    if key.exists():
        return []
    return [
        f"本 run 的专用密钥不存在: {key}\n"
        "      在密钥就位之前不能推送，否则会用本机默认身份连上服务器。\n"
        "      请联系人类研究员。"
    ]


def collect_files() -> list[pathlib.Path]:
    out = []
    share_root = SHARE.resolve()
    for folder, dirs, files in os.walk(SHARE, followlinks=False):
        dirs[:] = [d for d in dirs if d != '.git']
        for d in dirs:
            p = pathlib.Path(folder) / d
            if p.is_symlink() or not p.resolve().is_relative_to(share_root):
                raise ValueError('share must not include links to other workspaces')
        for name in files:
            p = pathlib.Path(folder) / name
            if p.is_symlink() or not p.resolve().is_relative_to(share_root):
                raise ValueError('share must not include links to other workspaces')
            out.append(p)
    return out


def check_sizes(files) -> list[str]:
    problems, total = [], 0
    for f in files:
        size = f.stat().st_size
        total += size
        if size > MAX_FILE_MB * 1024 * 1024:
            problems.append(
                f"{f.relative_to(ROOT).as_posix()} 有 {size/1048576:.1f} MB，"
                f"超过单文件上限 {MAX_FILE_MB} MB。"
                "大数据请留在本地，在汇报里写明路径、哈希和重新生成的方法。"
            )
    return problems


def check_secrets(files) -> list[str]:
    problems = []
    for f in files:
        if f.name.endswith('.jsonl.gz') and f.is_relative_to(SHARE / 'telemetry'):
            try:
                with gzip.open(f, 'rb') as fh:
                    raw = fh.read(256*1024 + 1)
                if len(raw) > 256*1024:
                    raise ValueError('Telemetry chunk expands beyond its block limit')
                from projection import check_text
                for line in raw.decode('utf-8').splitlines():
                    check_text(line, SECRET_PATTERNS)
            except (OSError, ValueError, EOFError, zlib.error):
                problems.append('压缩遥测完整性或秘密检查失败: ' + str(f.relative_to(SHARE)))
            continue
        if f.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for pat, label in SECRET_PATTERNS:
            m = pat.search(text)
            if m:
                line = text[: m.start()].count("\n") + 1
                problems.append(
                    f"{f.relative_to(ROOT).as_posix()}:{line} 疑似包含{label}，已阻止推送"
                )
                break
        if f.suffix.lower() == '.jsonl':
            from projection import check_text
            try:
                for line in text.splitlines():
                    if line.strip():
                        check_text(line, SECRET_PATTERNS)
            except ValueError:
                problems.append(f'{f.relative_to(ROOT).as_posix()}: JSONL content check failed')
    return problems


def check_reports() -> list[str]:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    try:
        from validate_report import validate_file
    except ImportError:
        return ["跳过汇报格式校验：找不到 bin/validate_report.py"]
    problems = []
    reports = sorted((SHARE / "reports").glob("*.yaml")) if (SHARE / "reports").is_dir() else []
    if not reports:
        problems.append("提示：share/reports/ 下还没有任何汇报文件（不阻止推送）")
        return problems
    for r in reports:
        for m in validate_file(r, ROOT):
            if m.startswith("SKIP:"):
                problems.append(m)
            else:
                problems.append(f"{r.relative_to(ROOT).as_posix()}  {m}")
    return problems


def configure(root):
    global ROOT, SHARE
    ROOT = pathlib.Path(root).resolve()
    SHARE = ROOT / 'share'
    if not SHARE.resolve().is_relative_to(ROOT) or SHARE.is_symlink():
        raise ValueError('share must be inside this workspace')
    if not (SHARE / '.git').exists():
        raise ValueError('This workspace needs its own configured share Git repository')
    if pathlib.Path(git('rev-parse','--show-toplevel').stdout.strip()).resolve() != SHARE.resolve():
        raise ValueError('share resolved to a different repository')
    if not pathlib.Path(git('rev-parse','--absolute-git-dir').stdout.strip()).resolve().is_relative_to(ROOT):
        raise ValueError('share Git state must belong to this workspace')


def publish(message='', check=False, telemetry_only=False) -> int:

    if not (SHARE / ".git").exists():
        print(f"错误: {SHARE} 不是一个已配置的仓库。请联系人类研究员。", file=sys.stderr)
        return 1

    if pathlib.Path(git('rev-parse', '--show-toplevel').stdout.strip()).resolve() != SHARE.resolve():
        raise ValueError('share Git repository resolved outside the share directory')

    for problem in check_key():
        print(f"错误: {problem}", file=sys.stderr)
        return 1

    files = collect_files()
    if telemetry_only:
        files = [f for f in files if f.is_relative_to(SHARE / 'telemetry')]
    hard = check_sizes(files) + check_secrets(files)
    soft = [] if telemetry_only else check_reports()

    blocking = [m for m in soft if not m.startswith(("提示：", "跳过"))]
    if blocking:
        print("汇报格式有问题，请先修正：")
        for m in blocking:
            print(f"  - {m}")
        print()
    for m in soft:
        if m.startswith(("提示：", "跳过")):
            print(m)

    if hard:
        print("以下问题必须解决后才能推送：", file=sys.stderr)
        for m in hard:
            print(f"  - {m}", file=sys.stderr)
        return 1
    if blocking:
        return 1

    if check:
        print(f"检查通过：share/ 共 {len(files)} 个文件。")
        return 0

    scope = ('--', 'telemetry') if telemetry_only else ()
    git("add", "-A", *scope)
    staged = git("diff", "--cached", "--name-only", *scope).stdout.strip()
    if staged:
        stamp = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
        msg = message.strip() or "share update"
        if telemetry_only:
            git('commit', '--only', '-m', f'{msg} ({stamp})', '--', 'telemetry')
        else:
            git("commit", "-m", f"{msg} ({stamp})")

    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    upstream = git("rev-parse", "--abbrev-ref", "@{upstream}", check=False)
    has_upstream = upstream.returncode == 0
    if has_upstream:
        pending = git("log", "--oneline", "@{upstream}..HEAD").stdout.strip()
        if not staged and not pending:
            print("share/ 没有变化，也没有待推送的提交。")
            return 0

    if has_upstream:
        proc = git("push", check=False)
    else:
        proc = git("push", "-u", "origin", branch, check=False)
    if proc.returncode != 0:
        print("推送失败：", file=sys.stderr)
        print('远端未确认本次推送，请检查 share 的网络和认证配置。', file=sys.stderr)
        print(
            "\n改动已在本地提交，不会丢失。网络恢复后重新运行本命令即可。",
            file=sys.stderr,
        )
        return 1

    if staged:
        names = staged.splitlines()
        print("已交付以下文件：")
        for name in names:
            print(f"  {name}")
        print(f"\n共 {len(names)} 项。")
    else:
        print("已把之前未推送成功的提交补交上去。")
    return 0
