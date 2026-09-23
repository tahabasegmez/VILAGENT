# Evals

The eval suite drives the real machine, so the operator runs it. `suite.yaml` holds the tasks;
`runner.py` runs them against a live gateway and writes one result line per task to
`logs/evals/<label>.jsonl`. How to start the gateway and the runner is in the runner's docstring.

## Commands

- `run --label L [--verifier fara|supervisor|none] [--memory on|off] [--threshold …]` pins those
  settings for the run and restores the operator's afterwards. Every result line records the
  settings, the budget used and the approvals asked and declined.
- `run --legacy` talks to a gateway from before the run API had settings endpoints; that is how
  the baseline (the first version, kept as a git bundle outside the repository) is recorded.
- `run --tag verifier` runs only the tasks built so a step's budget can run out before it is done.
- `compare A B` reports successes, **false passes** (the run said "completed" but the check
  failed) and approvals asked.
- `budgets L1 L2 …` prints the p95 and max use per budget and a suggested limit (1.5 × p95).

Evals only delete files inside `%USERPROFILE%\Documents\vilagent-evals`. Run each on a fresh
desktop.

## Runbook: measure, then tune constants (not structure)

- [ ] **Baseline:** restore the first version from its bundle with
      `git fetch ..\vilagent-01b367a.bundle master:baseline` and
      `git worktree add ..\vilagent-baseline baseline`, start that gateway, then
      `run --label baseline-1 --legacy` (and `baseline-2` for the noise level).
- [ ] **Latest:** `run --label latest-1` on the current code; `compare baseline-1 latest-1`.
- [ ] **Step check:** `run --tag verifier --label verify-fara --verifier fara`, the same with
      `supervisor` and `none`. Make the setting with the fewest false passes (without more
      blocked runs) the default.
- [ ] **Memory effect:** `run --label memory-off --memory off`, a warm-up
      `run --label warmup --memory on`, then `run --label memory-on --memory on`;
      `compare memory-off memory-on`. Tune the recall limits in `vilagent/memory/retrieval.py`
      and the relevance floor in `vilagent/memory/embeddings.py` only if memory-on isn't better.
- [ ] **Approval friction:** from the results, the approvals asked per task. If more than about
      one per task was unnecessary, tighten the approval keywords (the `settings` block of the
      state file) before the threshold.
- [ ] **Budgets:** `budgets latest-1 memory-on …`; set the `ComputerUseBudgetConfig` defaults to
      the suggested limits.
