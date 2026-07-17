# BASE v2 Gap — Quick Notes

**Date:** 2026-07-17
**Status:** Closed (no action needed today)
**Trigger:** SEED `graduate` task hard-stops when a `base` v1 binary is detected on PATH; only an informational notice when no binary is present.

## Current state on this machine

- `which base` returns nothing — no BASE binary installed.
- No `.base/` directory in the project or in `~/.base/`.
- `.claude/commands/seed/` is installed locally (via `seed --local`); SEED tasks will run and emit a "BASE not detected, continuing standalone" notice. No blocker.

## Brain foundation dependency on BASE

**None.** The brain spec + plan use:
- Local SQLite (`brain.db`) for runtime queries
- Local JSONL (`events.jsonl`) for append-only history
- Local yaml/json files (`rules.yaml`, `persona_hints.json`, `profile.json`)
- Local diffs (`data/brain/diffs/*.json`)

External integrations the brain does NOT need:
- Knowledge graph (entity registration) — would be useful post-graduation
- Workspace intelligence / proactive context — out of scope for this addon
- Project registry (`base project add`) — not relevant for an embedded package

## Decision

- **No install today.** BASE v2 install requires the paid community at `https://chrisai.cv/skool`; not worth the friction for an MVP brain foundation.
- **Defer for a future sub-project.** When the next sub-project starts (vision CAPTCHA solver or Branded Surveys MVP), re-evaluate whether knowledge-graph registration at "graduation" time adds value. At that point, if BASE v2 is desired, install + plan its integration.
- **SEED commands stay locally installed** under `.claude/commands/seed/` and are gitignored so they don't pollute `git status`. Reinstall with `seed --local` if wiped.

## Operational notes

- If `base` ever shows up on PATH and reports a v1 version, SEED `graduate` will hard-stop. Today, no risk.
- If we later install BASE v2, re-run graduation of any project we want registered in `base project list`. The brain itself doesn't graduate as a project — it's a package inside `survey-agent/` — so the BASE integration story is higher-level than this gap.

## Cross-references

- Brain spec: `docs/superpowers/specs/2026-07-16-brain-foundation-design.md`
- Brain plan Task 0: `docs/superpowers/plans/2026-07-16-brain-foundation.md`
- SEED install verification: `which seed && seed --help`; install with `seed --local`
- BASE v2 install: `https://chrisai.cv/skool` (paid community)
