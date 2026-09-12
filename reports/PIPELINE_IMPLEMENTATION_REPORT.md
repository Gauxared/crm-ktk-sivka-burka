# Отчёт о пайплайне разработки «Сивка-Бурка»

13 сентября 2026, Asia/Bangkok. Машинные журналы используют UTC.
**PIPE-001–PIPE-005: DONE. POC-001: DONE.** Проверен локальный полуавтоматический
цикл разработки; продуктовое приложение пока не создано.

## 1. Что реализовано

| Этап | Результат | Доказательства |
| --- | --- | --- |
| PIPE-001 | Monorepo, задачи JSON, роли, статусы, ADR, канонические документы | README, docs/, agents/, tasks/TASK_TEMPLATE.md |
| PIPE-002 | Branches/worktrees, scope, shared-file protection, блокировка контроллера | scripts/policy.py, scripts/task.py, tests/pipeline/ |
| PIPE-003 | AgentExecutor, LocalExecutor, ручной CloudExecutor, turboLLM API | scripts/executors.py, настоящие запросы POC |
| PIPE-004 | Validation, ревью snapshot, retry limit, escalation | 27 инфраструктурных тестов и structured review |
| PIPE-005 | Локальный код → проверки → ревью → merge → DONE → cleanup | evidence/POC-001/, evidence/PIPE-005-review.json |

PIPE-001–004 реализованы ведущим агентом в исходно пустом основном checkout.
Это записанное исключение для создания самой инфраструктуры, а не четыре запуска
локального воркера. Полный цикл с изоляцией доказан на POC-001.

## 2. Структура репозитория

```text
apps/                 api/health.py; placeholders web/admin/bot
packages/             placeholders contracts/ui/config
docs/                 процесс, локальная модель, POC-контракт, бизнес-черновики
decisions/            ADR по monorepo и границе исполнителей
agents/               architect, local-worker, reviewer, qa
tasks/                TASK_TEMPLATE.md, specs/*.json
scripts/              task.py, policy.py, executors.py, bootstrap helpers
tests/pipeline/       27 инфраструктурных тестов
tests/poc/            4 независимых + 4 теста воркера
reports/evidence/     запросы, ответы, проверки, ревью, снимок состояния
.pipeline/            локальные state.json и временная блокировка, ignored
.worktrees/           ignored; POC worktree уже удалён
.env                  локальная настройка подключения, ignored
```

Спецификации лежат в tasks/specs, живой статус — централизованно в .pipeline/state.json.
Это устраняет дублирование файлов задачи по шести каталогам и изменения основного
checkout при каждом переходе. status в JSON-спецификации — начальное значение.

## 3. Жизненный цикл

BACKLOG → READY → ACTIVE → REVIEW → DONE; BLOCKED фиксирует причину остановки.
READY проверяет зависимости, start — владельца, чистоту checkout и пересечение scope.
Продуктовые задачи не могут стартовать до DONE у PIPE-005.

Состояние содержит спецификацию, исполнителя, branch/worktree/base commit, попытки,
retry_count, review/validation, blockers, timestamps и историю событий.
Архив после bootstrap: [state-after-bootstrap.json](evidence/state-after-bootstrap.json).

## 4. Git-изоляция

Ветка POC: `codex/POC-001`; каталог: `.worktrees/POC-001`. Основная ветка сохранена
как исходная `master`. Внешнего remote нет, push не выполнялся.

Scope проверяет staged, unstaged, committed и untracked изменения относительно base.
Есть regression test для staged-изменения вне scope, скрытого восстановленной рабочей
копией. Запрещены traversal, ссылки/junction, метаданные контроллера, secret-like paths;
forbidden_paths имеют приоритет над allowed_paths. Shared-области требуют явного одобрения.

- `23e39fd`: исходная инфраструктура PIPE-001–004.
- `fdc22e5`: reasoning_effort после первой неудачи модели.
- `bb36267`: реализация POC-001 локальной моделью, принятая ведущим.
- `5651a2d`: merge POC-001 после PASS.

Перед повтором чистый worktree перебазирован на исправленный адаптер; старый base,
причина и первая попытка сохранены в истории. После merge worktree удалён штатным Git,
ветка оставлена для трассировки. Продуктовая разработка не начата.

## 5. Интеграция локальной модели

Endpoint: `http://127.0.0.1:6996/v1/chat/completions`, turboLLM gateway.
Runtime: `llama.cpp-b9608-rocm`, backend порт 8081.
Model ID: `qwen3.8-27b|IQ3_XXS|10934860704`; ответ: `Qwen3.8-27B-UD-IQ3_XXS.gguf`.
Файл IQ3_S в C:/LocalsLllm/unsloth автоматически не подменяет эту модель.

После пользовательской настройки подтверждены контекст 51 712, KV K/V `q4_0`,
GPU layers 99, parallel 1. Исходный профиль имел 16 384 и KV `q8_0`.
Раннер не менял конфиг turboLLM, не запускал второй сервер и не устанавливал пакеты.

В проектном .env: endpoint, model, context limit 51712, max output 4096, timeout 180 s,
`LOCAL_LLM_REASONING_EFFORT=off`. API key поддержан, но локально не требовался;
Authorization не записывается в request log. Источники контекста не содержат секретов.

Воркер получает только спецификацию, явно перечисленные исходники и feedback.
Он возвращает JSON с содержимым разрешённых текстовых файлов и handoff. Хост проверяет
весь список до первой записи и запускает команды из утверждённой задачи. Модели не
предоставлены терминал, Git или произвольное чтение диска. Streaming и native tools
адаптеру не нужны и отдельно не проверялись. Подробности: docs/local-model.md.

## 6. Выбор исполнителя

LOCAL: небольшие обратимые задачи с точным контрактом. LOCAL + CLOUD REVIEW: UI, CRUD,
DTO, адаптеры, тесты, миграции по принятой схеме. CLOUD: анализ, декомпозиция, сложная
интеграция. CLOUD ONLY: архитектура, auth, платежи, конкуренция бронирований, shared contracts.
Любой локальный код проходит облачное ревью до merge. CloudExecutor формирует ручной
handoff; платный API автоматически не вызывается.

## 7. Проверки

- **27 инфраструктурных тестов: PASS**: реальный Git lifecycle, scope, junction escape,
  stale review, drift основного HEAD, лимит попыток, feedback и reasoning_effort.
- **8 HTTP POC-тестов: PASS** в worktree и повторно после merge. Четыре независимых
  lead-теста и четыре теста воркера покрывают четыре основные группы поведения.
- Syntax checks и whitespace diff checks: PASS.
- Тесты используют реальные временные loopback-серверы и sockets; серверы закрыты.

У bootstrap CLI нет внешнего lint/typecheck/build toolchain: только стандартная библиотека
Python. Task-specific argv gates позволяют добавить команды после выбора продуктового
стека. Они выполняются без shell=True. Git whitespace проверен дополнительно с HEAD,
чтобы включить staged-добавления.

## 8. Ревью

Ведущий просмотрел полный фактический diff обоих файлов, контракт и gate logs.
Проверены точные маршруты и JSON, Content-Type/Length, отсутствие bind при импорте,
loopback по умолчанию, жизненный цикл сервера и отсутствие бизнес-логики.
Результат: [PASS](evidence/POC-001/review.json), блокирующих замечаний нет.

Ревью привязано к snapshot содержимого, индекса, HEAD/base и Git metadata.
Изменения после validation/review требуют новых проверок и одобрения.
Неблокирующее замечание: тесты воркера повторяют независимые тесты; восемь методов
нельзя представлять как восемь независимых сценариев.

## 9. Повторы и эскалация

retry_limit=1 — одна дополнительная попытка после первой. POC использовал обе.
Первая неудача сохранена с запросом и сырым ответом; неполный ответ не применялся.
После лимита раннер блокирует локальные обращения и предлагает cloud handoff.
CHANGES_REQUESTED возвращает ACTIVE с замечаниями; затем нужны validation и новое review.
Этот переход и передача ошибки следующему вызову проверены инфраструктурными тестами.

## 10. Настоящий POC-001

Задача: стандартный HTTP health-check с `GET /health`, точным JSON и 404 для остальных
путей. Модель создала apps/api/health.py и tests/poc/test_health_worker.py.

| Показатель | Попытка 1 | Попытка 2 |
| --- | --- | --- |
| Reasoning | отсутствует параметр; шаблон по умолчанию xhigh | явно off |
| Prompt tokens | 1561 | 1535 |
| Completion tokens | 4096, всё в reasoning | 871, ответ с файлами |
| Время | около 168,3 s по журналу | 28,875 s по клиенту |
| Finish reason | length | stop |
| Применено файлов | 0 | 2 |
| Итог | отклонён раннером | проверки → PASS → merge → DONE → cleanup |

Контекст и KV-кэш изменились между попытками: это не чистый benchmark reasoning on/off.
Подтверждена работоспособность второго режима на маленькой задаче; экономия облачных
токенов и качество больших задач не измерены.

Доказательства: [history](evidence/POC-001/history.json), [первый запрос](evidence/POC-001/attempt-01-request.json),
[первый ответ](evidence/POC-001/attempt-01-response.json), [повторный запрос](evidence/POC-001/attempt-02-request.json),
[результат](evidence/POC-001/attempt-02-result.json), [validation](evidence/POC-001/validation.json).

## 11. Ограничения

- Это проверенный локальный bootstrap, не production-ready платформа и не CRM.
- Один доверенный контроллер. Несколько worktrees допустимы без пересечения scope;
  распределённые контроллеры и параллельные обращения к GPU не реализованы.
- Проверки исполняют код под аккаунтом разработчика без OS sandbox; ведущий должен
  просматривать сгенерированный код до запуска команд.
- Только ограниченные текстовые замены. Нет автоматических удалений, binary patches,
  native tools или терминального агента модели.
- API timeout не заменяет общий watchdog дерева дочерних процессов.
- Review identity — аттестация ведущего, не криптографическая система полномочий.
- Изменение base, merge conflict или crash между Git/state требуют восстановления
  ведущим. Нет слепого удаления lock, force-cleanup или auto-merge.
- .pipeline/state.json локален и ignored. Новый клон сохраняет Git и архивный snapshot,
  но не восстанавливает автоматически живое состояние контроллера.
- Один POC не доказывает устойчивость 51k-контекста, оптимальность KV q4_0,
  качество сложного кода или пригодность health-check для публичного сервиса.

Первый инфраструктурный прогон выявил ошибку аргументов PowerShell в junction-фикстуре;
она исправлена. Проверка staged-scope расширена regression test. Оба случая устранены
до приёмки инфраструктуры; неудачный модельный запуск также не скрыт.

## 12. Следующее действие

Можно переходить к **ARCH-001 — Define MVP requirements**. Созданы ровно семь задач
ARCH-001–007, все в BACKLOG; бизнес-архитектура автоматически не выполнялась.
Сначала нужны реальные услуги клуба, роли, каналы бронирования, ограничения ресурсов
и критерии MVP. Затем — контекст системы, домен, жизненный цикл, API, данные, уведомления.
