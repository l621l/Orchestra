"""Deterministic limits for implementation requests."""
import time
from tools import definition
from tool_errors import ToolError
from tool_contracts import EDIT_TOOL_NAMES

BLOCKER_TOOL = definition(
    'report_blocker',
    'Report a concrete obstacle that prevents completing the task. Provide inspected evidence and the needed next step. Ends tool use and requests a final partial report, preserving existing changes. Available with or without Git.',
    {'reason': {'type': 'string', 'enum': ['missing_information', 'unavailable_capability', 'unsafe_change', 'no_justified_change']},
     'evidence': {'type': 'string', 'minLength': 1, 'maxLength': 4000, 'description': 'Specific inspected facts supporting the blocker.'},
     'next_step': {'type': 'string', 'minLength': 1, 'maxLength': 4000, 'description': 'What would resolve the blocker, or why no change is needed.'}},
    ['reason', 'evidence', 'next_step'])


class ImplementationPhase:
    def __init__(self, settings, enabled):
        self.edit_failures = {}
        self.disabled_edits = set()
        self.enabled = enabled
        self.workflow = settings.get('workflow', 'focused')
        self.max_read_lines = settings.get('implementation_read_max_lines', 120)
        self.edit_retry_limit = settings.get('edit_retry_limit', 2)
        self.started = time.monotonic()
        self.research_calls = 0
        self.active = False
        self.reason = None
        self.reads = 0
        self.max_reads = settings.get('implementation_max_read_calls', 2)
        self.max_research_calls = settings.get('implementation_max_research_calls', 6)
        self.deadline = self.started + settings.get('implementation_research_seconds', 180)
        if self.workflow == 'project':
            self.deadline = float('inf')
        self.reserve = settings['implementation_reserve_calls']

    def enter(self, reason):
        if self.workflow == 'project' and reason != 'remaining_calls':
            return
        if self.enabled and not self.active:
            self.active, self.reason = True, reason

    def update(self, remaining):
        if not self.enabled or self.active:
            return
        if remaining <= self.reserve:
            self.enter('remaining_calls')
        elif self.workflow == 'project':
            return
        elif self.research_calls >= self.max_research_calls:
            self.enter('research_calls')
        elif time.monotonic() >= self.deadline:
            self.enter('research_time')

    def record_edit_failure(self, name, code):
        if name not in EDIT_TOOL_NAMES or code == 'edit_retry_limit':
            return
        key = (name, code)
        self.edit_failures[key] = self.edit_failures.get(key, 0) + 1
        if self.edit_failures[key] >= self.edit_retry_limit:
            self.disabled_edits.add(name)

    def tools(self, tools):
        tools = [t for t in tools if t['function']['name'] not in self.disabled_edits]
        if not self.active:
            return tools
        if self.workflow == 'project':
            return [tool for tool in tools if tool['function']['name'] not in {'delegate_task', 'start_comparison_round'}]
        allowed = set(EDIT_TOOL_NAMES) | {'run_check', 'git_status', 'git_diff', 'prepare_commit', 'create_commit', 'git_create_branch', 'review_task_changes', 'apply_task_changes', 'report_blocker'}
        if self.reads < self.max_reads:
            allowed.add('read_file')
        return [tool for tool in tools if tool['function']['name'] in allowed]

    def admit(self, name, args):
        if name in self.disabled_edits:
            raise ToolError('edit_retry_limit', f'This editing tool is disabled for this turn after {self.edit_retry_limit} errors of the same type. Use another editing tool with a corrected approach, or report a concrete blocker.')
        if not self.active:
            return
        if self.workflow == 'project':
            if name in {'delegate_task', 'start_comparison_round'}:
                raise ToolError('research_closed', 'Finalization reserve: finish current work without starting another worker.')
            return
        if name not in EDIT_TOOL_NAMES | {'run_check', 'git_status', 'git_diff', 'prepare_commit', 'create_commit', 'git_create_branch', 'review_task_changes', 'apply_task_changes', 'report_blocker', 'read_file'}:
            raise ToolError('research_closed', 'Research is closed. Apply the supported change, verify it, or report a concrete blocker.')
        if name == 'read_file':
            start, end = args.get('start_line'), args.get('end_line')
            if type(start) is not int or type(end) is not int or start < 1 or end < start or end - start + 1 > self.max_read_lines:
                raise ToolError('focused_read_required', f'Implementation reads require explicit start_line/end_line spanning at most {self.max_read_lines} lines.')
            if self.reads >= self.max_reads:
                raise ToolError('implementation_read_limit', 'No implementation reads remain. Use existing evidence or report the missing information.')
            self.reads += 1


def validate_blocker(args):
    reasons = BLOCKER_TOOL['function']['parameters']['properties']['reason']['enum']
    if (set(args) != {'reason', 'evidence', 'next_step'}
            or args.get('reason') not in reasons
            or any(not isinstance(args.get(k), str) or not 1 <= len(args[k].strip()) <= 4000
                   for k in ('evidence', 'next_step'))):
        raise ToolError('invalid_blocker', 'Provide reason, specific evidence, and next_step; each text must contain 1â€“4000 characters.')
    return {key: value.strip() for key, value in args.items()}
