# Шаблон задачи v2

Основной исполнитель cloud. Обязанности разработки, QA и review может выполнять один
ведущий; самопроверка не объявляется независимым review. Все поля ниже обязательны.
Принятая задача с полными requirements, acceptance, dependencies и allowed_paths заранее
авторизует весь безопасный обратимый lifecycle. Не добавляйте отдельное поле подтверждения:
обычные implementation/fix/validation/review/reconcile/finish/cleanup не требуют человека.
Укажите достаточно точный контракт, чтобы escalation требовалась только для настоящего blocker.
Пример нужно адаптировать к реальному контракту и файлам: example.py и tests/example
не являются готовой задачей репозитория. Добавить явные зависимости от нужных ARCH/DEV.

```json
{
  "id": "DEV-001",
  "title": "Implement one agreed behavior",
  "type": "implementation",
  "status": "BACKLOG",
  "priority": "high",
  "executor": {
    "preferred": "cloud",
    "fallback": "cloud"
  },
  "reviewer": "cloud",
  "depends_on": [],
  "allowed_paths": [
    "apps/api/example.py",
    "tests/example/test_example.py"
  ],
  "forbidden_paths": [
    ".env*"
  ],
  "context": [
    "docs/requirements.md"
  ],
  "requirements": [
    "Replace this example with one concrete accepted behavior and its contract"
  ],
  "acceptance": [
    "Observable behavior and meaningful negative cases pass review"
  ],
  "validation": [
    {
      "name": "behavior tests",
      "argv": [
        "{python}",
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests/example",
        "-v"
      ],
      "timeout": 30
    },
    {
      "name": "whitespace",
      "argv": [
        "git",
        "diff",
        "--check"
      ],
      "timeout": 10
    }
  ],
  "risk": "medium",
  "retry_limit": 0
}
```

Спецификация хранится в tasks/specs, live status — в .pipeline/state.json. Регистрация
копирует spec, последующее редактирование файла не меняет уже зарегистрированную задачу.
Команды проверок — argv, не shell-текст; {python} — текущий интерпретатор. Context содержит
только нужные существующие файлы. Разрешённые пути должны соответствовать точному результату.

Для общего кода/конфигурации добавить shared_paths_approval с paths, reviewer, reason
в dedicated cloud task. Это фиксация scope ведущим, не дополнительный запрос пользователя,
если изменение уже входит в его поручение. Исторические спецификации не переписываются.

Для отдельного локального эксперимента явно поставить preferred=local, retry_limit=0
на первом сравнительном прогоне, указать гипотезу, входные данные, критерии и профиль.
Запуск: run ID --experimental-local. Этот флаг не переключает облачную задачу в local.
После неудачи не увеличивать лимиты тайно: сохранить результат и решить, нужен ли новый
эксперимент или handoff. Производственная задача не зависит от успеха такого эксперимента.
