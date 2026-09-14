"""Structural projection v2; does not classify people, deduplicate or sum usage.

Exported: the listed metadata, user text (unchanged), assistant visible text and
bounded tool call/result heads. Hidden reasoning, full tool arguments and full
tool output never leave the environment. Unknown field names are inventoried;
their values stay local and can be collected by a later version.
"""
import hashlib
import json

VERSION = 'skeleton-2'
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
num_turns total_cost_usd cost_usd index sequence sequence_number retry_count'''.split())
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


def project(record, patterns=None):
    """Patterns enable in-place masking of assistant text and tool heads.

    User text keeps the skeleton-1 behaviour: a match blocks the whole record and
    nothing is edited locally. Masked fields are marked, never passed off as verbatim.
    """
    omitted, redaction = [], []

    def guard(text, where):
        if patterns and any(pattern.search(text) for pattern, _ in patterns):
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
