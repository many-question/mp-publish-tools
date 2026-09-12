"""Structural projection v1; does not classify people, deduplicate or sum usage.

Only explicitly listed metadata and user text are exported. Unknown field names
are inventoried; their values stay local and can be collected by a later version.
"""
import json

VERSION = 'skeleton-1'
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
TEXT_TYPES = {'text', 'input_text'}


def project(record):
    omitted = []
    def walk(value, path='', user=False):
        if isinstance(value, list):
            return [walk(v, path + '[]', user) for v in value]
        if not isinstance(value, dict):
            return value if isinstance(value, (int, float, bool)) or value is None else None
        kind = value.get('type')
        # Native roles are retained; this is not a human/automation classification.
        local_user = value.get('role') == 'user' or kind in {'user', 'user_message'}
        text_allowed = local_user or (user and kind in TEXT_TYPES)
        out = {}
        for key, item in value.items():
            where = path + '/' + key
            if key in SCALARS and not isinstance(item, (dict, list)):
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
            elif key in CONTAINERS and isinstance(item, (dict, list)):
                out[key] = walk(item, where, local_user or user)
            elif key in {'content', 'text', 'message'} and text_allowed and isinstance(item, str):
                out[key] = item
            elif key == 'content' and isinstance(item, list):
                # tool_result content is excluded even when wrapped in a user message.
                if kind == 'tool_result':
                    omitted.append(where)
                else:
                    out[key] = [walk(part, where + '[]', local_user) for part in item]
            else:
                omitted.append(where)
        return out
    if not isinstance(record, dict):
        raise ValueError('Expected a native JSON object')
    native = walk(record)
    return {'native': native, 'projection_version': VERSION, 'omitted_fields': omitted,
            'extraction_status': 'projected' if native else 'unrecognized_structure'}


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
