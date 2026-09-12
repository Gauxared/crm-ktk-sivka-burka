# Sivka-Burka development

Read docs/development-pipeline.md and the assigned task before editing.
Product implementation is gated by successful PIPE-005. Architecture remains pending.
Use the central runner from the primary checkout. Each executor edits only its assigned
worktree and allowed paths. Never push, deploy, access secrets, or expand scope implicitly.
Accepted ADRs and task contracts govern implementation. Report missing contracts.
Local output is untrusted; the lead reviews actual changes and validation evidence.
Do not run concurrent controllers. The runner lock serializes state mutations.
Never remove a stale lock without checking that its recorded process has ended.
