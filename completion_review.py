"""Bounded fresh-context review block for development routes."""
import json
import re
from orchestra import parse_task_report

REVIEW_PROMPT = '''Independently audit the current implementation against ALL original requirements.
The implementation report and existing tests are claims to examine, not proof of correctness. Read the current source for each requirement, including unchanged code and real entry points. Diff excerpts are only navigation hints. Inspect the assertions and fixtures of existing tests: identify what would still be broken if those tests passed.
Build a coverage assessment for every requirement using its ID when available. For each material behavior inspect normal, boundary and failure paths. Seek concrete counterexamples: alternate valid input representations, response ordering, recovery with inconsistent persisted state, partial failures, retries and continuation after a failed item. Select cases relevant to this project. Do not impose a fixed number of hypotheses and do not stop merely because a baseline check passes.
Run the existing relevant checks and independently design and execute targeted checks for gaps in their coverage, with meaningful assertions against actual project code. At least one independent check is required in this review; merely rerunning the implementation's suite is baseline, not independent. Batch related reads and independent checks to use the available calls well. Reproduce suspected defects before fixing them where feasible; apply focused fixes and rerun affected checks after the final edit. Do not manufacture defects, rewrite working code or add speculative features.
Finish when requirements have evidence and material risks have been examined. If calls or budget run out, list the missing checks and return incomplete; do not turn untested behavior into a pass. Do not start another review cycle.
Return JSON with summary, next_steps, done, state, blocker and review={findings, checks, coverage, limitations}.
checks is an array of {argv: [exact command arguments as sent to run_command], kind: "baseline" or "independent", outcome: description of actual result}. An independent check must have succeeded through run_command in this review after the latest edit.
coverage is an array of {requirement: ID or requirement description, status: "verified" or "unverified" or "unmet", evidence: concrete source locations and executed checks or explanation of the gap}. Include every requirement, with one entry per ID when IDs exist. findings describes confirmed defects, fixes and any unresolved issues. limitations describes anything untested.
Use done=true,state=complete only when all coverage entries are verified, no material gap or unresolved defect remains and verification passes. Otherwise use done=false,state=continue (or needs_user for an evidenced external blocker). Report in the user's language.'''


def review_evidence_complete(review, messages, specification):
    """Check report coverage and match an independent check to fresh tool evidence."""
    if not isinstance(review, dict) or not all(
        isinstance(review.get(key), list) for key in ('findings', 'checks', 'coverage', 'limitations')
    ):
        return False
    coverage = review['coverage']
    if not coverage or not all(isinstance(item, dict) and item.get('status') == 'verified'
                              and isinstance(item.get('requirement'), str) and item['requirement'].strip()
                              and isinstance(item.get('evidence'), str) and item['evidence'].strip()
                              for item in coverage):
        return False
    ids = set(re.findall(r'\b[A-Z][A-Z0-9]*-\d+\b', specification))
    covered = {item['requirement'] for item in coverage}
    if not ids <= covered:
        return False
    pending, successful = {}, []
    for message in messages:
        for call in message.get('tool_calls', []):
            try:
                pending[call['id']] = (call['function']['name'], json.loads(call['function']['arguments']))
            except (KeyError, TypeError, ValueError):
                continue
        if message.get('role') != 'tool':
            continue
        try:
            name, args = pending[message['tool_call_id']]
            output = json.loads(message['content'])
            value = output.get('result', {}) if output.get('ok') else {}
            if name in {'create_file', 'replace_text'} and value.get('applied'):
                successful.clear()
            if name == 'run_command' and value.get('qualifies_as_verification'):
                successful.append(args['argv'])
        except (KeyError, TypeError, ValueError):
            continue
    return any(isinstance(check, dict) and check.get('kind') == 'independent'
               and check.get('argv') in successful and check.get('outcome')
               for check in review['checks'])


class CompletionReviewBlock:
    name = "review"

    async def run(self, context, implementation):
        engine = context.engine
        usage, trace, task_id = context.usage, context.trace, context.task_id
        state = engine._state
        evidence = {'specification': state['specification'],
                   'clarifications': state.get('clarifications', []),
                   'implementation_report': implementation['answer'] if implementation is not None else None,
                   'changed_files': state['modified_files'],
                   'recent_edit_diffs': state.get('review_patches', []),
                   'note': 'Diff excerpts may be incomplete and include already committed changes. Current files are authoritative.'}
        instructions = engine.options.get('completion_review_instructions', '').strip()
        prompt = REVIEW_PROMPT
        if instructions:
            prompt += '\nAdditional review priorities from settings:\n' + instructions
        prompt += ('\nOnce a defect is confirmed, prioritize its smallest viable fix and a focused '
                   'regression check before exploring more requirements. Batch related confirmed fixes '
                   'when practical, reuse successful checks for unchanged behavior, and avoid repeating '
                   'the baseline until an edit affects it. Preserve unrelated work. If funds are low, '
                   'prioritize the highest-impact confirmed defect and report the remaining gaps.')
        prompt += engine.git_prompt_suffix()
        messages = [{'role': 'system', 'content': prompt},
                    {'role': 'user', 'content': json.dumps(evidence, ensure_ascii=False)}]
        state['completion_review'] = {'status': 'running', 'messages': messages}
        max_calls = min(engine.settings['execution']['max_api_calls_per_turn'],
                        engine.settings['execution']['max_steps_per_agent'])
        trace.event('completion_review_started', task_id=task_id, max_calls=max_calls)
        engine.journal_event('completion_review_started')
        engine._review_active = True
        before_spend = state['budget_spent']
        budget = {'calls': max_calls}
        # A new pass must be able to repeat the implementation's checks and reads.
        state['signatures'] = {}
        try:
            result = await engine.run('worker', messages,
                budget, usage, trace, task_id,
                max_calls=max_calls, completion_review=True,
                label='reviewer',
                output_limit=engine.options['completion_review_output_tokens'])
            report = parse_task_report(result['answer'])
            review = report.get('review') if isinstance(report, dict) else None
            structured = review_evidence_complete(review, messages, state['specification'])
            passed = (result['status'] == 'completed' and result['verification']['status'] == 'passed'
                      and isinstance(report, dict) and report.get('done') is True
                      and report.get('state') == 'complete' and structured)
            if not passed:
                result.update(status='partial', stop_reason='completion_review_incomplete')
            state['completion_review'].update(status='passed' if passed else 'incomplete', result=result)
            result['completion_review'] = {'status': state['completion_review']['status'],
                                         'report': report, 'verification': result['verification'],
                                         'calls': max_calls - budget['calls'],
                                         'cost_usd': max(0, state['budget_spent'] - before_spend)}
            trace.event('completion_review_finished', task_id=task_id, **result['completion_review'])
            engine.journal_event('completion_review_finished', **result['completion_review'])
            state['messages'].append({'role': 'user', 'content':
                'Completion review result (recorded evidence): ' + result['answer']})
            return result
        finally:
            engine._review_active = False
            if state['completion_review']['status'] == 'running':
                state['completion_review']['status'] = 'interrupted'
            engine.save_state()
