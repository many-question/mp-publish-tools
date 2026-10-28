"""结构投影 skeleton-4：保留列明的元数据、用户原文和 assistant 可见正文。

工具只留有界的调用与结果头；Codex 桌面版的线程投递是消息，按 10.3 保留全文、命中秘密时就地遮盖。
隐藏推理及未列明字段的值不导出。Desktop / CodexDesktop 只读各自格式的来源证据，发现研究 agent 家族。
Claude 的正文、会话登记和共享笔记规则沿用 skeleton-3。
"""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

VERSION = 'skeleton-4'
CODEX_SCALARS = {'namespace', 'author', 'recipient', 'forked_from_ordinal_exclusive', 'history_base'}
SCALARS = set('''type subtype role id uuid parentUuid sessionId session_id parent_session_id
thread_id parent_thread_id turn_id parent_turn_id root_turn_id call_id parent_call_id
tool_use_id parent_tool_use_id message_id reply_to timestamp time created_at started_at
namespace author recipient forked_from_ordinal_exclusive history_base
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
    codex = isinstance(record, dict) and record.get('type') in {
        'session_meta', 'response_item', 'event_msg', 'turn_context', 'message',
        'function_call', 'function_call_output', 'custom_tool_call', 'custom_tool_call_output', 'agent_message'}

    def guard(text, where):
        # skeleton-3: same test as check_text, so a masked record is no longer blocked as a whole.
        if hits(text, patterns):
            redaction.append({'path': where, 'reason': 'secret_check'})
            return {'masked': True, 'reason': 'secret_check',
                    'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
        return text

    def passthrough(value):
        # Codex 的消息类别与来源只保留约定中的标量，不带内部上下文。
        out = {}
        if isinstance(value.get('content_item_kinds'), list):
            out['content_item_kinds'] = [v for v in value['content_item_kinds'] if isinstance(v, str)]
        if scalar(value.get('turn_id')) and 'turn_id' in value:
            out['turn_id'] = value['turn_id']
        if isinstance(value.get('content_item_metadata'), list):
            out['content_item_metadata'] = []
            for item in value['content_item_metadata']:
                prov = item.get('provenance') if isinstance(item, dict) else None
                out['content_item_metadata'].append({'provenance': {k: v for k, v in prov.items()
                    if k in ('type', 'namespace', 'hook') and scalar(v)}} if isinstance(prov, dict) else {})
        return out

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
                if codex and kind == 'custom_tool_call' and value.get('name') == 'exec' and label == 'input' and isinstance(raw, str):
                    names = codex_call_names(raw)
                    if names:
                        out['codex_app_calls'] = names
                if codex and kind == 'custom_tool_call_output' and label == 'output':
                    results = codex_result_ids(raw)
                    if results:
                        out['codex_app_results'] = [{'threadId': tid} for tid in results]
                if codex and kind == 'function_call_output' and value.get('namespace') == 'codex_app' and value.get('name') in (
                        'create_thread', 'send_message_to_thread', 'handoff_thread', 'fork_thread') and key == 'output':
                    out['output'] = guard(raw, path + '/output') if isinstance(raw, str) else mask(raw, patterns, path + '/output', redaction)
                    consumed.add(key)
                    break
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
                if key not in out:
                    omitted.append(where)
            elif codex and key == 'history_base' and isinstance(item, dict):
                out[key] = {k: v for k, v in item.items() if k in ('thread_id', 'end_ordinal_exclusive', 'end_byte_offset') and scalar(v)}
            elif codex and key == 'internal_chat_message_metadata_passthrough' and isinstance(item, dict):
                out[key] = passthrough(item)
            elif key in SCALARS and (codex or key not in CODEX_SCALARS) and not isinstance(item, (dict, list)):
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


def project_registry(entry, codex=False):
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
    if codex and isinstance(entry.get('codex'), dict):
        registry['codex'] = {k: v for k, v in entry['codex'].items()
                             if k in ('thread_source', 'evidence', 'outside_workspace', 'continuation_pending') and plain(v)}
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


# ---------------------------------------------------------------- Codex 桌面版（10.2、10.3）

CODEX_ENV = 'CODEX_THREAD_ID'
CODEX_DELIVERIES = ('create_thread', 'send_message_to_thread', 'fork_thread', 'handoff_thread')
CODEX_LINE_BYTES = 64 * 1024 * 1024
CODEX_START_ROWS = 64
CODEX_START_BYTES = 4 * 1024 * 1024


def default_codex_dir(env=None):
    env = os.environ if env is None else env
    return Path(env.get('CODEX_HOME') or Path.home() / '.codex')


def codex_header(path):
    """只读第一行认线程；目录扫描不会把会话正文整份载入内存。"""
    try:
        with Path(path).open('rb') as fh:
            raw = fh.readline(CODEX_LINE_BYTES + 1)
        if len(raw) > CODEX_LINE_BYTES:
            return None
        row = json.loads(raw)
        if not isinstance(row, dict):
            return None
        value = row.get('payload') if row.get('type') == 'session_meta' else None
        return value if isinstance(value, dict) and isinstance(value.get('id'), str) and PLAIN_ID.fullmatch(value['id']) else None
    except (OSError, ValueError, RecursionError):
        return None


def codex_records(path):
    """逐行读取需要核对的记录，不保留整份历史。"""
    with Path(path).open('rb') as fh:
        while raw := fh.readline(CODEX_LINE_BYTES + 1):
            if len(raw) > CODEX_LINE_BYTES:
                raise ValueError('Codex record exceeds 64 MiB')
            if not raw.endswith(b'\n'):
                break
            try:
                row = json.loads(raw)
            except (ValueError, RecursionError):
                continue
            if isinstance(row, dict):
                yield row


def codex_start_records(path):
    """无历史 user 的权限初查只读固定启动前缀，超长行也不能越过字节预算。"""
    remaining = CODEX_START_BYTES
    with Path(path).open('rb') as fh:
        for _ in range(CODEX_START_ROWS):
            raw = fh.readline(remaining)
            remaining -= len(raw)
            if not raw.endswith(b'\n'):
                break
            try:
                row = json.loads(raw)
            except (ValueError, RecursionError):
                row = None
            if isinstance(row, dict):
                yield row
            if not remaining:
                break


def codex_delegation(output):
    """提取来源编号和任务单；不执行投递里的 XML 或正文。"""
    if not isinstance(output, str):
        return None
    match = re.search(r'<codex_delegation>\s*<source_thread_id>([^<]+)</source_thread_id>', output)
    return match[1].strip() if match and PLAIN_ID.fullmatch(match[1].strip()) else None


def codex_result_ids(value, depth=0, known=None, method=None):
    """只认创建结果、MCP 的 text 和代码模式的 JSON 输出；known 另记已知的排队或失败形状。"""
    if depth > 12:
        return []
    if isinstance(value, str):
        try:
            part = json.loads(value)
        except (ValueError, RecursionError):
            # 代码模式会在 JSON 前面加运行状态；正文里的说明文字不能成为创建证据。
            if not value.startswith(('Script completed', 'Script running', 'Wall time:')) or '\nOutput:\n' not in value:
                return []
            body = value.split('\nOutput:\n', 1)[1]
            out, cursor, decoder = [], 0, json.JSONDecoder()
            while cursor < len(body):
                while cursor < len(body) and body[cursor].isspace():
                    cursor += 1
                if cursor == len(body):
                    break
                try:
                    part, end = decoder.raw_decode(body, cursor)
                except (ValueError, RecursionError):
                    end = body.find('\n', cursor)
                    cursor = len(body) if end < 0 else end + 1
                else:
                    out += codex_result_ids(part, depth + 1, known, method)
                    cursor = end
            return out
        return codex_result_ids(part, depth + 1, known, method)
    if isinstance(value, list):
        return [tid for v in value for tid in codex_result_ids(v, depth + 1, known, method)]
    if not isinstance(value, dict):
        return []
    if value.get('isError'):
        if known is not None and value['isError'] is True:
            known.add('tool_error')
        return []
    if value.get('type') in ('input_text', 'text', 'output_text') and isinstance(value.get('text'), str):
        return codex_result_ids(value['text'], depth + 1, known, method)
    own, host = value.get('threadId'), value.get('hostId')
    if (isinstance(own, str) and PLAIN_ID.fullmatch(own) and isinstance(host, str) and host and
            set(value) <= {'threadId', 'hostId', 'projectlessOutputDirectory'} and
            ('projectlessOutputDirectory' not in value or isinstance(value['projectlessOutputDirectory'], str))):
        return [own]
    client = value.get('clientThreadId')
    if isinstance(client, str) and client and not own:
        if known is not None:
            known.add('client_thread_only')
        return []  # 排队中的编号不能替代可用于家族关系的 threadId。
    if (method == 'handoff_thread' and not own and isinstance(value.get('operationId'), str) and value['operationId']
            and isinstance(value.get('status'), str) and value['status'] and isinstance(value.get('steps'), list)):
        if known is not None:
            known.add('handoff_operation')
        return []  # 异步交接操作不提供目的线程编号，不能据此推断家族目标。
    if not (set(value) <= {'content', 'isError', 'structuredContent', '_meta'} and isinstance(value.get('content'), list)):
        return []
    if value.get('isError'):
        return []
    return [tid for block in value['content'] if isinstance(block, dict) and block.get('type') == 'text'
            for tid in codex_result_ids(block.get('text'), depth + 1, known, method)]


def codex_call_names(body):
    """完整代码模式输入中的 Codex 工具名，按调用出现顺序导出。"""
    if not isinstance(body, str):
        return []
    return re.findall(r'tools\.mcp__codex_app__([A-Za-z0-9_]+)\s*\(', body)


def codex_call_title(body):
    """只取可唯一关联到一次创建调用的顶层字符串 title；批量调用不猜对应。"""
    calls = list(re.finditer(r'(?:create_thread|fork_thread|handoff_thread)\s*\(', body))
    if not calls:
        try:
            args = json.loads(body)
        except (ValueError, RecursionError):
            return None
        return args.get('title') if isinstance(args, dict) and isinstance(args.get('title'), str) else None
    if len(calls) != 1:
        return None
    start = calls[0].end()
    while start < len(body) and body[start].isspace():
        start += 1
    if start == len(body) or body[start] != '{':
        return None
    parts, depth, quote, escaped, part = [], 1, None, False, start + 1
    for at in range(start + 1, len(body)):
        char = body[at]
        if quote:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == quote:
                quote = None
        elif char in ('"', "'", '`'):
            quote = char
        elif char in '{[(':
            depth += 1
        elif char in '}])':
            depth -= 1
            if depth == 0:
                parts.append(body[part:at])
                break
        elif char == ',' and depth == 1:
            parts.append(body[part:at])
            part = at + 1
    else:
        return None
    titles = [match[1] for field in parts if (match := re.fullmatch(
        r'''\s*["']?title["']?\s*:\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')\s*''', field))]
    if len(titles) != 1:
        return None
    try:
        value = ast.literal_eval(titles[0])
    except (ValueError, SyntaxError):
        return None
    return value if isinstance(value, str) else None


class CodexDesktop(Desktop):
    """Codex 家族图：只读日志头、来源投递及家族内的派活输出，不依赖状态库。"""

    framework = 'codex-desktop'
    include_outside = True

    def __init__(self, workspace, family=None, env=None, extra_roots=()):
        env = os.environ if env is None else env
        family = family or {}
        self.workspace = normal(str(workspace))
        self.bound = [r for r in family.get('roots') or [] if isinstance(r, str) and PLAIN_ID.fullmatch(r)]
        self.roots = set(self.bound) | set(extra_roots)
        caller = env.get(CODEX_ENV, '')
        self.caller_cli = caller if PLAIN_ID.fullmatch(caller) else None
        self.caller_host = self.caller_cli
        self.codex_dir = Path(family['codex_dir']) if family.get('codex_dir') else default_codex_dir(env)
        self.registry_dir = self.projects_dir = self.codex_dir
        self.projects_root = real(self.codex_dir)
        # Codex 没有 Claude 的共享笔记；collector 不扫描这个位置。
        self.notes_dir = self.codex_dir / 'arp-no-shared-notes'
        self.notes_root = real(self.notes_dir)
        self.registry_ok = self.codex_dir.is_dir()
        self.entries, self._unreadable, self.files, self.headers = {}, [], {}, {}
        self.children, self.edges, self.outputs, self.scanned = {}, {}, {}, set()
        self.delivery_checked = set()
        self.methods = {}  # 创建方法证据与历史来源分开；handoff 投递的 sender 不是续接源。
        self.permission_sources = {}  # 仅供主持权限核对，不作为登记父或家族树的边。
        self._unknown, self._mismatched = set(), set()
        self._known_results = {'client_thread_only': set(), 'tool_error': set(), 'handoff_operation': set()}
        self._family_ids, self._related = set(), set()
        self._inside_cache = {}
        # 工作区里的持久摘要可由 agent 改写，不能据此跳过会影响主持权限或家族关系的证据读取。
        # 旧 arp-codex-discovery.json 原样保留但不读取；scanned 只在本实例内去重。
        self._common = self.git_common(self.workspace)
        for name in ('sessions', 'archived_sessions'):
            folder = self.codex_dir / name
            for base, dirs, names in os.walk(folder):
                dirs[:] = sorted(d for d in dirs if within(Path(base) / d, self.projects_root))
                for name in sorted(names):
                    path = Path(base) / name
                    if path.suffix != '.jsonl' or not within(path, self.projects_root):
                        continue
                    header = codex_header(path)
                    if header is None:
                        self._unreadable.append(str(path))
                        continue
                    source = header.get('source')
                    sub = source.get('subagent') if isinstance(source, dict) else None
                    if header.get('thread_source') == 'guardian_review' or isinstance(sub, dict) and sub.get('other') == 'guardian':
                        self.headers[header['id']] = header  # 只留类别，不能把 guardian 当作缺日志的普通父线程。
                        continue
                    tid = header['id']
                    saved = self.headers.setdefault(tid, dict(header))
                    for key, value in header.items():
                        if saved.get(key) is None:
                            saved[key] = value
                    self.files.setdefault(tid, []).append(path)
                    entry = self.entries.setdefault(tid, {'sessionId': tid, 'cliSessionId': tid})
                    self.update_header(tid, header)
        for tid, header in list(self.headers.items()):
            sub = self.subagent(tid)
            if sub:
                parent = sub.get('parent_thread_id')
                if isinstance(parent, str) and PLAIN_ID.fullmatch(parent):
                    self.edges[tid] = ('thread_spawn.parent_thread_id', parent)
                continue
            if header.get('thread_source') == 'agent_created_thread':
                # 只有这类线程必须先找来源投递；fork 头已足以建待识别图，正文留到实际进入家族时再读。
                self.scan(tid, delivery_only=True)
        self.rebuild()
        # 只对显式根、调用者及其已知家族读派活记录。
        seeds = [(r, 'bound_root') for r in (*self.bound, *extra_roots) if r]
        self._related = {tid for tid, _ in seeds}
        # 已知家族的调用输出也是权限证据，须在调用者可能自动成根之前读取。
        self.members(seeds)
        if self.caller_cli:
            self._related.update([self.caller_cli, *self.permission_ancestors(self.caller_cli)])
            if self.caller_cli in self.entries and not self.is_helper(self.caller_cli):
                # 缺 fork 头的 user 交接只能从自身投递读到 sender；仍只读已关联调用者，不扫无关正文。
                if self.caller_cli not in self.scanned:
                    full = self.at_workspace(self.caller_cli)
                    self.scan(self.caller_cli, delivery_only=not full, startup_only=not full and not self.has_delivery_history(self.caller_cli))
                self.rebuild()
                self.scan_permission_sources(self.caller_cli, {tid for tid, _ in seeds})
                self._related.update(self.permission_ancestors(self.caller_cli))
        role, _ = self.classify({tid for tid, _ in seeds})
        if self.caller_cli and role in ('root', 'new_root'):
            self.roots.add(self.caller_cli)
            self.refresh_relation(self.caller_cli)  # 本次自动成根即解除 pending，不等下一次 publish 才改登记。
            seeds.append((self.caller_cli, 'caller_root'))
        self.members(seeds)

    @property
    def unknown(self):
        return {key for key in self._unknown if key.split(':', 1)[0] in self._family_ids}

    @property
    def known_results(self):
        counts = {kind: sum(key.split(':', 1)[0] in self._family_ids for key in keys)
                  for kind, keys in self._known_results.items()}
        counts['continuation_pending'] = len(self.pending_continuations)
        return counts

    @property
    def pending_continuations(self):
        """当前待识别逻辑线程集合；诊断按集合摘要比较，成员互换也能观察到。"""
        pending = self._family_ids | {tid for controller, outputs in self.outputs.items() if controller in self._family_ids
                                     for tid, info in outputs.items() if info['method'] == 'handoff_thread' and not self.history_parent(tid)}
        if not self.is_helper(self.caller_cli) and self.related_to_roots(self.caller_cli, self.roots):
            pending.add(self.caller_cli)  # 权限已关联的调用者也诊断 pending，不把候选关系变成登记或采集边。
        return {tid for tid in pending if tid not in self.roots and bool(
            (self.entries.get(tid, {}).get('codex') or {}).get('continuation_pending')
            or (self.methods.get(tid) or {}).get('method') == 'handoff_thread' and not self.history_parent(tid))}

    @property
    def mismatched(self):
        return {tid for tid in self._mismatched if tid in self._family_ids or
                any(tid in outputs for parent, outputs in self.outputs.items() if parent in self._family_ids)}

    @property
    def unreadable(self):
        paths = {str(path) for tid in self._family_ids for path in self.files.get(tid, ())}
        return sorted({path for path in self._unreadable if path in paths})

    def apply_outputs(self, tid, outputs):
        for child, info in outputs.items():
            if child == tid or not isinstance(child, str) or not PLAIN_ID.fullmatch(child) or not isinstance(info, dict):
                continue
            if info.get('method') not in ('create_thread', 'fork_thread', 'handoff_thread'):
                continue
            self.outputs.setdefault(tid, {})[child] = info
            self.add_permission_sources(child, tid)
            edge = self.edges.get(child)
            raw_parent = (self.methods.get(child) or {}).get('sender') or self.history_parent(child)
            if info['method'] == 'create_thread' and edge and (raw_parent != tid if raw_parent else edge[1] != self.owner(tid)):
                self._mismatched.add(child)
                continue
            confirmed = self.methods.get(child)
            if not confirmed or not confirmed.get('delivery'):
                self.methods[child] = dict(method=info['method'], sender=tid, delivery=False)
            if child in self.entries:
                if isinstance(info.get('title'), str):
                    self.entries[child]['title'] = info['title']
            self.refresh_relation(child)

    def add_permission_sources(self, tid, *sources):
        for source in sources:
            if isinstance(source, str) and PLAIN_ID.fullmatch(source) and source != tid:
                self.permission_sources.setdefault(tid, set()).add(source)

    def permission_parents(self, tid):
        """保留原始候选链用于拒绝发布，和可登记的 owner 关系分开。"""
        return self.permission_sources.get(tid, set()) | {parent for _, parent in self.parents(tid)}

    def permission_ancestors(self, tid):
        todo, seen, out = [tid], {tid}, []
        while todo:
            for parent in sorted(self.permission_parents(todo.pop())):
                if parent not in seen:
                    seen.add(parent)
                    out.append(parent)
                    todo.append(parent)
        return out

    def related_to_roots(self, tid, roots):
        """候选来源可递归连到已知根即拒绝成员发汇报；不从候选中挑一个猜登记父。"""
        return any(parent in roots for parent in self.permission_ancestors(tid))

    def scan_permission_sources(self, tid, roots):
        """只对调用者可达的候选链补读首投递；已知关联足够拒绝时不扩读正文。"""
        if not roots or tid in roots:
            return
        queue, seen = [tid], set()
        while queue and not self.related_to_roots(tid, roots):
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            if current in self.entries and current not in roots and not self.is_helper(current):
                self._related.add(current)
                if current not in self.delivery_checked and not (self.methods.get(current) or {}).get('delivery'):
                    self.scan(current, delivery_only=True, startup_only=not self.has_delivery_history(current))
                    self.rebuild()
            queue += sorted(self.permission_parents(current))

    def has_delivery_history(self, tid):
        header = self.headers.get(tid) or {}
        base = header.get('history_base')
        return (header.get('forked_from_id') is not None or isinstance(base, dict) and base.get('thread_id') is not None
                or header.get('thread_source') in ('agent_created_thread', 'agent_forked_thread'))

    def history_parent(self, tid):
        """历史复制来源只取有效的头部字段，不能拿投递 sender 代替。"""
        header = self.headers.get(tid) or {}
        parent = header.get('forked_from_id')
        base = header.get('history_base')
        inherited = base.get('thread_id') if isinstance(base, dict) else None
        if isinstance(inherited, str) and PLAIN_ID.fullmatch(inherited) and inherited != parent:
            return None  # 两份可靠头部来源相互矛盾时不猜其中一个。
        return (parent if 'subagent_history_start_ordinal' not in header and isinstance(parent, str)
                and PLAIN_ID.fullmatch(parent) and parent != tid else None)

    def refresh_relation(self, tid):
        """从已有证据重新计算登记，迟到的方法确证不会留下互相冲突的旧字段。"""
        entry = self.entries.get(tid)
        if self.subagent(tid):
            return
        method = self.methods.get(tid) or {}
        kind, sender = method.get('method'), method.get('sender')
        history = self.history_parent(tid)
        header = self.headers.get(tid) or {}
        source = header.get('thread_source')
        relation, parent, evidence, pending = None, None, [], False
        if kind == 'handoff_thread':
            parent, relation = history, 'spawnedFrom.sessionId'
            evidence = ['session_meta.forked_from_id', 'codex_app.handoff_thread'] if history else ['codex_app.handoff_thread']
            pending = history is None
        elif kind == 'fork_thread' or source == 'agent_forked_thread':
            parent, relation = history or sender, 'forkedFromSessionId'
            evidence = ['forkedFromSessionId', 'codex_app.fork_thread' if kind == 'fork_thread' else 'thread_source.agent_forked_thread']
        elif kind == 'create_thread':
            parent = sender
            relation = 'codex_delegation.source_thread_id' if method.get('delivery') else 'create_thread.output'
            evidence = [relation]
        elif history:
            parent, relation, evidence = history, 'forkedFromSessionId', ['forkedFromSessionId']
            pending = source in (None, 'user')
        elif (header.get('forked_from_id') is not None or isinstance(header.get('history_base'), dict)
              and header['history_base'].get('thread_id') is not None):
            pending = source in (None, 'user')
        raw_parent = parent
        if raw_parent and self.is_helper(raw_parent):
            self.add_permission_sources(tid, raw_parent)
            parent = self.owner(raw_parent)
            evidence.append('subagent_source_thread_id:' + raw_parent)
            if parent is None and kind == 'handoff_thread':
                pending = True
        if parent == tid:
            parent = None
        self.edges.pop(tid, None)
        if parent and parent != tid:
            self.edges[tid] = (relation, parent)
            if kind == 'create_thread' and relation != 'create_thread.output' and tid in self.outputs.get(raw_parent, {}):
                evidence.append('create_thread.output')
        if entry is None:
            return
        entry.pop('spawnedFrom', None)
        entry.pop('forkedFromSessionId', None)
        if parent:
            if relation == 'forkedFromSessionId':
                entry['forkedFromSessionId'] = parent
            else:
                entry['spawnedFrom'] = dict(sessionId=parent)
                if kind == 'handoff_thread':
                    entry['spawnedFrom']['handOff'] = True
                if isinstance(entry.get('title'), str):
                    entry['spawnedFrom']['title'] = entry['title']
        entry['codex']['evidence'] = evidence
        entry['codex'].pop('continuation_pending', None)
        if pending and tid not in self.roots:
            entry['codex']['continuation_pending'] = True

    @staticmethod
    def git_common(path):
        """只解析 .git 与 commondir；不调用 git、不修改仓库。"""
        if path is None:
            return None
        git = Path(path) / '.git'
        try:
            if git.is_file():
                text = git.read_text(encoding='utf-8').strip()
                if not text.startswith('gitdir:'):
                    return None
                git = (Path(path) / text[7:].strip()).resolve()
            if not git.is_dir():
                return None
            common = git / 'commondir'
            return real(git / common.read_text(encoding='utf-8').strip()) if common.is_file() else real(git)
        except (OSError, ValueError):
            return None

    def subagent(self, tid):
        source = (self.headers.get(tid) or {}).get('source')
        sub = source.get('subagent') if isinstance(source, dict) else None
        spawn = sub.get('thread_spawn') if isinstance(sub, dict) else None
        return spawn if isinstance(spawn, dict) else None

    def is_helper(self, tid):
        header = self.headers.get(tid) or {}
        return bool(tid and (header.get('thread_source') in ('guardian_review', 'subagent')
                    or isinstance(header.get('source'), dict) and 'subagent' in header['source']))

    def owner(self, tid):
        """子任务代表最近的顶层线程；缺来源、循环或 guardian 均不能猜一个道。"""
        seen = set()
        while self.is_helper(tid):
            header = self.headers.get(tid) or {}
            sub = header.get('source')
            sub = sub.get('subagent') if isinstance(sub, dict) else None
            if tid in seen or header.get('thread_source') == 'guardian_review' or isinstance(sub, dict) and sub.get('other') == 'guardian':
                return None
            seen.add(tid)
            parent = (self.subagent(tid) or {}).get('parent_thread_id')
            if not isinstance(parent, str) or not PLAIN_ID.fullmatch(parent) or parent not in self.headers:
                return None
            tid = parent
        return tid

    def update_header(self, tid, header):
        entry = self.entries[tid]
        base = header.get('history_base')
        self.add_permission_sources(tid, header.get('forked_from_id'), base.get('thread_id') if isinstance(base, dict) else None,
                                    (self.subagent(tid) or {}).get('parent_thread_id'))
        for key in ('cwd', 'model'):
            if isinstance(header.get(key), str):
                entry[key] = header[key]
        stamp = header.get('timestamp')
        if isinstance(stamp, str):
            try:
                from datetime import datetime
                created = int(datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp() * 1000)
                entry['createdAt'] = min(entry.get('createdAt', created), created)
            except ValueError:
                pass
        thread_source = header.get('thread_source') or (entry.get('codex') or {}).get('thread_source')
        if thread_source not in (None, 'user', 'agent_created_thread', 'agent_forked_thread', 'subagent', 'automation', 'guardian_review'):
            self._unknown.add(tid)
        saved = self.headers.setdefault(tid, {})
        for key, value in header.items():
            if saved.get(key) is None:
                saved[key] = value
        if thread_source == 'agent_forked_thread':
            saved['thread_source'] = thread_source
        entry.setdefault('codex', {}).update(thread_source=saved.get('thread_source') or 'user')
        entry['codex'].setdefault('outside_workspace', False)

    def rebuild(self):
        self.children = {}
        for tid in set(self.entries) | set(self.methods):
            self.refresh_relation(tid)
        for tid, (relation, parent) in self.edges.items():
            if tid != parent:
                self.children.setdefault(parent, []).append((tid, relation))

    def scan(self, tid, delivery_only=False, startup_only=False):
        if tid in (self.delivery_checked if delivery_only else self.scanned):
            return
        calls = {}
        header = self.headers.get(tid) or {}
        base = header.get('history_base') or {}
        boundaries = [header.get('subagent_history_start_ordinal'), header.get('forked_from_ordinal_exclusive'),
                      base.get('end_ordinal_exclusive') if isinstance(base, dict) else None]
        boundary = max((v for v in boundaries if isinstance(v, int) and not isinstance(v, bool)), default=0)
        first_delivery = False
        for path in sorted(self.files.get(tid, ())):
            try:
                for row in (codex_start_records(path) if startup_only else codex_records(path)):
                    kind, payload = row.get('type'), row.get('payload')
                    if not isinstance(payload, dict):
                        continue
                    if kind == 'session_meta' and payload.get('id') == tid:
                        self.update_header(tid, payload)
                    ordinal = row.get('ordinal')
                    if kind != 'session_meta' and isinstance(ordinal, int) and ordinal < boundary:
                        continue
                    if kind == 'turn_context' and isinstance(payload.get('model'), str):
                        self.entries[tid]['model'] = payload['model']
                    if kind != 'response_item':
                        continue
                    native = payload.get('type')
                    if not isinstance(native, str):
                        self._unknown.add(tid + ':record-type')
                        continue
                    if native == 'function_call_output' and payload.get('namespace') == 'codex_app' and payload.get('name') in ('create_thread', 'fork_thread', 'handoff_thread') and not first_delivery:
                        first_delivery = True
                        sender = codex_delegation(payload.get('output'))
                        if payload['name'] == 'handoff_thread':
                            self.add_permission_sources(tid, sender)
                        if payload['name'] == 'handoff_thread' or sender and sender != tid:
                            self.methods[tid] = dict(method=payload['name'], sender=sender, delivery=True)
                            self.refresh_relation(tid)
                        elif delivery_only:
                            self._unknown.add(tid)
                        if delivery_only:
                            self.delivery_checked.add(tid)
                            return
                    if delivery_only:
                        continue
                    if native in CALL_KINDS:
                        body = payload.get('input', payload.get('arguments', ''))
                        body = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
                        method = next((name for name in ('create_thread', 'fork_thread', 'handoff_thread')
                                       if name in str(payload.get('name', '')) or re.search(name + r'\s*\(', body)), None)
                        if method:
                            if isinstance(payload.get('call_id'), str) and payload['call_id']:
                                calls[payload['call_id']] = {'method': method, 'title': codex_call_title(body)}
                            else:
                                self._unknown.add(tid + ':call-id')
                    elif native in RESULT_KINDS and isinstance(payload.get('call_id'), str) and payload['call_id'] in calls:
                        info = calls[payload['call_id']]
                        known, key = set(), tid + ':call:' + payload['call_id']
                        created = list(dict.fromkeys(codex_result_ids(payload.get('output', payload.get('content')), known=known, method=info['method'])))
                        for kind in known:
                            self._known_results[kind].add(key)
                        if not created and not known:
                            self._unknown.add(key)
                        if len(created) > 1:
                            info = dict(info, title=None)
                        self.apply_outputs(tid, {child: info for child in created})
            except (OSError, ValueError, RecursionError):
                self._unreadable.append(str(path))
        if delivery_only and not startup_only and not first_delivery and header.get('forked_from_id') is None and header.get('thread_source') != 'agent_forked_thread':
            self._unknown.add(tid)
        if not delivery_only:
            self.scanned.add(tid)
        self.delivery_checked.add(tid)

    def parents(self, lid):
        edge = self.edges.get(lid)
        return [edge] if edge and edge[1] != lid else []

    def members(self, roots):
        found, queue, visited = {}, [(tid, rel) for tid, rel in roots if not self.is_helper(tid)], set()
        while queue:
            tid, relation = queue.pop(0)
            if tid in visited:
                continue
            visited.add(tid)
            helper = self.is_helper(tid)
            owner = self.owner(tid) if helper else tid
            if helper and owner is None:
                continue
            self._family_ids.add(tid)
            if tid in self.entries:
                self.scan(tid)
                self.entries[tid]['codex']['outside_workspace'] = not self.inside(tid)
                self.rebuild()
            edge = self.edges.get(tid, (None, None))
            # 扫描可能把历史 fork 关系确证为 handoff；返回当前关系，不把入队时的临时类型交给 setup/观察。
            if not helper:
                found[tid] = (edge[0] if edge[0] and relation not in ('bound_root', 'caller_root', 'host') else relation, edge[1])
            # 子任务输出归一化后可能新添 owner 的孩子；重访孩子列表，不把 helper 自己返回成一条道。
            children = self.children.get(tid, []) + (self.children.get(owner, []) if helper else [])
            queue += [(child, rel) for child, rel in sorted(children) if child not in visited]
        return [(tid, relation if relation in ('bound_root', 'caller_root', 'host') else self.edges.get(tid, (None, None))[0],
                 self.edges.get(tid, (None, None))[1]) for tid, (relation, _) in found.items()]

    def inside(self, lid):
        path = self.path_of(lid, 'cwd')
        if path is None or self.workspace is None:
            return False
        if path == self.workspace or path.is_relative_to(self.workspace):
            return True
        if lid not in self._family_ids and lid not in self._related:
            return False  # 不读取无关 cwd 的 .git，尤其不能触碰任意网络路径。
        key = str(path)
        if key not in self._inside_cache:
            self._inside_cache[key] = bool(self._common and self.git_common(path) == self._common)
        return self._inside_cache[key]

    def caller(self):
        return self.caller_cli

    def classify(self, roots):
        lid = self.caller()
        if self.is_helper(lid):
            return 'member', lid
        if lid in roots:
            return 'root', lid
        if self.related_to_roots(lid, roots):
            return 'member', lid
        # 没有任何候选关联已知家族时，仍按 4.1 的 cwd 与线程类别判定新根或工作区外调用者。
        role, lid = super().classify(roots)
        if role == 'new_root' and (self.headers.get(lid) or {}).get('thread_source') not in (None, 'user'):
            return ('member' if any(self.inside(a) for a in self.permission_ancestors(lid)) else 'outside'), lid
        if role == 'outside' and any(self.inside(a) for a in self.permission_ancestors(lid)):
            return 'member', lid
        return role, lid

    def hosts(self, roots):
        return [tid for tid in super().hosts(roots) if not self.is_helper(tid) and
                (self.headers.get(tid) or {}).get('thread_source') in (None, 'user') and
                not (self.entries[tid].get('codex') or {}).get('continuation_pending') and
                (self.methods.get(tid) or {}).get('method') not in ('handoff_thread', 'fork_thread')]

    def logs(self, lid, extra=()):
        out = [('thread', lid, p) for p in sorted(self.files.get(lid, ()))]
        queue, seen = [lid], {lid}
        while queue:
            parent = queue.pop(0)
            for child, relation in sorted(self.children.get(parent, ())):
                if relation == 'thread_spawn.parent_thread_id' and child not in seen and self.owner(child) == lid:
                    seen.add(child)
                    self._family_ids.add(child)
                    queue.append(child)
                    out += [('subagent', child, p) for p in sorted(self.files.get(child, ()))]
        return out
