# Sivka-Burka development

Read docs/development-pipeline.md and the assigned task before editing.

## Autonomous Execution Policy

An accepted task with defined requirements, acceptance criteria, dependencies and allowed
paths pre-authorizes its complete safe, reversible lifecycle. Drive it from preparation through
implementation, fixes, validation, self-review, reconciliation when applicable, finish and
cleanup without asking whether to continue after routine stages. Report one final result, or one
specific true blocker.

Escalate only for an unresolved product decision or contract conflict, missing credentials or
access, paid or production action, destructive or irreversible operation, required scope change,
or an exhausted local recovery path. A test failure, implementation defect, lint issue, ordinary
merge/rebase recovery, or a need to repeat validation/review is not a human checkpoint: diagnose,
fix within the accepted scope, validate again and review the current diff again.

Cloud is the default product implementer. Architect, developer, QA and reviewer are
responsibilities, not mandatory separate agents. Do not spawn agents without explicit authorization.
Local generation is optional experimental work, never a prerequisite for product delivery.
Use the central runner from the primary checkout. Edit only the assigned worktree and
allowed paths. Never push, deploy, access secrets, or expand scope implicitly.
Accepted business contracts govern implementation. Report missing contracts; do not invent them.
Local output is untrusted. Review generated code before executing it; retain failed evidence.
Use appropriate automated checks and review the actual diff. Do not claim independent
review when the same agent implemented and reviewed the change.
Do not run concurrent controllers. Never remove a stale lock without checking its process.
