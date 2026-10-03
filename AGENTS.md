# Agent working agreement

Applies to human and agent contributors alike. Short on purpose — if it
doesn't fit on one page it won't be followed.

## Scope discipline

- Fix the assigned item. No drive-bys, no speculative generality, no
  "while I'm here" refactors. A good change is small and reviewable.
- No new dependencies (pip or npm) without a stated reason. Stdlib first.
- No new tool surface, schema fields, or result fields unless the task
  says so. Surface changes ripple across Hermes/MCP/OpenCode + parity
  tests; propose them, don't smuggle them.

## Production code

- `ruff` clean, line-length 120. No exceptions.
- Never raise out of logging, metrics, or probe paths: report, don't crash.
- Never log secrets: keys, tokens, bearer material, page bodies. Presence
  (booleans, counts, hashes), never values.
- Match existing idioms in the file you're editing. Read it first.

## Tests

Tests are mandatory for new behavior — agent-written code has repeatedly
shipped bugs only tests caught. But tests must earn their lines:

- One behavior per test, named for the behavior, not the function.
- Every new test must prove it bites: revert the production hunk and show
  the test failing (mutation check). Tautological tests (pass with and
  without the change) are worse than none — delete them.
- Mock at seams (clock, browser, network, filesystem), never live services.
  No `time.sleep` in tests; patch the clock or the sleeper.
- Prefer extending the nearest existing test file over creating a new one,
  unless the file exceeds ~400 lines — then split by behavior.

## Docs

- Touching behavior? Update the docs that own the claim:
  [architecture](docs/architecture.md), [decisions](docs/decisions.md),
  [live-testing](docs/live-testing.md), [known-issues](docs/known-issues.md),
  or [roadmap](docs/roadmap.md).
- Tick completed roadmap items; do not leave rationale only in chat.

## Parallel work

- One worker per git worktree (`git worktree add ../<name> <base>`), never
  two writers in one checkout. A merger integrates sequentially, running
  the full suite green after each merge.
- Workers: implement + verify, never commit or push. The merger commits.
- If your change needs another worker's unmerged file, stop and say so —
  don't duplicate it.
