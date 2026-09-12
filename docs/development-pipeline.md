# Development pipeline

Version 1 is a semi-automatic, local development tool, not a production sandbox.
Lead defines contracts/tasks; runner creates a worktree; local model proposes files;
runner applies only permitted paths; lead runs validation, reviews, merges, cleans up.
PIPE-001 through PIPE-005 precede all product work. PIPE-005 requires actual local
generation and a reviewed, tested, merged POC. ARCH tasks stay BACKLOG until then.

Task definitions are versioned JSON in tasks/specs. Live lifecycle is centralized in
.pipeline/state.json (ignored): this avoids six duplicate task folders and prevents
operational state changes from dirtying the integration checkout during merges.
Each task record stores the full approved specification, owner, base commit, branch,
worktree, attempts, retries, review, blockers, history and UTC timestamps.
Reports and prompts live in ignored reports/runs; copy selected non-secret evidence
to reports/evidence for durable Git history. Git is the source of truth for code.

The controller holds an exclusive lock for each command, including inference/checks.
One controller at a time; several isolated worktrees may exist. Conflicting write
scopes are rejected conservatively. This version favors correctness over throughput.
One local request at a time matches the configured parallel=1 server.
The worker has no shell, network, Git, deployment or production credentials.
The lead supplies explicit source/context files; no recursive repository upload.
The runner executes only validation argv arrays from the lead-approved specification.
Generated code must be inspected before validation: tests run under your OS account.

No installation, hosted services, CI service or cloud credentials are required.
Run the infrastructure test suite locally before integrating runner changes.

Genesis exception: PIPE-001..004 are built and reviewed by the lead in the initially
empty primary checkout, because isolation and validation do not exist beforehand.
scripts/accept_bootstrap.py records a cloud attestation referencing the current clean
commit and tracked evidence. It accepts only PIPE-001..005, enforces dependencies,
and additionally requires local generation, merged POC-001 and cleanup for PIPE-005.
It cannot close normal product tasks. The full worktree lifecycle is proved by POC-001.
