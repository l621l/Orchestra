# Orchestra — Direct

## Development routes

Direct now runs a finite sequence of blocks: `implement -> review` by default.
`development_route.py` defines the block interface, registry and route runner;
`completion_review.py` owns the fresh-context review prompt, execution and report.
Both blocks use the existing runtime, tools and shared task cost guard.
An incomplete block stops the route. There is no automatic review loop.

Optional settings under `direct`:

```json
{
  "route": ["implement", "review"],
  "completion_review_budget_percent": 20,
  "completion_review_output_tokens": 8192
}
```

Use `["implement"]` to run implementation alone. If `route` is omitted,
`completion_review_enabled` (default true) selects whether to include review.
Explicit routes take precedence. Unknown or repeated blocks are rejected.
To add a block, implement `run(context, previous)` returning the standard task
result and register it in `BLOCKS`. The runner controls sequencing and stopping;
each block owns its context and limits. This is a code/configuration interface,
not a graphical route editor.

Review receives the requirements, clarifications, implementation report, changed
paths and bounded edit-diff excerpts (including edits already committed). It must
inspect current files where excerpts are insufficient, reproduce suspected bugs,
fix confirmed defects and verify the latest revision. A completion claim without
a successful tool-executed check is rejected. Implementation's forced-edit rule
remains active; review does not require unnecessary edits.

Review now reports `coverage` per requirement (one entry per ID), with `verified`,
`unverified` or `unmet` status and concrete evidence. It must inspect gaps in the
existing tests and execute an independent targeted check. Reported command arguments
are matched to successful tool results after the latest edit; missing coverage or
baseline-only reports cannot pass. This checks evidence structure, not the semantic
quality of the model's tests. Review can commit and push verified fixes when those
options are enabled under Project Settings > Git.
Implementation can use the full task allowance. Review uses the remaining funds
up to 20% of `max_task_cost_usd` by default. Change it under Project Settings > Limits.
Old review call limits are removed when loading configuration; global call/step
limits remain as loop protection. The output ceiling per review request is 8192,
further reduced using remaining funds and an input-cost estimate with a margin.
Each request tells the model its stage allowance, spend, remaining funds, estimated
input cost and approximate number of requests at 2048 output tokens each.
Reasoning tokens count as output. Tokenization/cache estimates can differ from
actual billing, so this is not an exact provider-enforced dollar cap.
If an API response omits usage, the remaining stage allowance is conservatively
reserved and marked estimated, preventing unmetered calls from repeating for free.
Stage spend persists across interruptions. Explicit continuation after a budget
stop starts a fresh allocation, matching the existing task-budget reset behavior.
Budget exhaustion leaves the task partial. Reports and stage statuses are
saved in task state, trace and the local project journal; no `VALIDATION.md` is
automatically created. A resumed partial task starts the route again against the
current files. Review uses fresh messages and defaults to the executor's model.
Choose a separate provider and model under Project Settings > Limits, or set
`roles.reviewer` in the connection configuration. The selected reviewer needs
its own pricing entry. Its charges count toward the shared task budget.

The application stores the live map, journal and cumulative usage under
`logs/projects/<project-id>/`. The ID is derived from the canonical workspace
path and stays the same across branch switches in that folder. Model rates remain
in the application's `pricing.json`. Existing workspace usage is imported on
the next metered run. When switching branches in the desktop app, recognized
legacy journal and map edits are backed up before restoring their tracked copies.
Orchestra commits a repository copy of `PROJECT_MAP.md` generated from the
staged Python sources. It refreshes that copy only when creating a commit and
preserves a user-authored map.

Reasoning effort is selected separately under Project Settings > Models for the executor
and Project Settings > Limits for the reviewer. `Same as executor` inherits the executor's effort.
The role configuration stores `reasoning_effort`; `default` omits the API effort
parameter rather than forcing `low`. Known GPT models expose their supported
levels; unknown models and providers without supported effort controls disable
the selector and omit the parameter. MiMo retains its existing thinking toggle.
Capability reference: https://developers.openai.com/api/docs/guides/deployment-checklist

Исполнитель читает проект, меняет файлы и запускает проверки. В приложении
можно запускать задачи в разных проектах одновременно. У каждого проекта
свои настройки, журнал и кнопка остановки.

## Запуск

Для запуска приложения на Windows установите Python и Git. Из папки Orchestra
выполните:

```cmd
py -m pip install -r requirements-qt.txt
py -B orchestra_qt.py
```

On Ubuntu or Debian (including WSL), run `bash start-linux.sh` from the repository.
The launcher installs missing system packages via APT, prepares a Python virtual
environment, and opens the desktop app. APT may ask for your sudo password.
For commits, configure `git config user.name` and `git config user.email` in
your project. Pushing to GitHub also requires credentials in the Linux environment.

GUI хранит настройки в `%LOCALAPPDATA%\Orchestra`. В **Global Settings** задаются
подключения провайдеров, тайм-аут API и цены моделей. В **Project Settings** выбранного
проекта задаются исполнитель, ревьюер, лимиты, Debug и доступ к Git. Проектные
настройки сохраняются локально по пути проекта и действуют при следующем запуске.
Пока у проекта нет своих настроек, он использует прежние общие значения.
В Linux каталог по умолчанию — `~/.local/share/Orchestra`.

Для запуска из терминала установите зависимости из `requirements.txt`,
скопируйте `config.example.json` в `config.local.json` и укажите ключ провайдера.
Файл с ключом исключён из Git.

```cmd
py -m pip install -r requirements.txt
py -B orchestra.py --workspace "C:\path\to\project"
```

CLI может использовать настройки и историю приложения через `--data-dir`.

## Работа

- `N: <задача>` — новая задача; предыдущий checkpoint архивируется.
- `C` — продолжить сохранённую задачу.
- Обычный текст — уточнение незавершённой задачи или новая задача, если активной нет.
- `S` — локальный статус без обращения к модели; `Q` — выход.
- GUI поддерживает новую задачу, продолжение и остановку.

Сохраняются история диалога и вызовов инструментов, доступные рассуждения модели, изменения, проверки и расход.
При переполнении контекста удаляются старые завершённые группы сообщений,
сохраняется краткая сводка фактов. После отмены незаконченная группа вызовов
не отправляется обратно модели. При успешном запуске с `launch=true`
сохраняется рабочая копия проекта. Журнал проекта — `ORCHESTRA_JOURNAL.jsonl`.

После установленного числа шагов исследования без изменений доступны только
инструменты редактирования; после правки снова доступны чтение и проверки.
Повторяющиеся действия и отсутствие прогресса ограничены. Преждевременный отчёт
может быть отклонён с просьбой закончить работу; это часть Direct, не ревьюер.

## Лимиты и инструменты

`settings.json` содержит:

- `direct.max_task_cost_usd` — денежный порог; проверяется между запросами,
  поэтому один запрос может превысить остаток. Продолжение после превышения
  начинает новый бюджетный отрезок.
- `direct.writer_request_characters` — предел контекста запроса.
- `direct.no_progress_calls`, `recovery_calls`, `repeated_action_limit` — защита от циклов.
- `execution.max_steps_per_agent`, `max_api_calls_per_turn` — предел числа запросов;
  действует меньший из двух.
- `execution.research_calls_before_action` — переход от исследования к редактированию.
- `generation.max_output_tokens` — предел одного ответа, включая reasoning.

Общего лимита выходных токенов на задачу нет. Стоимость рассчитывается по
полученному usage и `pricing.json`; это оценка, а не сверка со счётом провайдера.

Базовые инструменты: список файлов, чтение, поиск, создание, точная замена текста
и ограниченный запуск команд. Некорректный Python отклоняется до записи.
Команды выполняются без shell с ограничением времени и вывода.

Git-инструменты включаются в настройках. Сохранены статус, diff, история,
проверки, подготовка и создание коммита, pull и push. Коммит требует актуальной
проверки. Git-операции выполняются отдельным вызовом; автоматического цикла CI нет.
При выключенном Git агент не вызывает его даже при редактировании файлов.

## Код и проверки

- `direct_dev.py` — задача, контекст, бюджет и защита от зацикливания.
- `orchestra.py` — цикл вызовов инструментов и обработка ошибок.
- `writer_transport.py` — Chat Completions и поток рассуждений модели.
- `task_state.py`, `context_history.py`, `workspace_status.py` — продолжение и статус.
- `runtime_tools.py` — запуск команд и фактические свидетельства выполнения кода.
- `tools.py`, `git_tools.py` — операции с проектом.
- `orchestra_app.py`, `orchestra_cli.py`, `orchestra_qt.py` — интерфейсы запуска.

```cmd
py -B -m unittest discover -q
```

Тесты работают без платных запросов. Они проверяют инструменты, Direct,
продолжение после отмены, лимиты, сохранение reasoning, восстановление после
обрезанного ответа и допуск к коммитам. Качество работы модели проверяется
отдельным реальным прогоном.
