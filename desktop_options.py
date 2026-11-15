"""Settings fields shared by the desktop interfaces."""

LIMITS = (
    ('direct', 'max_task_cost_usd', 'Порог расходов на задачу, USD', float, 0, 0.1),
    ('api', 'timeout_seconds', 'Тайм-аут API, сек.', float, 0, 180),
    ('execution', 'max_api_calls_per_turn', 'Вызовы API за запуск', int, 1, 300),
    ('execution', 'max_steps_per_agent', 'Шаги за запуск', int, 1, 100),
    ('generation', 'max_output_tokens', 'Выходные токены на запрос', int, 1, 131072),
    ('direct', 'writer_request_characters', 'Размер запроса исполнителя, символы', int, 3000, 250000),
    ('direct', 'no_progress_calls', 'Шаги без прогресса до восстановления', int, 1, 6),
    ('direct', 'recovery_calls', 'Шаги восстановления', int, 1, 4),
)

DEFAULT_CONFIG = {
    'providers': {'mimo': {'api_key': '', 'base_url': 'https://api.xiaomimimo.com/v1'},
                  'gpt': {'api_key': '', 'base_url': ''}},
    'roles': {'writer': {'provider': 'mimo', 'model': 'mimo-v2.6-pro'}},
}
