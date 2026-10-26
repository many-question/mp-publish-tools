"""Structural projection v3; does not classify people, deduplicate or sum usage.

Exported: the listed metadata, user text (unchanged), assistant visible text and
bounded tool call/result heads. Hidden reasoning, full tool arguments and full
tool output never leave the environment. Unknown field names are inventoried;
their values stay local and can be collected by a later version.

skeleton-3 也规定桌面版的会话登记与共享笔记导出什么，以及哪些 session 属于
一个研究 agent 的家族（Desktop：只读登记的关系字段，按编号找成员的日志）。
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

VERSION = 'skeleton-3'
SCALARS = set('''type subtype role id uuid parentUuid sessionId session_id parent_session_id
thread_id parent_thread_id turn_id parent_turn_id root_turn_id call_id parent_call_id
tool_use_id parent_tool_use_id message_id reply_to timestamp time created_at started_at
response_id ordinal subagent_history_start_ordinal history_mode multi_agent_version window_id thread_source
apiBlockIndex logicalParentUuid retryInMs retryAttempt maxRetries toolUseID userType entrypoint
finished_at ended_at duration duration_ms durationMs duration_api_ms elapsed_ms
start_time end_time ts cwd model model_provider provider version cli_version originator
source origin trigger isSidechain isMeta isCompactSummary is_error status exit_code
attempt max_retries retry_delay_ms stop_reason stop_sequence requestId request_id
request_type name tool_name tool server channel phase agent_id agentId forked_from_id
num_turns total_cost_usd cost_usd index sequence sequence_number retry_count
turnOrigin promptSource agentName customTitle'''.split())
# skeleton-3：origin 是对象时只留这些标量键（消息来自研究人员、其他 session 还是后台任务）。
ORIGIN_KEYS = ('kind', 'from', 'producer', 'hostInjected', 'fromMode', 'runId')
# 道的标签：这两种记录除 agentName、customTitle、name 外另留 title。
TITLE_RECORDS = {'agent-name', 'custom-title'}
TITLE_KEYS = {'title'}
CONTAINERS = {'payload', 'message', 'event', 'data', 'item', 'invocation', 'delta', 'source', 'thread_source', 'context_window'}
USAGE = {'usage', 'token_usage', 'total_token_usage', 'last_token_usage', 'modelUsage',
         'turn_token_usage', 'thread_token_usage',
         'input_token_details', 'output_token_details', 'cached_tokens', 'cache_creation',
         'info', 'rate_limits', 'duration'}
TEXT_TYPES = {'text', 'input_text', 'output_text', 'Text'}
USER_KINDS = {'user', 'user_message'}
# Native assistant carriers whose visible text is exported in full.
ASSISTANT_KINDS = {'assistant', 'assistant_message', 'agent_message', 'AgentMessage'}
# Hidden reasoning: only the native marker and listed IDs survive, never the body.
REASONING_KINDS = {'thinking', 'redacted_thinking', 'reasoning', 'Reasoning', 'reasoning_text',
                   'encrypted_reasoning', 'summary_text', 'agent_reasoning', 'agent_reasoning_delta',
                   'agent_reasoning_raw_content', 'agent_reasoning_section_break'}
# Tool records keep identity plus a bounded head; arguments/output stay local.
CALL_KINDS = {'tool_use', 'server_tool_use', 'function_call', 'custom_tool_call',
              'local_shell_call', 'McpToolCall'}
RESULT_KINDS = {'tool_result', 'function_call_output', 'custom_tool_call_output',
                'local_shell_call_output', 'web_search_tool_result', 'McpToolCall'}
CALL_INPUT_KEYS = ('input', 'arguments', 'action')
RESULT_OUTPUT_KEYS = ('content', 'output', 'result')
# Recognizable argument fields, in order; a command array is joined with spaces.
HEAD_KEYS = ('command', 'cmd', 'file_path', 'path', 'pattern', 'url', 'description', 'prompt')
INPUT_HEAD_CHARS = 240
OUTPUT_HEAD_CHARS = 120


def canonical(value):
    """Bytes a head describes: a string as written, anything else canonical JSON."""
    if isinstance(value, str):
        return value.encode('utf-8')
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def joined(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        return ' '.join(value)
    return None


def input_head(value, depth=0):
    """Prefer a recognizable argument; None means fall back to serialization."""
    if isinstance(value, str):
        # Frameworks that pass arguments as a JSON string still name their fields.
        if depth == 0 and value[:1] in ('{', '['):
            try:
                inner = json.loads(value)
            except ValueError:
                inner = None
            if isinstance(inner, (dict, list)):
                found = input_head(inner, depth + 1)
                if found is not None:
                    return found
        return value
    if isinstance(value, dict):
        for key in HEAD_KEYS:
            text = joined(value.get(key))
            if text:
                return text
        if depth < 3:
            for key in sorted(value):
                if isinstance(value[key], dict):
                    found = input_head(value[key], depth + 1)
                    if found is not None:
                        return found
        return None
    return joined(value)


def output_head(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get('text')
                parts.append(text if isinstance(text, str) else '<' + str(item.get('type')) + '>')
        return '\n'.join(parts)
    return None


# 紧凑 JSON 里一个字符串后面至少还跟着 `,`、`}` 之类的字符；秘密模式可能从字符串里一直匹配进去。
FOLLOWING = ',"":,"":'


def hits(text, patterns):
    """和 check_text 同一判据：原文，以及它在序列化记录里的样子（转义之后、后面还跟着别的内容）。
    宁可多遮：遮盖之后的记录必须能通过 check_text，否则整条被拦。"""
    if not patterns:
        return False
    serialized = json.dumps(text, ensure_ascii=False) + FOLLOWING
    return any(pattern.search(text) or pattern.search(serialized) for pattern, _ in patterns)


def project(record, patterns=None):
    """Patterns enable in-place masking of assistant text and tool heads.

    User text keeps the skeleton-1 behaviour: a match blocks the whole record and
    nothing is edited locally. Masked fields are marked, never passed off as verbatim.
    """
    omitted, redaction = [], []

    def guard(text, where):
        # skeleton-3: same test as check_text, so a masked record is no longer blocked as a whole.
        if hits(text, patterns):
            redaction.append({'path': where, 'reason': 'secret_check'})
            return {'masked': True, 'reason': 'secret_check',
                    'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
        return text

    def head(value, path, out):
        """Bounded identity of a tool call/result; the payload itself is omitted."""
        kind, consumed = value.get('type'), set()
        for kinds, keys, extract, limit, label in (
                (CALL_KINDS, CALL_INPUT_KEYS, input_head, INPUT_HEAD_CHARS, 'input'),
                (RESULT_KINDS, RESULT_OUTPUT_KEYS, output_head, OUTPUT_HEAD_CHARS, 'output')):
            if kind not in kinds:
                continue
            for key in keys:
                if key not in value or key in consumed:
                    continue
                raw = value[key]
                text = extract(raw)
                if text is None:
                    text = json.dumps(raw, ensure_ascii=False, sort_keys=True)
                blob = canonical(raw)
                out[label + '_head'] = guard(text[:limit], path + '/' + label + '_head')
                out[label + '_bytes'] = len(blob)
                out[label + '_sha256'] = hashlib.sha256(blob).hexdigest()
                consumed.add(key)
                break
        return consumed

    def walk(value, path='', user=False, assistant=False):
        if isinstance(value, list):
            return [walk(v, path + '[]', user, assistant) for v in value]
        if not isinstance(value, dict):
            return value if isinstance(value, (int, float, bool)) or value is None else None
        kind = value.get('type')
        # Native roles are retained; this is not a human/automation classification.
        local_user = value.get('role') == 'user' or kind in USER_KINDS
        local_assistant = value.get('role') == 'assistant' or kind in ASSISTANT_KINDS
        assistant_here = (local_assistant or assistant) and not (local_user or user)
        hidden = kind in REASONING_KINDS
        user_text = not hidden and (local_user or (user and kind in TEXT_TYPES))
        assistant_text = not hidden and assistant_here and (local_assistant or kind in TEXT_TYPES)
        out = {}
        consumed = head(value, path, out) if kind in CALL_KINDS or kind in RESULT_KINDS else set()
        for key, item in value.items():
            where = path + '/' + key
            if key in consumed:
                # The head above describes it; the full argument/output stays local.
                omitted.append(where)
            elif key in SCALARS and not isinstance(item, (dict, list)):
                out[key] = item
            elif key == 'origin' and isinstance(item, dict):
                out[key] = {k: v for k, v in item.items() if k in ORIGIN_KEYS and scalar(v)}
                omitted.extend(where + '/' + k for k in item if k not in out[key])
            elif key in TITLE_KEYS and kind in TITLE_RECORDS and scalar(item):
                out[key] = item
            elif key in {'source', 'thread_source'} and (kind == 'session_meta' or
                    (record.get('type') == 'session_meta' and path == '/payload')):
                # Native session provenance is needed centrally to distinguish
                # the root task from framework-created helper sessions.
                out[key] = item
            elif key in USAGE and isinstance(item, (dict, list, int, float)):
                # info is usage-bearing only on native token events.
                if key != 'info' or kind == 'token_count':
                    out[key] = item
                else:
                    omitted.append(where)
            elif key in CONTAINERS and isinstance(item, (dict, list)) and not hidden:
                out[key] = walk(item, where, local_user or user, assistant_here)
            elif key in {'content', 'text', 'message'} and isinstance(item, str) and (user_text or assistant_text):
                out[key] = item if user_text else guard(item, where)
            elif key == 'content' and isinstance(item, list) and not hidden:
                out[key] = [walk(part, where + '[]', local_user, assistant_here) for part in item]
            else:
                omitted.append(where)
        return out
    if not isinstance(record, dict):
        raise ValueError('Expected a native JSON object')
    native = walk(record)
    event = {'native': native, 'projection_version': VERSION, 'omitted_fields': omitted,
             'extraction_status': 'projected' if native else 'unrecognized_structure'}
    if redaction:
        event['redaction'] = redaction
    return event


def check_text(text, patterns):
    # Scan decoded strings as well as serialized text; JSON escapes cannot hide a key.
    if any(pattern.search(text) for pattern, _ in patterns):
        raise ValueError('Projected data matched the secret check; source retained locally')
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for key, val in value.items():
                yield key
                yield from strings(val)
        elif isinstance(value, list):
            for val in value:
                yield from strings(val)
    for value in strings(json.loads(text)):
        if any(pattern.search(value) for pattern, _ in patterns):
            raise ValueError('Projected data matched the secret check; source retained locally')


def scalar(value):
    return value is None or isinstance(value, (str, int, float, bool))


def mask(value, patterns, path, redaction):
    """像 assistant 正文那样就地遮盖命中秘密检查的文字：换成标记，路径记进 redaction。"""
    if isinstance(value, str):
        if hits(value, patterns):
            redaction.append({'path': path, 'reason': 'secret_check'})
            return {'masked': True, 'reason': 'secret_check', 'sha256': hashlib.sha256(value.encode('utf-8')).hexdigest()}
        return value
    if isinstance(value, list):
        return [mask(v, patterns, path + '[]', redaction) for v in value]
    if isinstance(value, dict):
        return {k: mask(v, patterns, path + '/' + k, redaction) for k, v in value.items()}
    return value


# ---------------------------------------------------------------- 会话登记（mp-session-registry/1）

# 只取 docs/multi-session.md 3.2 表里的字段；权限、界面状态、工具快照等不读不传。
REGISTRY_SCALARS = ('sessionId', 'cliSessionId', 'forkedFromSessionId', 'forkedAtMessageUuid', 'cwd', 'originCwd',
                    'worktreePath', 'worktreeName', 'branch', 'sourceBranch', 'title', 'model', 'effort',
                    'createdAt', 'lastActivityAt', 'completedTurns', 'isArchived')
# 表里没列子键的，原样带（只有 JSON 值，深度有限）。
REGISTRY_PLAIN = ('priorCliSessionIds', 'writtenBranches', 'previousTitles', 'prs', 'lastTurnReport')
REGISTRY_OBJECTS = {'spawnedFrom': ('sessionId', 'taskId', 'title', 'name', 'sideSession', 'userFork', 'handOff',
                                    'sharedCheckout', 'wakeParent', 'btw'),
                    'postTurnSummary': ('status_category', 'status_detail', 'needs_action')}
REGISTRY_LISTS = {'sideSessionNotes': ('at', 'batch', 'messageUuid', 'report', 'sessionId', 'title'),
                  'peerReceipts': ('messageId', 'targetSessionId', 'toolUseId', 'sentAt', 'deliveredAt', 'readAt',
                                   'repliedAt')}
PLAIN_DEPTH = 6


def plain(value, depth=0):
    """这个值是不是能原样带的 JSON（深度有限）。不是的整个丢掉，键名记进 unrecognized，不留半截。"""
    if scalar(value):
        return True
    if depth < PLAIN_DEPTH and isinstance(value, list):
        return all(plain(v, depth + 1) for v in value)
    if depth < PLAIN_DEPTH and isinstance(value, dict):
        return all(isinstance(k, str) and plain(v, depth + 1) for k, v in value.items())
    return False


def project_registry(entry):
    """(登记投影, 不认识的字段)。列出的字段类型不对时值丢掉、键名计入第二项，不静默当成没有。"""
    registry, unrecognized = {}, []

    def pick(value, keys, where):
        out = {}
        for key in keys:
            if key in value:
                if plain(value[key]):
                    out[key] = value[key]
                else:
                    unrecognized.append(where + '.' + key)
        return out
    for key in REGISTRY_SCALARS:
        if key in entry:
            if scalar(entry[key]):
                registry[key] = entry[key]
            else:
                unrecognized.append(key)
    for key in REGISTRY_PLAIN:
        if key in entry:
            if plain(entry[key]):
                registry[key] = entry[key]
            else:
                unrecognized.append(key)
    for key, keys in REGISTRY_OBJECTS.items():
        if key in entry:
            if isinstance(entry[key], dict):
                registry[key] = pick(entry[key], keys, key)
            elif entry[key] is None:
                registry[key] = None
            else:
                unrecognized.append(key)
    for key, keys in REGISTRY_LISTS.items():
        if key in entry:
            if isinstance(entry[key], list):
                registry[key] = [pick(item, keys, key + '[]') for item in entry[key] if isinstance(item, dict)]
                if len(registry[key]) != len(entry[key]):
                    unrecognized.append(key + '[]')
            elif entry[key] is None:
                registry[key] = None
            else:
                unrecognized.append(key)
    return registry, sorted(set(unrecognized))


# ---------------------------------------------------------------- 共享笔记（mp-shared-note/1）

NOTE_TEXT_SUFFIXES = ('.md', '.txt', '.json', '.csv')
NOTE_TEXT_BYTES = 1024 * 1024


def project_note(name, size, modified, digest, data=None, patterns=None):
    """一个笔记文件的清单项；后缀是文本、不超过 1 MiB 时带全文（data 是文件的全部字节）。
    正文命中秘密检查时不带正文、清单照发；文件名命中由调用方整条留在本地。"""
    note = {'name': name, 'size': size, 'modified': modified, 'sha256': digest}
    if name.lower().endswith(NOTE_TEXT_SUFFIXES):
        if size > NOTE_TEXT_BYTES or data is None:
            note['text_withheld'] = 'too_large'
        else:
            try:
                text = data.decode('utf-8')
            except UnicodeDecodeError:
                note['text_withheld'] = 'not_utf8'
            else:
                if hits(text, patterns):
                    note['text_withheld'] = 'secret_check'
                else:
                    note['text'] = text
    return note


# ---------------------------------------------------------------- 桌面版的 session 家族（4.1）

LOCAL_ID = re.compile(r'local_[A-Za-z0-9-]{1,80}')
PLAIN_ID = re.compile(r'[A-Za-z0-9-]{1,80}')
ENV_SESSION, ENV_HOST = 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_CODE_HOST_SESSION_ID'
ENV_ACCOUNT, ENV_ORG = 'CLAUDE_CODE_ACCOUNT_UUID', 'CLAUDE_CODE_ORGANIZATION_UUID'


def desktop_homes(env):
    """桌面版可能的数据目录，新的在前。

    较新的 Windows 桌面版放在 %LOCALAPPDATA%\\Claude-Data\\<配置名>\\（2026-10 实测），较早的放在 %APPDATA%\\Claude；
    两处都找。macOS 是 ~/Library/Application Support/Claude，Linux 是 $XDG_CONFIG_HOME/Claude。最后一个总是老位置。"""
    homes = []
    if os.name == 'nt':
        local = Path(env.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local') / 'Claude-Data'
        try:
            homes += sorted(d for d in local.iterdir() if d.is_dir())
        except OSError:
            pass
        base = env.get('APPDATA') or Path.home() / 'AppData' / 'Roaming'
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support'
    else:
        base = env.get('XDG_CONFIG_HOME') or Path.home() / '.config'
    return homes + [Path(base) / 'Claude']


def desktop_home(env):
    """有会话登记的那个数据目录；都没有时取老位置。"""
    homes = desktop_homes(env)
    return next((h for h in homes if (h / 'claude-code-sessions').is_dir()), homes[-1])


def default_projects(env):
    """Claude Code 的日志根目录 ~/.claude/projects。"""
    config = env.get('CLAUDE_CONFIG_DIR')
    return Path(config) / 'projects' if config else Path.home() / '.claude' / 'projects'


def caller_registry(env):
    """调用者环境里的会话登记目录 <账号>/<组织>（3.1）；不是桌面版 session 时为 None。"""
    account, org = env.get(ENV_ACCOUNT, ''), env.get(ENV_ORG, '')
    if PLAIN_ID.fullmatch(account) and PLAIN_ID.fullmatch(org):
        paths = [home / 'claude-code-sessions' / account / org for home in desktop_homes(env)]
        return next((p for p in paths if p.is_dir()), paths[-1])
    return None


def registry_candidates(env):
    """本机的会话登记目录：调用者环境里的那个在前，其后是其余已有的。"""
    out = [d for d in (caller_registry(env),) if d is not None]
    for home in desktop_homes(env):
        try:
            found = sorted(d for d in (home / 'claude-code-sessions').glob('*/*') if d.is_dir())
        except OSError:
            found = []
        out += [d for d in found if d not in out]
    return out


def notes_home(registry_dir, env):
    """共享笔记的根目录：和会话登记在同一个桌面版数据目录下
    （<数据目录>/claude-code-sessions/<账号>/<组织> → <数据目录>/side-session-notes）。"""
    if registry_dir is not None and Path(registry_dir).parent.parent.name == 'claude-code-sessions':
        return Path(registry_dir).parent.parent.parent / 'side-session-notes'
    return desktop_home(env) / 'side-session-notes'


def encode_cwd(path):
    """日志目录名：路径里非字母数字的字符换成 -（3.3）。"""
    return re.sub(r'[^A-Za-z0-9]', '-', str(path))


def normal(value):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        return None
    try:
        return Path(os.path.normcase(os.path.abspath(value)))
    except (OSError, ValueError):
        return None


def real(path):
    """解析链接与目录联接之后的位置（比较用）。"""
    return Path(os.path.normcase(os.path.realpath(path)))


def within(path, root):
    """path 自身不是链接或 Windows 目录联接，解析之后仍在 root（已经 real() 过）之下。"""
    try:
        junction = getattr(os.path, 'isjunction', None)
        if path.is_symlink() or junction and junction(path):
            return False
        return real(path).is_relative_to(root)
    except (OSError, ValueError):
        return False


class Desktop:
    """桌面版的 session 图。只读各 session 登记里的关系与位置字段；按日志编号找家族成员的日志，不读别的日志。

    family 是 publish 配置里的 family 段：roots（setup 绑定的根）、registry_dir、projects_dir、notes_dir，
    目录缺省用桌面版的标准位置。extra_roots 只用来在没有指定登记目录时找到它。
    """

    def __init__(self, workspace, family=None, env=None, extra_roots=()):
        env = os.environ if env is None else env
        family = family or {}
        self.workspace = normal(str(workspace))
        self.bound = [r for r in family.get('roots') or [] if isinstance(r, str) and LOCAL_ID.fullmatch(r)]
        host, session = env.get(ENV_HOST, ''), env.get(ENV_SESSION, '')
        self.caller_host = host if LOCAL_ID.fullmatch(host) else None
        self.caller_cli = session if PLAIN_ID.fullmatch(session) else None
        if family.get('registry_dir'):
            self.registry_dir = Path(family['registry_dir'])
        else:
            # 没有指定时：含有某个根（或调用者）登记的那个目录，否则调用者环境里的。
            wanted = [r for r in (*self.bound, *extra_roots, self.caller_host) if r]
            self.registry_dir = next((d for d in registry_candidates(env) if any((d / (r + '.json')).is_file() for r in wanted)),
                                     caller_registry(env)) if wanted else caller_registry(env)
        self.projects_dir = Path(family['projects_dir']) if family.get('projects_dir') else default_projects(env)
        self.notes_dir = Path(family['notes_dir']) if family.get('notes_dir') else notes_home(self.registry_dir, env)
        # 日志和笔记只认这两个目录里面的：不跟随链接与目录联接（含指向外面的子目录、文件）。
        self.projects_root, self.notes_root = real(self.projects_dir), real(self.notes_dir)
        self.entries, self.unreadable, self._dirs = {}, [], None
        self.registry_ok = self.registry_dir is not None and self.registry_dir.is_dir()
        if self.registry_ok:
            for path in sorted(self.registry_dir.glob('local_*.json')):
                if not LOCAL_ID.fullmatch(path.stem):
                    continue
                entry = self.read(path)
                if entry is None and path.stem == self.caller_host:
                    # 登记是原地改写的，调用者自己的可能刚好读到一半：再读一次。
                    time.sleep(0.05)
                    entry = self.read(path)
                if entry is None:
                    self.unreadable.append(path.stem)
                else:
                    self.entries[path.stem] = entry
        self.children = {}
        for lid in self.entries:
            for relation, parent in self.parents(lid):
                self.children.setdefault(parent, []).append((lid, relation))

    @staticmethod
    def read(path):
        try:
            entry = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError, RecursionError):
            return None
        return entry if isinstance(entry, dict) else None

    def parents(self, lid):
        """[(关系, 上级编号)]：spawnedFrom.sessionId（执行 session、交接）与 forkedFromSessionId（分叉）。"""
        entry = self.entries.get(lid) or {}
        spawned = entry.get('spawnedFrom')
        pairs = (('spawnedFrom.sessionId', spawned.get('sessionId') if isinstance(spawned, dict) else None),
                 ('forkedFromSessionId', entry.get('forkedFromSessionId')))
        return [(relation, value) for relation, value in pairs
                if isinstance(value, str) and LOCAL_ID.fullmatch(value) and value != lid]

    def ancestors(self, lid):
        seen, out, todo = {lid}, [], [lid]
        while todo:
            for _, parent in self.parents(todo.pop()):
                if parent not in seen:
                    seen.add(parent)
                    out.append(parent)
                    todo.append(parent)
        return out

    def members(self, roots):
        """[(编号, 依据, 上级)]：roots 是 [(编号, 'bound_root' 或 'caller_root')]，后代沿登记关系逐层找。"""
        found, queue = {}, []
        for lid, relation in roots:
            if lid not in found:
                found[lid] = (relation, None)
                queue.append(lid)
        while queue:
            parent = queue.pop(0)
            for child, relation in sorted(self.children.get(parent, ())):
                if child not in found:
                    found[child] = (relation, parent)
                    queue.append(child)
        return [(lid, relation, parent) for lid, (relation, parent) in found.items()]

    def path_of(self, lid, key):
        return normal((self.entries.get(lid) or {}).get(key))

    def at_workspace(self, lid):
        """登记里的 cwd 就是绑定的工作区本身（做根的前提）。"""
        return self.workspace is not None and self.path_of(lid, 'cwd') == self.workspace

    def inside(self, lid):
        """cwd 或 originCwd 是工作区本身或在它里面（含 .claude/worktrees/ 下的 worktree）。"""
        for key in ('cwd', 'originCwd'):
            path = self.path_of(lid, key)
            if path is not None and self.workspace is not None and (path == self.workspace or path.is_relative_to(self.workspace)):
                return True
        return False

    def caller(self):
        """调用 publish 的桌面版 session；不是桌面版 session 时为 None。"""
        if self.caller_host:
            return self.caller_host
        if self.caller_cli:
            for lid, entry in self.entries.items():
                prior = entry.get('priorCliSessionIds')
                if entry.get('cliSessionId') == self.caller_cli or isinstance(prior, list) and self.caller_cli in prior:
                    return lid
        return None

    def classify(self, roots):
        """(身份, 编号)，依次判断（约定 4.1“不是已有家族的后代”）：
        root 已知的根；member 上级链里有已知的根（执行、分叉、交接，不能发汇报）；
        new_root 否则 cwd 就是工作区本身，成为新的根——上级在工作区外、或在工作区里但还没有根都算；
        member 否则上级链里有 cwd 在工作区（或它的 worktree）里的 session，例如还没有根时 worktree 里的执行 session；
        outside 与本工作区无关（例如在别处操作的操作者）；None 不是桌面版 session。"""
        lid = self.caller()
        if lid is None:
            return None, None
        if lid in roots:
            return 'root', lid
        up = self.ancestors(lid)
        if any(a in roots for a in up):
            return 'member', lid
        if self.at_workspace(lid):
            return 'new_root', lid
        if any(self.inside(a) for a in up):
            return 'member', lid
        return 'outside', lid

    def hosts(self, roots):
        """可以绑成根的 session，和 classify 的 new_root 同一条规则：cwd 就是工作区本身，上级链里没有已知的根。
        先列上级少的，这样分叉出来的主持排在原来的主持之后、作为它家族里的成员出现。"""
        found = [lid for lid in self.entries if lid not in roots and self.at_workspace(lid)
                 and not any(a in roots for a in self.ancestors(lid))]
        return sorted(found, key=lambda lid: (len(self.ancestors(lid)), -self.activity(lid)))

    def activity(self, lid):
        value = (self.entries.get(lid) or {}).get('lastActivityAt')
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    def project_dirs(self):
        if self._dirs is None:
            try:
                self._dirs = sorted(d for d in self.projects_dir.iterdir() if d.is_dir() and within(d, self.projects_root))
            except OSError:
                self._dirs = []
        return self._dirs

    def logs(self, lid, extra=()):
        """[(种类, 日志编号, 路径)]：cliSessionId 与 priorCliSessionIds 的日志，以及它们的 subagents/ 文件。"""
        entry = self.entries.get(lid) or {}
        ids = []

        def add(kind, value):
            if isinstance(value, str) and PLAIN_ID.fullmatch(value) and value not in [i for _, i in ids]:
                ids.append((kind, value))
        add('cliSessionId', entry.get('cliSessionId'))
        prior = entry.get('priorCliSessionIds')
        for value in prior if isinstance(prior, list) else []:
            add('priorCliSessionIds', value)
        for value in extra:
            add('cliSessionId', value)
        preferred = {encode_cwd(entry[k]).lower() for k in ('cwd', 'worktreePath', 'originCwd') if isinstance(entry.get(k), str)}
        dirs = self.project_dirs()
        first = [d for d in dirs if d.name.lower() in preferred]
        out = []
        for kind, cid in ids:
            # 先找按 cwd 编码的目录；路径太长被截断、或中途换过目录时再逐个目录找这个编号。
            out += self.find(first, kind, cid) or self.find([d for d in dirs if d not in first], kind, cid)
        return out

    def find(self, dirs, kind, cid):
        out = []
        for folder in dirs:
            main = folder / (cid + '.jsonl')
            if main.is_file() and within(main, self.projects_root):
                out.append((kind, cid, main))
            sub = folder / cid / 'subagents'
            if sub.is_dir() and within(sub, self.projects_root):
                out += [('subagents', cid, f) for f in sorted(sub.glob('agent-*.jsonl'))
                        if f.is_file() and within(f, self.projects_root)]
        return out

    def notes_folder(self, lid):
        return self.notes_dir / lid
