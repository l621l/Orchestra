"""Stage allowances and approximate per-request spending guidance."""
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
    # Tokenization and cache hits vary. Reserve a margin for input, and disclose
    # that this is an estimate rather than a provider-enforced dollar cap.
    characters = len(json.dumps([messages, tools], ensure_ascii=False))
    input_tokens = math.ceil(characters / 3 * 1.25)
    input_rate = max(rates.get('input_cache_miss', 0), rates.get('input_cache_write', 0))
    input_cost = input_tokens * input_rate / 1_000_000
    output_rate = rates['output'] / 1_000_000
    available = remaining - input_cost
    cap = (min(output_limit, max(256, math.floor(available / output_rate)))
           if output_rate else output_limit)
    typical = input_cost + min(2048, cap) * output_rate
    guidance = (f'\nSTAGE MONEY BUDGET: approximately ${remaining:.5f} remaining. '
                f'Estimated input cost for this request: ${input_cost:.5f}; '
                f'actual output ceiling: {cap} tokens, including reasoning. '
                f'About {remaining / typical:.1f} requests at this context size and 2048 output tokens each. '
                if typical else f'\nSTAGE MONEY BUDGET: ${remaining:.5f} remaining; configured rates are zero. ')
    guidance += ('These are estimates, not a spending target. Cache use and tokenization vary. '
                 'Prioritize material risks, batch useful work, and leave room for verification and the final report. '
                 'Report incomplete coverage honestly when money is insufficient.')
    return cap, guidance
