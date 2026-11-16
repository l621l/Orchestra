"""Stage allowances and estimated per-request output limits."""
import json
import math

from orchestra_core.models.model_errors import TaskCostLimit


def allowance(options, review_enabled, reviewing):
    total = options['max_task_cost_usd']
    share = options.get('completion_review_budget_percent', 20) / 100
    return total * share if review_enabled and reviewing else total


def request_budget(remaining, rates, messages, tools, output_limit):
    if remaining <= 0:
        raise TaskCostLimit('Stage budget exhausted. Work remains incomplete.')
    # Reserve for a cache miss because a cache hit is not guaranteed.
    characters = len(json.dumps([messages, tools], ensure_ascii=False))
    input_tokens = math.ceil(characters / 3 * 1.25)
    input_rate = max(rates.get('input_cache_miss', 0), rates.get('input_cache_write', 0))
    input_cost = input_tokens * input_rate / 1_000_000
    output_rate = rates['output'] / 1_000_000
    available = remaining - input_cost
    minimum_output = min(256, output_limit)
    if available < minimum_output * output_rate:
        raise TaskCostLimit(
            f'Remaining budget ${remaining:.5f} cannot cover the next estimated request '
            f'(input ${input_cost:.5f} plus {minimum_output} output tokens). '
            'Work saved; completion is not implied.')
    return min(output_limit, math.floor(available / output_rate)) if output_rate else output_limit
