"""Load and validate Orchestra runtime configuration."""
import json
import math
from pathlib import Path


def completion_review_enabled(direct):
    route = direct.get('route')
    return 'review' in route if isinstance(route, list) else direct.get('completion_review_enabled', True)


def set_completion_review(direct, enabled):
    direct['completion_review_enabled'] = enabled
    route = direct.get('route')
    if isinstance(route, list):
        if enabled and 'review' not in route:
            route.append('review')
        elif not enabled and 'review' in route:
            route.remove('review')


def load_configuration(directory):
    directory = Path(directory)
    config, settings, prompts, pricing = tuple(
        json.loads((directory / name).read_text(encoding='utf-8'))
        for name in ('config.local.json', 'settings.json', 'prompts.json', 'pricing.json')
    )
    return config, normalize_settings(settings), prompts, pricing


def normalize_settings(settings):
    import copy
    settings = copy.deepcopy(settings)
    settings['direct'] = {k: v for k, v in settings.get('direct', settings.get('pipeline', {})).items()
                          if k in {'writer_request_characters', 'no_progress_calls', 'recovery_calls',
                                   'repeated_action_limit', 'max_task_cost_usd',
                                   'completion_review_enabled', 'completion_review_calls',
                                   'completion_review_output_tokens', 'completion_review_instructions',
                                   'completion_review_policy_version', 'completion_review_budget_percent', 'route'}}
    direct = settings['direct']
    direct.pop('completion_review_calls', None)
    direct.pop('completion_review_policy_version', None)
    direct.setdefault('completion_review_budget_percent', 20)
    for key in ('pipeline', 'roles', 'reasoning_effort'):
        settings.pop(key, None)
    for key in ('completion_review_enabled', 'iterations_per_task', 'stop_after_confirmation',
                'max_output_tokens_per_turn', 'final_output_reserve_tokens'):
        settings.get('execution', {}).pop(key, None)
    return settings



def validate_settings(settings):
    ranges = {
        ("api", "timeout_seconds"): (0, None, False, False),
        ("api", "max_retries"): (0, None, True, True),
        ("generation", "max_output_tokens"): (1, None, True, True),
        ("execution", "max_api_calls_per_turn"): (1, None, True, True),
        ("execution", "max_steps_per_agent"): (1, None, True, True),
        ("execution", "final_answer_target_tokens"): (1, None, True, True),
    }
    for (section, name), (low, high, integer, inclusive) in ranges.items():
        value = settings.get(section, {}).get(name)
        if (type(value) not in ((int,) if integer else (int, float))
                or not math.isfinite(value)
                or (value < low if inclusive else value <= low)
                or (high is not None and value > high)):
            raise ValueError(f"Invalid settings: {section}.{name}")
    if set(settings["generation"]) != {"max_output_tokens"}:
        raise ValueError("Unexpected generation setting.")
    if type(settings['execution'].get('project_map_enabled',True)) is not bool:
        raise ValueError('project_map_enabled must be boolean.')
    map_limit=settings['execution'].get('project_map_context_characters',2500)
    if type(map_limit) is not int or not 1000 <= map_limit <= 16000:
        raise ValueError('project_map_context_characters must be between 1000 and 16000.')
    for key in ('research_calls_before_action', 'implementation_calls_reserve'):
        value = settings['execution'].get(key, 4)
        if type(value) is not int or value < 1:
            raise ValueError(f'{key} must be a positive integer.')
    options = settings.get('tools', {})
    if (not isinstance(options, dict) or set(options) - {'editing_enabled', 'allow_git_mutations', 'allow_git_push', 'git_history_enabled', 'git_tools_enabled'}
            or any(type(value) is not bool for value in options.values())):
        raise ValueError('tools settings must contain supported boolean flags.')
    checks = settings.get('checks', {})
    if (not isinstance(checks, dict) or any(not isinstance(name, str) or not name.strip()
            or not isinstance(argv, list) or not argv
            or any(not isinstance(arg, str) or not arg or '\0' in arg for arg in argv)
            for name, argv in checks.items())):
        raise ValueError('checks must map names to non-empty command argument arrays.')
    if settings["execution"]["final_answer_target_tokens"] >= settings["generation"]["max_output_tokens"]:
        raise ValueError("final_answer_target_tokens must be smaller than max_output_tokens.")


def validate_prompts(prompts):
    required = {"worker", "final_response", "resource_status"}
    if not isinstance(prompts, dict) or set(prompts) != required:
        raise ValueError(f"prompts.json must contain exactly: {', '.join(sorted(required))}.")
    for name, prompt in prompts.items():
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"Prompt '{name}' must be a non-empty string.")
    try:
        prompts["resource_status"].format(
            shared_calls_after=0, agent_calls_after=0,
            delegations_remaining=0, tools_available="no",
            max_output_tokens=2, recommended_output_tokens=1,
            agent_input_tokens_used=0, agent_output_tokens_used=0,
            agent_api_calls_used=0)
    except (KeyError, IndexError, ValueError) as error:
        raise ValueError(f"Invalid resource_status template: {error}") from error
