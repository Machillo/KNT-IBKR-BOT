---
name: context-efficiency
description: Work efficiently in long Claude Code sessions on KNT (audits, research loops, overnight work) by cutting unnecessary context/token use WITHOUT lowering correctness, safety or evidence standards. Use at the start of long tasks and whenever the session is getting large.
---

# Context efficiency (correctness > safety > efficiency)

Saving tokens never means skipping needed tests, assuming uninspected code, weakening risk
controls, omitting evidence, accepting weak statistics, or skipping a required review.

## Rebuild state cheaply
1. `git status`, `git log --oneline -15`, `git diff --stat <base>...HEAD` before re-reading code.
2. Read `docs/OVERNIGHT_PROGRESS.md` (checkpoint) and the relevant skill, not the whole repo.
3. Reuse recent audit findings; re-verify only what changed (`git diff` of that file).

## Read less, precisely
- Search first (Grep/Glob with a pattern), then read only the relevant line range.
- Don't re-read a file you already understand unless it changed; review your own edits via
  `git diff`, not by re-reading modules.
- Dump large multi-file reads to a scratch file only when you truly need all of it.

## Run less, escalate deliberately
- Focused test → module tests → full suite (always full suite before commit).
- Pipe long outputs through `tail`/`grep`; print summaries (counts, aggregates), never raw logs,
  CSVs or DB dumps. Offline research scripts should emit compact tables.
- Long experiments: write results to `reports/` (ignored) and print a ≤ 30-line summary.

## Delegate only for independence
- Use agents for independent review (safety, methodology, release) or wide read-only sweeps —
  not to redo work you already did. One agent per concern; don't launch duplicates; give them the
  saved diff path instead of pasting code.

## Persist the right things
- Durable rules → `CLAUDE.md` / skills (via reviewed commits). Temporary progress → the checkpoint
  doc (compact: baseline, blocks done, decisions, tests, key numbers, pending, next action).
- Experiment outcomes → `docs/experiments/LOG.md` (one entry per hypothesis).
- Small commits with descriptive messages so later sessions understand changes from the diff.
