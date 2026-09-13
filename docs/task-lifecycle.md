# Task schema and lifecycle

Use JSON only, with the fields documented in tasks/TASK_TEMPLATE.md.
Task file status is the initial state; .pipeline/state.json owns live state thereafter.
Edits to a registered spec are not implicitly adopted. Create a new ID for changed scope.

BACKLOG → READY (`ready`, after all dependencies DONE) → ACTIVE (`start`, exclusive owner)
→ REVIEW (`validate`, all gates pass) → DONE (`finish`, approved current snapshot merged).
`create` accepts BACKLOG or READY only. READY requires complete checks and satisfied deps.
`start` enforces explicit depends_on and scope isolation; there is no implicit local-POC gate.
`review` with CHANGES_REQUESTED returns ACTIVE with feedback; BLOCKED or ESCALATE sets
BLOCKED with concrete reasons. Failed validation stays ACTIVE with logged diagnostics.
`resume ID --reason ...` returns BLOCKED to ACTIVE, only if a worktree exists; retain history.
Local attempts are bounded: retry_limit is the number of extra calls after the first.
After the limit, set BLOCKED/escalation; lead may hand off to cloud, never reset counters.
Transport, parse, scope and validation failures are recorded. No silent automatic retry.

Review JSON (lead writes this only after inspecting actual task + diff + gate logs):

```json
{
  "task": "POC-001",
  "reviewer": "cloud:codex",
  "status": "PASS",
  "snapshot": "copy from validation.json",
  "blocking": [],
  "non_blocking": [],
  "tests": {"status": "pass"},
  "scope": {"status": "pass"},
  "architecture": {"status": "pass"},
  "recommended_actions": []
}
```

PASS requires empty blocking, three passing assessments and matching validation snapshot.
Reviews are lead assertions, not cryptographic identities. Worker cannot call the runner.
Changes after validation or review invalidate approval. No DONE on failed merge.
Merge conflicts block; resolve explicitly, do not use forced cleanup or discard changes.

## Execution policy v2

New task templates use cloud. `run ID` on ACTIVE cloud tasks prepares a manual context
handoff and does not touch local configuration or inference. The current Codex agent edits
the worktree directly. `run ID --experimental-local` is required for ACTIVE local tasks;
without opt-in no attempt is consumed. This guard also applies to historical local specs.
No automatic model switch occurs after failure. `handoff ID` explicitly selects cloud.
Roles may be combined; review is called self-review when implementer and reviewer coincide.
The gate evidence and matching snapshot remain required for either execution route.
