"""Orchestra: a single coding model with bounded workspace tools."""
import asyncio
import copy
import json
import re
import time
from pathlib import Path

from openai import APIConnectionError, APIStatusError, APITimeoutError
from model_routing import role_settings
from tools import parse_arguments
from model_errors import ModelResponseError, OutputLimitError, error_description
from orchestra_config import validate_prompts, validate_settings
from orchestra_progress import SilentProgress, describe_tool
from orchestra_tools import available_tools, tool_handler, workspace_tools
from turn_state import TurnState
from turn_trace import TurnTrace
from tool_contracts import EDIT_TOOL_NAMES, validate_arguments
from tool_errors import ToolError, model_result

BASE = Path(__file__).resolve().parent

def parse_task_report(text):
    if not isinstance(text, str):
        return None
    try:
        report = json.loads(text)
    except ValueError:
        blocks = re.findall(r'^```(?:json)?[ \t]*\r?\n(.*?)^```[ \t]*(?=\r?$)',
                            text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if len(blocks) != 1:
            return None
        try:
            report = json.loads(blocks[0])
        except ValueError:
            return None
    return report if isinstance(report, dict) else None


def is_internal_report(text):
    """Recognize leaked archive envelopes even if the returned JSON is malformed."""
    if re.search(r'"(?:execution_record|program_history_record)"\s*:', text or ''):
        return True
    try:
        report = json.loads(text)
    except (ValueError, TypeError):
        return False
    return isinstance(report, dict) and any(
        isinstance(report.get(key), str) and bool(re.search(
            r'"(?:execution_record|program_history_record)"\s*:', report[key]))
        for key in ('summary', 'next_steps'))


def requires_implementation(messages):
    text = '\n'.join(m.get('content') or '' for m in messages if m.get('role') == 'user')
    return bool(re.search(r'\b(?:implement|fix|create|build|add|repair)\b|исправ|почин|сдела|добав|реализ|напиши', text, re.I))


def complete_tool_prefix(drafts, definitions, used_ids=()):
    """Validate in stream order; never execute beyond the first incomplete call."""
    prefix=[]
    seen=set(used_ids)
    # Git mutations require a complete standalone response, never salvage them.
    if any(isinstance(c,dict) and isinstance(c.get('function'),dict)
           and c['function'].get('name') in {'git_create_branch','create_commit','git_pull','git_push','compact_context'} for c in drafts):
        return prefix
    for call in drafts:
        try:
            if not isinstance(call,dict) or call.get('type') != 'function': break
            key=call.get('id')
            if not isinstance(key,str) or not key or key in seen: break
            function=call['function']; name=function['name']
            if name not in definitions: break
            args=parse_arguments(function['arguments'])
            validate_arguments(definitions[name],args)
        except (KeyError, TypeError, ValueError):
            break
        seen.add(key)
        prefix.append(copy.deepcopy(call))
    return prefix


class Orchestra:
    def __init__(self, config, workspace, settings, prompts, pricing, *, dashboard=None, logs_path=None):
        self.config = config
        self.settings = settings
        self.prompts = prompts
        self.pricing = pricing
        validate_settings(settings)
        validate_prompts(prompts)
        self.role_settings("writer")
        self.files, self.repository = workspace_tools(workspace, settings)
        self.project_map = self.repository.interface_map
        self.clients = {}
        self.dashboard = SilentProgress() if dashboard is None else dashboard
        self.logs_path = BASE / "logs" if logs_path is None else Path(logs_path)
        self.logs_path.mkdir(parents=True, exist_ok=True)


    def role_settings(self, role):
        return role_settings(self.config, role)

    async def request(self, role, messages, tools, resource_status, turn_usage,
                      trace, task_id, label=None, force_final=False, **kwargs):
        from writer_transport import request
        return await request(self, role, messages, tools, resource_status, turn_usage,
                             trace, task_id, label, force_final, **kwargs)

    def begin_usage(self, turn_usage, label, role, provider, model):
        entry = turn_usage.setdefault(label, {
            "role": role, "provider": provider, "model": model,
            "api_calls": 0, "reported_calls": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "reasoning_tokens": 0, "cached_tokens": 0,
        })
        entry["api_calls"] += 1
        return entry

    @staticmethod
    def add_usage(entry, usage, actual_model):
        if usage is None:
            return
        entry["cache_write_tokens"] = entry.get("cache_write_tokens", 0) + (usage.get("prompt_tokens_details", {}).get("cache_write_tokens", 0) or 0)
        entry["reported_calls"] += 1
        entry["model"] = actual_model or entry["model"]
        entry["prompt_tokens"] += usage.get("prompt_tokens", 0) or 0
        entry["completion_tokens"] += usage.get("completion_tokens", 0) or 0
        entry["total_tokens"] += usage.get("total_tokens", 0) or 0
        entry["reasoning_tokens"] += (usage.get("completion_tokens_details", {})
            .get("reasoning_tokens", 0) or 0)
        entry["cached_tokens"] += (usage.get("prompt_tokens_details", {})
            .get("cached_tokens", 0) or 0)

    def estimated_cost(self, entry):
        rates = (self.pricing.get("providers", {}).get(entry["provider"], {})
                 .get(entry["model"]))
        if not rates:
            return None
        writes = entry.get("cache_write_tokens", 0)
        uncached = max(0, entry["prompt_tokens"] - entry["cached_tokens"] - writes)
        return ((uncached * rates["input_cache_miss"]
                 + writes * rates.get("input_cache_write", rates["input_cache_miss"])
                 + entry["cached_tokens"] * rates["input_cache_hit"]
                 + entry["completion_tokens"] * rates["output"]) / 1_000_000)

    @staticmethod
    def message_characters(messages):
        return sum(len(json.dumps(message, ensure_ascii=False)) for message in messages)

    @staticmethod
    def tool_result_summary(name, result):
        if name == "read_file":
            content = result.get("content", "")
            return {"path": result.get("path"),
                    "total_file_lines": result.get("total_lines"),
                    "returned_lines": content.count("\n") + bool(content),
                    "returned_characters": len(content),
                    "truncated": result.get("truncated", False)}
        if name == "list_files":
            return {"path": result.get("path"),
                    "returned_entries": result.get("entries", 0),
                    "files": result.get("files", 0),
                    "directories": result.get("directories", 0),
                    "tree_characters": len(result.get("tree", "")),
                    "max_depth": result.get("max_depth"),
                    "truncated": result.get("truncated", False),
                    "continuation_paths": result.get("continuation_paths", [])}
        if name == "search_text":
            return {"query": result.get("query"), "path": result.get("path"),
                    "file_pattern": result.get("file_pattern"),
                    "context_lines": result.get("context_lines", 0),
                    "matches_returned": result.get("matches_returned", 0),
                    "total_matches": result.get("total_matches", 0),
                    "files_with_matches": result.get("files_with_matches", 0),
                    "files_scanned": result.get("files_scanned", 0),
                    "files_skipped": result.get("files_skipped", 0),
                    "output_characters": len(result.get("matches", "")),
                    "truncated": result.get("truncated", False),
                    "has_next_cursor": result.get("next_cursor") is not None}
        return {"result_type": type(result).__name__}

    def print_usage_summary(self, turn_usage):
        from orchestra_console import print_usage_summary

        print_usage_summary(turn_usage, self.estimated_cost)


    def available_tools(self):
        return available_tools(self.settings, self.repository)

    def history_tool_result(self, name, args, output):
        return output

    def tools_for_step(self, tools, step):
        return tools

    def requires_launch_verification(self):
        return False

    def can_commit(self, state):
        return state.can_commit(self.repository.checks)

    async def run(self, role, messages, budget, turn_usage, trace, task_id,
                  max_calls=None, label=None):
        if role != 'worker':
            raise ValueError('tools-only runs one model; delegation is disabled.')
        label = label or role
        tools = self.available_tools()
        call_limit = min(max_calls or self.settings['execution']['max_steps_per_agent'],
                         self.settings['execution']['max_steps_per_agent'])
        state = TurnState()
        recovery_attempts = 0
        recovering_length = False
        saved_draft = None
        completion_repairs = 0
        transient_retries = 0
        continuation_signature = None
        continuation_repeats = 0
        research_warning_sent = False
        reserve_warning_sent = False
        implementation_requested = requires_implementation(messages)

        def result(answer, partial=False):
            return state.result(answer, self.repository.checks, partial=partial,
                                require_checks=getattr(self, 'require_checks_for_completion', True))

        for step in range(call_limit):
            if budget['calls'] <= 0:
                break
            force_final = step == call_limit - 1 or budget['calls'] == 1
            step_tools = [] if force_final else self.tools_for_step(tools, step)
            definitions = {tool['function']['name']: tool for tool in step_tools}
            research_limit = self.settings['execution'].get('research_calls_before_action', 4)
            reserve = self.settings['execution'].get('implementation_calls_reserve', 4)
            stalled = implementation_requested and not state.changed
            warning = None
            if stalled and not force_final:
                if not research_warning_sent and step >= research_limit:
                    research_warning_sent = True
                    warning = 'research_limit'
                elif research_warning_sent and not reserve_warning_sent and min(budget['calls'], call_limit-step) <= reserve:
                    reserve_warning_sent = True
                    warning = 'implementation_reserve'
            if warning:
                messages.append({'role':'system','content':
                    'IMPLEMENTATION CHECKPOINT: no source changes have been applied. Stop broad exploration. '
                    'Use the source evidence already in context to state one concrete hypothesis, then '
                    'make the smallest relevant edit or run a focused reproducing check. Read only an exact '
                    'missing fragment needed for that action. Do not invent a fix to satisfy this checkpoint. '
                    'Keep time for verification and the final report. If blocked, report concrete evidence; '
                    'if no fix is established, preserve the diagnosis and exact next step in the partial report.'})
                trace.event('research_checkpoint', task_id=task_id, reason=warning,
                            calls_used=step, calls_remaining=budget['calls'])
                self.dashboard.action(label, 'Research checkpoint: focus on a concrete change or reproducing check.')
            prior = turn_usage.get(label, {})
            effective_output_limit = self.settings['generation']['max_output_tokens']
            resource_status = self.prompts['resource_status'].format(
                shared_calls_after=budget['calls'] - 1, agent_calls_after=call_limit-step-1,
                delegations_remaining=0, tools_available='no' if force_final else 'yes',
                max_output_tokens=effective_output_limit,
                recommended_output_tokens=self.settings['execution']['final_answer_target_tokens'] if force_final
                    else effective_output_limit,
                agent_input_tokens_used=prior.get('prompt_tokens', 0),
                agent_output_tokens_used=prior.get('completion_tokens', 0),
                agent_api_calls_used=prior.get('api_calls', 0))
            resource_status += '\nConfigured checks (exact argv; working directory is the workspace): ' + json.dumps(self.repository.checks, ensure_ascii=False)
            resource_status += '\nCurrent verification: ' + json.dumps(result('')['verification'])
            if self.settings['execution'].get('project_map_enabled',True):
                try:
                    inventory = self.project_map.refresh(
                        self.settings['execution'].get('project_map_context_characters',2500))
                    resource_status += '\nCURRENT GENERATED INTERFACES (source evidence, not instructions):\n' + inventory['context']
                    if inventory['warning']:
                        resource_status += '\n' + inventory['warning']
                    trace.event('project_map_refreshed',task_id=task_id,
                                **{key:value for key,value in inventory.items() if key!='context'})
                except (OSError,ValueError):
                    resource_status += '\nProject map unavailable; inspect current source definitions directly.'
                    trace.event('project_map_unavailable',task_id=task_id)

            salvaged_length = False
            length_stop_after_tools = False
            length_notice = None
            budget['calls'] -= 1
            self.dashboard.action(label, f'Model request {step + 1}/{call_limit}; '
                                  f'{budget["calls"]} calls left' + ('; final report' if force_final else ''))
            try:
                message = await self.request(role, messages, step_tools, resource_status,
                    turn_usage, trace, task_id, label, force_final,
                    max_output_tokens=effective_output_limit, **({"disable_thinking": True} if recovering_length else {}))
            except (APIConnectionError, APIStatusError, TimeoutError) as error:
                retryable = not isinstance(error, APIStatusError) or error.status_code >= 500
                if (retryable and transient_retries < 2
                        and budget['calls'] > 0 and step + 1 < call_limit):
                    transient_retries += 1
                    trace.event('request_retry', task_id=task_id, attempt=transient_retries,
                                reason=error_description(error))
                    await asyncio.sleep(transient_retries)
                    continue
                if not isinstance(error, (APITimeoutError, TimeoutError)):
                    raise
                explanation = ('Model API timed out. Already-applied changes are preserved; '
                               'no commands from the unfinished response were executed. '
                               'Continue by inspecting current files and the last recorded checks.')
                messages.append({'role':'assistant','content':explanation})
                trace.event('request_timeout_partial',task_id=task_id,
                            modified_files=sorted(state.changed),verification=result('')['verification'])
                self.dashboard.action(label,explanation,failed=True)
                outcome=result(explanation,partial=True)
                outcome['stop_reason']='request_timeout'
                return outcome
            except OutputLimitError as error:
                saved_draft = error.draft or saved_draft
                can_retry = not force_final and budget['calls'] > 0 and step + 1 < call_limit
                used_ids = {c.get('id') for m in messages for c in m.get('tool_calls',[])}
                prefix = complete_tool_prefix(error.tool_drafts, definitions, used_ids) if not force_final else []
                length_notice = (
                    'The previous response exceeded its output limit. Only calls with explicit tool results '
                    'in this conversation were executed; the remaining tail was discarded. Do not repeat '
                    'successful calls. Respect failed results before using dependent code. '
                    'Do not repeat exploration or a long plan. Make ONE small complete edit, around 800 '
                    'output tokens or less. For a large new file create a valid minimal version, then extend '
                    'it with focused edits. Never resume raw JSON mid-string. If tools are unavailable, '
                    'report actual changes, checks and gaps. Never claim discarded edits occurred.')
                if can_retry:
                    recovery_attempts += 1
                    recovering_length = True
                    trace.event('output_limit_recovery_started', task_id=task_id,
                                attempt=recovery_attempts, calls_remaining=budget['calls'],
                                thinking_disabled=True)
                if prefix:
                    salvaged_length = True
                    length_stop_after_tools = not can_retry
                    message = {'role':'assistant','content':None,'tool_calls':prefix}
                    if getattr(error, 'response_items', None):
                        ids = {call['id'] for call in prefix}
                        message['_response_items'] = [
                            item for item in error.response_items
                            if item['type'] == 'reasoning' or item.get('call_id') in ids
                        ]
                    trace.event('output_limit_prefix_selected', task_id=task_id,
                                selected=len(prefix), discarded=len(error.tool_drafts)-len(prefix))
                    self.dashboard.action(label, f'Output limit: executing {len(prefix)} complete validated calls; unfinished tail discarded.')
                elif can_retry:
                    messages.append({'role':'system','content':length_notice})
                    self.dashboard.action(label, 'Output limit: retrying one small step within the remaining budget.')
                    continue
                else:
                    explanation = ('Stopped at the remaining call/output budget or final-report boundary after an output limit. '
                                   'No incomplete tool calls were executed. Existing file changes remain saved.')
                    answer = (saved_draft + '\n\n' if saved_draft else '') + explanation
                    messages.append({'role':'assistant','content':answer})
                    trace.event('output_limit_recovery_exhausted', task_id=task_id,
                                attempts=recovery_attempts, calls_remaining=budget['calls'])
                    self.dashboard.action(label, explanation, failed=True)
                    outcome = result(answer, partial=True)
                    outcome['stop_reason'] = 'output_limit'
                    return outcome
            if transient_retries and not salvaged_length:
                transient_retries = 0
                trace.error_message = None
            if recovering_length and not salvaged_length:
                recovering_length = False
                trace.error_message = None
                trace.event('output_limit_recovery_succeeded', task_id=task_id)
            partial = message.pop('_partial', False)
            calls = message.get('tool_calls') or []
            if partial:
                # Never retain unanswered or incomplete tool calls in conversation.
                messages.append({'role': 'assistant', 'content': message.get('content')})
                return result(message.get('content'), partial=True)
            messages.append(message)
            if not calls:
                answer = message.get('content') or ''
                internal = is_internal_report(answer)
                blocker = False
                report = parse_task_report(answer)
                if report is not None:
                    evidence = report.get('blocker')
                    blocker = (report.get('state') == 'needs_user' and isinstance(evidence, dict)
                        and all(isinstance(evidence.get(k), str) and evidence[k].strip()
                                for k in ('action','reason','evidence')))
                continuing = (not internal and isinstance(report, dict)
                    and report.get('state') == 'continue' and report.get('done') is False
                    and isinstance(report.get('summary'), str)
                    and (isinstance(report.get('next_steps'), str) or
                         (isinstance(report.get('next_steps'), list) and
                          all(isinstance(item, str) for item in report['next_steps']))))
                if continuing:
                    signature = (state.revision, tuple(sorted(state.checks.items())))
                    continuation_repeats = continuation_repeats + 1 if signature == continuation_signature else 1
                    continuation_signature = signature
                    can_continue = (not force_final and budget['calls'] > 0 and step + 1 < call_limit
                                    and continuation_repeats <= 2)
                    trace.event('continuation_report', task_id=task_id, resumed=can_continue,
                                calls_remaining=budget['calls'],
                                reports_without_progress=continuation_repeats)
                    if can_continue:
                        messages.append({'role':'system','content':
                            'Progress report saved in this conversation. The task is still active; continue '
                            'the next unfinished requirement now with tools, using the same remaining budget. '
                            'Do not repeat this report or restart completed work. Make a focused edit/check '
                            'toward the stated next step. Finish only when the requested scope is verified, '
                            'an evidenced external blocker requires the user, or the budget requires a final report.'})
                        self.dashboard.action(label, 'Progress report received; continuing the next step within the same budget.')
                        continue
                    outcome = result(answer, partial=True)
                    outcome['stop_reason'] = ('continuation_stalled' if continuation_repeats > 2
                                              else 'call_limit' if force_final else 'report_budget_boundary')
                    return outcome
                no_progress = implementation_requested and not state.changed and not state.checks and not blocker
                unverified_edit = (self.requires_launch_verification()
                    and not blocker and isinstance(report, dict)
                    and report.get('done') is True
                    and state.checks.get('runtime') != (state.revision, True))
                if internal or no_progress or unverified_edit:
                    trace.event('premature_completion_rejected', task_id=task_id,
                                internal_report=internal, no_progress=no_progress, unverified_edit=unverified_edit,
                                calls_remaining=budget['calls'])
                    if not force_final and completion_repairs < 2 and budget['calls'] > 0:
                        completion_repairs += 1
                        messages.append({'role':'system','content':
                            ('Completion rejected: run the current project after the latest edit and fix any failure. '
                             if unverified_edit else
                             'The answer was rejected: internal history is not a final report, and research alone '
                             'does not deliver an implementation request. Use current evidence to make a focused '
                             'edit and run a relevant check. If already correct, demonstrate it with a check. ') +
                            'If blocked, report needs_user with action, reason and concrete evidence. '
                            'Do not repeat exploration. Stay within the remaining budget.'})
                        self.dashboard.action(label, 'Premature report rejected; continuing within the remaining budget.')
                        continue
                    facts = result('', partial=True)
                    diagnosis = ''
                    next_step = 'Implement the requested change and verify it; inspect recorded failures first.'
                    if not internal:
                        candidate = report
                        if isinstance(candidate, dict):
                            diagnosis = candidate.get('summary') if isinstance(candidate.get('summary'), str) else ''
                            if isinstance(candidate.get('next_steps'), str) and candidate['next_steps'].strip():
                                next_step = candidate['next_steps'][:1500]
                        else:
                            diagnosis = answer
                    facts['answer'] = json.dumps({'summary': 'Work is incomplete. Runtime facts: ' +
                        json.dumps({'modified_files': facts['modified_files'],
                                    'verification': facts['verification']}, ensure_ascii=False) +
                        ('\nModel diagnosis (unverified): ' + diagnosis[:2500] if diagnosis else ''),
                        'next_steps': next_step,
                        'done': False, 'state': 'continue', 'blocker': None}, ensure_ascii=False)
                    facts['stop_reason'] = 'verification_required' if unverified_edit else 'premature_report'
                    return facts
                outcome = result(answer)
                if force_final:
                    outcome['stop_reason'] = 'call_limit'
                elif blocker:
                    outcome['stop_reason'] = 'needs_user'
                return outcome
            names = {c['function']['name'] for c in calls}
            exclusive = len(calls) > 1 and bool(names & {'git_create_branch', 'create_commit', 'git_pull', 'git_push', 'compact_context'})
            # Execute in response order. A later read sees earlier edits; checks
            # use the revision at that point. No concurrent writers or Git changes.
            prefix_failed = False
            for call in calls:
                name = call['function']['name']
                started = time.monotonic()
                event_id = trace.new_id('tool')
                args = {}
                summary = error_code = None
                self.dashboard.update(label, trace.tasks[task_id]['depth'], f'tool Â· {name}')
                try:
                    if salvaged_length and prefix_failed:
                        raise ToolError('skipped_after_prefix_failure', 'Not executed: an earlier call in the recovered prefix failed. Inspect its result before retrying dependent actions.')
                    if force_final or name not in definitions:
                        raise ToolError('tool_unavailable', 'This tool is unavailable. Use the exposed tools or finish the report.')
                    if exclusive:
                        raise ToolError('conflicting_actions', 'No tools in this batch were executed. Branch creation, commits, pulls, pushes and context compaction must each be called alone.')
                    args = parse_arguments(call['function']['arguments'])
                    validate_arguments(definitions[name], args)
                    self.dashboard.update(label, trace.tasks[task_id]['depth'],
                                          describe_tool(name, args))
                    self.dashboard.action(label, 'Starting ' + describe_tool(name, args))
                    if name == 'create_commit' and not self.can_commit(state):
                        raise ToolError('verification_required', 'Verify the latest edit before committing. Run all configured checks, or a qualifying runtime check if supported for this mode.')
                    trace.progress('tool', name=name)
                    handler = tool_handler(name, self.files, self.repository)
                    execute=getattr(self,'execute_tool',handler.execute)
                    operation = asyncio.create_task(asyncio.to_thread(execute, name, args))
                    try:
                        value = await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        # A local write/check cannot be killed by cancelling its
                        # awaiting coroutine. Settle it before exiting the turn.
                        await operation
                        raise
                    state.record_result(name, args, value)
                    summary = self.tool_result_summary(name, value)
                    if name in EDIT_TOOL_NAMES:
                        summary = {key: value.get(key) for key in ('applied', 'files', 'files_changed', 'warnings', 'interface_memory')}
                    elif name == 'run_command':
                        summary = {'passed': value.get('passed'), 'returncode': value.get('returncode'),
                                   'output_tail': value.get('output', '')[-1600:],
                                   'verified_copy': value.get('verified_copy'),
                                   'checkpoint_error': value.get('checkpoint_error')}
                    elif name == 'run_check':
                        summary = {key: value.get(key) for key in ('name', 'passed', 'returncode', 'truncated')}
                    output = {'ok': True, 'result': model_result(name, value)}
                except ToolError as error:
                    error_code = error.code
                    state.record_failure(name, args, self.repository.checks)
                    output = {'ok': False, 'error': error.code, 'message': str(error), **error.details}
                except Exception as error:
                    error_code = type(error).__name__
                    state.record_failure(name, args, self.repository.checks)
                    output = {'ok': False, 'error': 'internal_tool_error',
                              'message': error_description(error) + ' Inspect current files before retrying.'}
                duration = time.monotonic() - started
                self.dashboard.tool_finished(label, name, args, output, duration)
                if not output['ok']:
                    prefix_failed = True
                    summary = {'message': output['message']}
                    for field in ('diagnostic', 'output', 'truncated', 'omitted_bytes'):
                        if field in output:
                            summary[field] = output[field]
                elif name == 'run_check':
                    summary['output'] = output['result']['output']
                trace.record_tool(task_id, label, event_id, call['id'], name, args,
                                  duration, output['ok'], summary, error_code)
                if name in EDIT_TOOL_NAMES and output['ok'] and summary.get('applied'):
                    trace.progress('files', paths=summary.get('files') or [])
                if name in {'run_check', 'run_command'}:
                    trace.progress('verification', name=name, passed=output['ok'] and summary.get('passed', False))
                messages.append({'role': 'tool', 'tool_call_id': call['id'],
                                 'content': json.dumps(self.history_tool_result(name, args, output),
                                                       ensure_ascii=False)})
            if salvaged_length:
                trace.error_message = None
                messages.append({'role':'system','content':length_notice})
                trace.event('output_limit_prefix_settled',task_id=task_id,
                            modified_files=sorted(state.changed),verification=result('')['verification'])
                if length_stop_after_tools:
                    outcome=result((saved_draft + '\n' if saved_draft else '') +
                        'Output/call budget boundary reached. Complete calls were processed; unfinished tail discarded. '
                        'Saved changes: ' + ', '.join(sorted(state.changed)),partial=True)
                    outcome['stop_reason']='call_limit'
                    return outcome
        outcome = result('Call budget exhausted. Inspect saved changes and verification results before continuing.', partial=True)
        outcome['stop_reason'] = 'call_limit'
        return outcome


async def main():
    from orchestra_cli import main as run_console

    await run_console(BASE)


def __getattr__(name):
    if name == 'AgentDashboard':
        from orchestra_console import AgentDashboard

        return AgentDashboard
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    asyncio.run(main())
