# Review protocol (internal reviewer)

Distilled from five adversarial external reviews (PRs #2, #3, #10, #11 +
re-runs). Follow it literally; a verdict without this chain is not a review.

## Rules

- Never implement. Read-only, except writing the verdict. No commits.
- Never review your own implementation work (rotate reviewers if needed).
- Verdicts go on the PR thread with machine-readable first line:
  `<!-- jev-review: approved -->` or `<!-- jev-review: request-changes -->`.

## Evidence chain (every review, in order)

1. **Suite + lint on the PR tree**: `uv run pytest -q` (record count),
   `uv run ruff check .` clean. Any red = request-changes, stop here.
2. **Diff review line by line**: every production hunk gets a why; flag
   speculative generality, scope creep beyond the PR brief, secrets in logs.
3. **Mutation re-run**: for each behavior hunk, revert it and show the
   owning test failing. Tautological tests (pass either way) are findings,
   not coverage.
4. **Live MCP chain** (stdio, no browser needed for wire; browser for
   drives): initialize → tools/list (exact set + canonical schemas) →
   smoke call (validation error path) → one live `drive` + one live `read`
   against example.com/IANA. Paste result fields, not summaries.
5. **Fresh-eyes doc read**: README + CHANGELOG + test guide must describe
   what the code does. Stale claims are findings (they caused real
   confusion twice: `jev_read` background params, 2-vs-3 tool count).

## Live-failure discipline

A failed live run is investigated to root cause (cipher: run it cheaper —
direct `Browser.observe()`, no model, no cost — before theorizing).
"Cannot stage it" is reported with the attempt log, never silently dropped.

## Verdict format

APPROVED or REQUEST CHANGES with majors (blockers, numbered M1..) and
minors. Majors need: file:line, mechanism, fix direction, and why it
matters in production (not in tests). Close with one line on what would
make the next review faster.
