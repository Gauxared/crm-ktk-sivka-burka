# Git isolation

Primary checkout is the integration workspace (initial branch master).
Task branches: codex/<ID>; task worktrees: .worktrees/<ID>.
Only the controller creates, commits, merges and removes these worktrees.
Start and finish require a clean integration checkout. State/logs are ignored.
No remote is required; no push or production deployment is performed.

Diff scope is checked against the recorded base and includes staged, unstaged,
committed and untracked files. Both sides of renames are included (no rename detection).
Symlinks, junction escapes, traversal, hidden metadata and secret-like paths are rejected.
Forbidden paths override allowed paths. Shared files require explicit lead approval.
Task scopes with overlapping prefixes cannot be active together. This is conservative.

Review seals the complete changed-content snapshot. Finish requires the same snapshot,
unchanged integration HEAD since start, passing validation and PASS review. This prevents
integrating tests run against a stale base. Rebase/recovery is a lead operation and needs
new validation/review; automatic conflict resolution is intentionally absent.
Finish commits task files and merges with --no-ff. Cleanup checks DONE, a clean worktree,
and task commit ancestry before `git worktree remove`; branch is retained for traceability.
Crash recovery: inspect state, Git log and worktree list. Never blindly delete lock/state
or run forced removal. Back up state and reconcile completed Git operations manually.
