# ADR-002: bounded local worker and deterministic host
Status: ACCEPTED

Use Python standard library and Git worktrees for this bootstrap. Task definitions use
JSON, live state is local/ignored. The local OpenAI-compatible API returns a strict file
envelope; host validates all paths before writing any file. Lead owns validation commands,
task state and review. Cloud adapter is a manual handoff, requiring no additional API key.

This avoids dependence on runtime-specific tool calling. Plain HTTP inference is portable.
Consequence: bounded text edits only, no autonomous terminal exploration. Context is an
explicit allowlist. OS execution of generated tests is trusted development, not sandboxed.
