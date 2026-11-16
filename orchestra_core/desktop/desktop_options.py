"""Settings fields shared by the desktop interfaces."""

LIMITS = (
    ('direct', 'max_task_cost_usd', 'Task cost limit (USD)', float, 0, 0.1),
    ('api', 'timeout_seconds', 'API timeout (seconds)', float, 0, 180),
    ('execution', 'max_api_calls_per_turn', 'API calls per run', int, 1, 300),
    ('execution', 'max_steps_per_agent', 'Steps per run', int, 1, 100),
    ('generation', 'max_output_tokens', 'Output tokens per request', int, 1, 131072),
    ('direct', 'writer_request_characters', 'Executor request size (characters)', int, 3000, 250000),
)

DEFAULT_CONFIG = {
    'providers': {'mimo': {'api_key': '', 'base_url': 'https://api.xiaomimimo.com/v1'},
                  'gpt': {'api_key': '', 'base_url': 'https://api.openai.com/v1'},
                  'claude': {'api_key': '', 'base_url': 'https://api.anthropic.com/v1/',
                             'transport': 'chat'},
                  'deepseek': {'api_key': '', 'base_url': 'https://api.deepseek.com/v1',
                               'transport': 'chat'},
                  'qwen': {'api_key': '', 'base_url': 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1',
                           'transport': 'chat'}},
    'roles': {'writer': {'provider': 'mimo', 'model': 'mimo-v2.6-pro'}},
}

MODEL_PRESETS = {
    'claude': ('claude-sonnet-4-6', 'claude-opus-4-6', 'claude-haiku-4-5-20251001'),
    'deepseek': ('deepseek-v4-flash', 'deepseek-v4-pro', 'deepseek-chat', 'deepseek-reasoner'),
    'qwen': ('qwen-plus', 'qwen-flash', 'qwen-max'),
}
