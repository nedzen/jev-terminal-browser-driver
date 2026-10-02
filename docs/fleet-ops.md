# Fleet operations — worker orchestration playbook

Learned the hard way. Every rule below cost a real incident. Follow them
literally; exceptions need owner approval.

## 1. Never delegate without watching

Firing a prompt and moving on is how workers sit blocked for an hour
(degen's question UI) and pings go unconfirmed (reviewer channel).
After every delegation: confirm receipt (read-back within minutes), track
state (idle/working/blocked) next to every status round, and treat a
missing acknowledgement as an incident, not patience.

## 2. Event-driven monitoring, not polling

Polling sleeps (60–90s) burn wall time and still miss transitions. Use
server-side blocking waits instead:

```bash
herdr agent wait <name> --until blocked   # fires on transition, zero tokens while waiting
```

One waiter per worker, armed at task start, re-armed after each
intervention. Logs are for forensics (`/tmp/fleet-watch.log`); waits are
for intervention. Polling loops are banned except as a backstop.

## 3. One writer per worktree, always

Two writers in one checkout caused a silent near-loss (harden-a/c) and a
second scare (wN in degen's tree). Rules:

- One worker per git worktree, created with `herdr worktree` tooling in
  the project folder so herdr tracks them.
- Workers never commit or push. The merger integrates sequentially,
  suite-green after each merge.
- Name every agent (`herdr agent rename`); name stray panes on sight
  (`spare-a`, `guard-*`) and freeze them read-only immediately.
- Never branch, checkout, or reset inside a worker's worktree. Integrate
  via patch-copy from outside.

## 4. Freeze protocol

When a worker's output is collected: prompt it to stop writing, verify
`git status` is stable across two reads, then treat the tree as
append-only for the merger. Releasing a worker = explicit stand-down,
not silence.

## 5. Unblocking workers

- Read the FULL blocked UI before acting (all question tabs, not the
  first). w4's three-question stop needed three different answers.
- `send-keys` sends keys, not text: use it for select/dismiss (enter/esc),
  never for content. Complex answers go via `esc` + `agent prompt` with
  everything restated (dismissal loses prior answers).
- Answer with data, not permissions: give URLs/text/fields rather than
  blanket authorizations. Prefer structural paths (deny-name over model
  luck) that make the safe outcome inevitable instead of approved.
- Never approve a permission prompt you don't fully understand. Report
  and escalate instead.

## 6. Verification before integration

Every hunk: suite green + ruff clean + mutation check (revert → owning
test must fail). Spot-check worker claims yourself on the merged tree —
their green is evidence, not proof. Record the counts (suite total,
new tests, mutation kills) in the merge commit message.

## 7. Review cadence

Epic branches, not per-batch PRs: merge worker output locally (verified
each merge), open one PR + one review per epic. The reviewer sees
suite-green diffs with mutation notes attached. Reviewer is deep work
only (structural, measurement design); routine checks stay with suite +
merger spot-checks.

## 8. Models and cost

Implementers on the cheap seat (Space Bunny Free), reviewer on the
strong model (Muse Spark — owner-assigned, do not change). Switch models
only on clean idle input; a `/model` prompt mid-task lands as chat text.
A model switch that doesn't take is an incident to report, not retry blindly.

## 9. Reporting contracts

Workers report in chat on completion: hunks, verification numbers,
gaps left open. No report = not done, regardless of green. The merger
records per-epic totals; the roadmap ledger (`docs/roadmap.md`) updates
on every gate close — stale lines are worse than none.
