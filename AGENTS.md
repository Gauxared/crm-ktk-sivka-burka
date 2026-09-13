# Sivka-Burka development

Read docs/development-pipeline.md and the assigned task before editing.
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
