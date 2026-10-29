"""Archive large review diffs only after their comparison round is closed."""
import copy
import json


def compact_round_reviews(messages, task_ids, round_id, archive_path, min_characters=4000):
    """Keep call/result pairs and every field except archived diff bodies.

    Save exact original tool messages before replacing anything. Decisions,
    reports, checks, errors, user instructions and other rounds are untouched.
    """
    calls = {}
    for message in messages:
        for call in message.get('tool_calls') or []:
            function = call.get('function') or {}
            if function.get('name') != 'review_task_changes':
                continue
            try:
                arguments = json.loads(function.get('arguments', '{}'))
            except (ValueError, TypeError):
                continue
            if isinstance(arguments, dict) and arguments.get('task_id') in task_ids:
                calls[call['id']] = arguments
    replacements = []
    for index, message in enumerate(messages):
        if message.get('role') != 'tool' or message.get('tool_call_id') not in calls:
            continue
        try:
            output = json.loads(message.get('content', ''))
        except (ValueError, TypeError):
            continue
        if not isinstance(output, dict) or output.get('ok') is not True:
            continue
        result = output.get('result')
        if not isinstance(result, dict) or result.get('task_id') not in task_ids:
            continue
        diff = result.get('diff')
        if result.get('context_compacted') or not isinstance(diff, str) or len(diff) < min_characters:
            continue
        compact = copy.deepcopy(message)
        reduced = copy.deepcopy(output)
        reduced['result'].update(
            diff='[Diff archived after round closure; request review_task_changes again if needed.]',
            context_compacted=True, round_id=round_id, diff_characters=len(diff),
            review_again={'tool': 'review_task_changes',
                          'arguments': calls[message['tool_call_id']]})
        compact['content'] = json.dumps(reduced, ensure_ascii=False)
        if len(compact['content']) < len(message['content']):
            replacements.append((index, copy.deepcopy(message), compact))
    if not replacements:
        return None
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: never overwrite an earlier archive. A write failure
    # leaves the in-memory transcript completely unchanged.
    with archive_path.open('x', encoding='utf-8') as file:
        json.dump({'round_id': round_id, 'messages': [old for _, old, _ in replacements]},
                  file, ensure_ascii=False, indent=2)
    for index, _, compact in replacements:
        messages[index] = compact
    return {'messages_compacted': len(replacements),
            'characters_before': sum(len(old['content']) for _, old, _ in replacements),
            'characters_after': sum(len(new['content']) for _, _, new in replacements)}
