# КТК «Сивка-Бурка»: среда разработки

Репозиторий для будущих сайта, бронирования, бота и CRM клуба в Шерегеше.
Сейчас создаётся и проверяется только пайплайн разработки. Бизнес-архитектура
и приложение ещё не реализованы. Python 3.12+ и Git — единственные зависимости.

Статус и доказательства: [отчёт](reports/PIPELINE_IMPLEMENTATION_REPORT.md).
Правила: [пайплайн](docs/development-pipeline.md), [Git](docs/git-workflow.md),
[роли](docs/agent-responsibilities.md), [жизненный цикл](docs/task-lifecycle.md).

## Работа с задачей

Команды выполняются из основного checkout; пример для новой задачи:

```powershell
Copy-Item .env.example .env
python scripts/task.py create tasks/specs/POC-001.json
python scripts/task.py ready POC-001
python scripts/task.py start POC-001
python scripts/task.py run POC-001
python scripts/task.py validate POC-001
python scripts/task.py status POC-001
```

Повторно запускать уже завершённый POC не нужно. Для новой задачи создайте новый ID
по tasks/TASK_TEMPLATE.md. State хранится локально в .pipeline/state.json.

## Ревью и завершение

Прочитайте задачу, весь diff worktree и reports/runs/<ID>/validation.json.
Составьте JSON по docs/task-lifecycle.md с актуальным snapshot из validation.json:

```powershell
python scripts/task.py review TASK-001 path/to/review.json
python scripts/task.py finish TASK-001
python scripts/task.py cleanup TASK-001
```

При CHANGES_REQUESTED: `run`, `validate`, новое `review`. Cloud-исполнитель получает
контекст через `python scripts/task.py handoff TASK-001`, работает вручную в worktree,
после чего проходит те же validate/review/finish. Автоматического вызова платного API нет.

Проверка инфраструктуры: `python -m unittest discover -s tests/pipeline -v`.
Проверка POC после слияния: `python -m unittest discover -s tests/poc -v`.
