"""Load a selected runtime profile; credentials remain in config.local.json."""
import copy
import json
import sys
from pathlib import Path


def load_runtime_settings(path, profile='default'):
    with Path(path).open(encoding='utf-8') as file:
        settings = json.load(file)
    if not isinstance(settings, dict):
        raise ValueError('Settings must be a JSON object.')
    profiles = settings.pop('profiles', {})
    if not isinstance(profiles, dict) or 'default' in profiles:
        raise ValueError('profiles must be an object; default is reserved for base settings.')
    def resolve_commands(result):
        checks = result.get('checks', {})
        if not isinstance(checks, dict):
            raise ValueError('checks must be an object.')
        for command in checks.values():
            if isinstance(command, list):
                command[:] = [sys.executable if part == '{python}' else part for part in command]
        return result

    if profile == 'default':
        return resolve_commands(settings)
    if profile not in profiles or not isinstance(profiles[profile], dict):
        raise ValueError(f'Unknown or invalid profile: {profile}. Available: default, ' + ', '.join(profiles))

    def merge(base, override, location=''):
        result = copy.deepcopy(base)
        for key, value in override.items():
            field = location + key
            if key not in base:
                raise ValueError(f'Unknown profile setting: {field}')
            if field == 'checks':
                # A project chooses its checks; do not retain unrelated defaults.
                result[key] = copy.deepcopy(value)
            elif isinstance(base[key], dict) and isinstance(value, dict):
                result[key] = merge(base[key], value, field + '.')
            else:
                result[key] = copy.deepcopy(value)
        return result

    return resolve_commands(merge(settings, profiles[profile]))
