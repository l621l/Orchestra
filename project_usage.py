"""Accumulate provider-reported usage for a project."""
import json
from collections import defaultdict

from interface_notes import atomic_text


FILENAME = '.orchestra-usage.json'
IGNORE_RULE = '/' + FILENAME
COUNTERS = (
    'api_calls', 'reported_calls', 'prompt_tokens', 'completion_tokens',
    'total_tokens', 'reasoning_tokens', 'cached_tokens', 'cache_write_tokens',
)


def ensure_ignored(root):
    path = root / '.gitignore'
    if path.is_symlink():
        raise ValueError('Orchestra usage requires a regular .gitignore file.')
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        content = b''
    rule = IGNORE_RULE.encode('ascii')
    if rule not in content.splitlines():
        with path.open('ab') as stream:
            if content and not content.endswith(b'\n'):
                stream.write(b'\n')
            stream.write(rule + b'\n')


def read_usage(root, *, legacy_workspace=None):
    path = root / FILENAME
    if path.is_symlink():
        raise ValueError('Orchestra usage requires a regular statistics file.')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        if legacy_workspace is not None:
            return read_usage(legacy_workspace)
        return {'version': 1, 'currency': 'USD', 'runs': 0, 'total': {}, 'models': []}
    if not isinstance(data, dict) or data.get('version') != 1:
        raise ValueError('Unrecognized Orchestra usage format.')
    return data


def record_usage(root, usage, costs, *, legacy_workspace=None, task='', run_id='', status=''):
    data = read_usage(root, legacy_workspace=legacy_workspace)
    runs = defaultdict(list)
    for entry, cost in zip(usage, costs):
        runs[entry.get('provider') or 'unknown', entry.get('model') or 'unknown'].append((entry, cost))

    def accumulate(target, entries):
        target['runs'] = target.get('runs', 0) + 1
        target['estimated_usd'] = target.get('estimated_usd', 0) + sum(
            cost for _, cost in entries if cost is not None)
        target['complete'] = target.get('complete', True) and all(
            cost is not None and entry.get('reported_calls', 0) == entry['api_calls']
            for entry, cost in entries)
        for key in COUNTERS:
            target[key] = target.get(key, 0) + sum(entry.get(key, 0) for entry, _ in entries)

    models = {(entry['provider'], entry['model']): entry for entry in data['models']}
    for (provider, model), entries in runs.items():
        accumulate(models.setdefault((provider, model), {'provider': provider, 'model': model}), entries)
    accumulate(data['total'], list(zip(usage, costs)))
    stages = {(entry['provider'], entry['model'], entry['stage']): entry
              for entry in data.get('stages', [])}
    history = data.setdefault('history', [])
    for entry, cost in zip(usage, costs):
        if 'stage' not in entry:
            continue
        key = entry['provider'], entry['model'], entry['stage']
        stage = stages.setdefault(key, dict(zip(('provider', 'model', 'stage'), key)))
        accumulate(stage, [(entry, cost)])
        for metric in ('elapsed_seconds', 'edits', 'lines_added', 'lines_removed'):
            stage[metric] = stage.get(metric, 0) + entry[metric]
        history.append({**entry, 'estimated_usd': cost, 'task': task, 'run_id': run_id,
                        'run_status': status})
    data['stages'] = list(stages.values())
    data['runs'] = data['total']['runs']
    data['models'] = sorted(models.values(), key=lambda entry: (entry['provider'], entry['model']))
    root.mkdir(parents=True, exist_ok=True)
    atomic_text(root / FILENAME, json.dumps(data, ensure_ascii=False, indent=2) + '\n')
