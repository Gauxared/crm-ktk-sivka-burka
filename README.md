# КТК «Сивка-Бурка»: разработка CRM

Основной путь разработки — облачный исполнитель в Codex. Локальная модель остаётся
необязательным экспериментом: её преимущество по полному времени работы пока не доказано.
Архитектура ARCH-001–007 принята; рабочее приложение ещё предстоит реализовать.
Репозиторий содержит контроллер задач, документы и изолированные POC, а не готовую CRM.
Для самого контроллера нужны Python 3.12+ и Git; зависимости продукта определяются отдельно.

Правила: [процесс v2](docs/development-pipeline.md), [роли](docs/agent-responsibilities.md),
[Git](docs/git-workflow.md), [жизненный цикл](docs/task-lifecycle.md),
[гайд моделей](docs/model-selection-guide.md).
Исторический [отчёт bootstrap](reports/PIPELINE_IMPLEMENTATION_REPORT.md) описывает прежний
процесс и статус на тот момент. Текущая схема работы определена процессом v2.

## Обычная облачная задача

Подготовьте спецификацию по [шаблону](tasks/TASK_TEMPLATE.md) с preferred=cloud.
Команды выполняются из основного checkout; код редактируется только в назначенном worktree.

```powershell
python scripts/task.py create tasks/specs/DEV-001.json
python scripts/task.py ready DEV-001
python scripts/task.py start DEV-001
python scripts/task.py run DEV-001
# Ведущий реализует изменение в указанном worktree.
python scripts/task.py validate DEV-001
python scripts/task.py review DEV-001 reports/runs/DEV-001/cloud-review.json
python scripts/task.py finish DEV-001
python scripts/task.py cleanup DEV-001
```

DEV-001 и review.json в примере — будущие файлы, их нужно подготовить, а не запускать
несуществующую задачу. run готовит контекст для текущего облачного исполнителя;
не запускает отдельную облачную модель и не требует .env, GGUF или TurboLLM.
После замечаний ведущий исправляет код и повторяет validate/review.

## Локальный эксперимент

Только при отдельной задаче с preferred=local и ограниченным бюджетом:

```powershell
python scripts/task.py run EXP-001 --experimental-local
```

Перед вызовом проверить endpoint/model ID/реальный контекст. Инструкции и ограничения:
[локальный адаптер](docs/local-model.md). Без флага локальная генерация не запускается.
`handoff EXP-001` передаёт реализацию ведущему; это не автоматический вызов облачного API.
Повторять неудачную генерацию вместо разработки продукта не требуется.

## Состояние и проверки

`python scripts/task.py status` показывает live state, сохранённый в ignored .pipeline.
Неудачные POC-003/004 остаются диагностическими BLOCKED-задачами, не блокируют непересекающиеся
задачи продукта и не считаются завершёнными реализациями. Их ответы не удалены.
Политика процесса не переписывает исторические спецификации и результаты экспериментов.

```powershell
python -m unittest discover -s tests/pipeline -v
python -m unittest discover -s tests/poc -v
```

Нет подключения оплаты, production-ботов, публикации сайта или push в удалённый репозиторий.
Следующая работа: согласовать уточнения хранения уведомлений, подготовить окружение и
реализовать первый сквозной сценарий по принятым контрактам.
