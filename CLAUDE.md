<!-- gitnexus:start -->

# GitNexus — Code Intelligence

This project is indexed by GitNexus as **survey-agent** (838 symbols, 1347
relationships, 33 execution flows). Use the GitNexus MCP tools to understand
code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it
> auto-selects an available runner. No `.gitnexus/run.cjs` yet?
> `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a
  function, class, or method, run
  `impact({target: "symbolName", direction: "upstream"})` and report the blast
  radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only
  affect expected symbols and execution flows. For regression review, compare
  against the default branch:
  `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before
  proceeding with edits.
- When exploring unfamiliar code, use `query({search_query: "concept"})` to find
  execution flows instead of grepping. It returns process-grouped results ranked
  by relevance.
- When you need full context on a specific symbol — callers, callees, which
  execution flows it participates in — use `context({name: "symbolName"})`.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings
  (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands
  the call graph.
- NEVER commit changes without running `detect_changes()` to check affected
  scope.

## Resources

| Resource                                      | Use for                                  |
| --------------------------------------------- | ---------------------------------------- |
| `gitnexus://repo/survey-agent/context`        | Codebase overview, check index freshness |
| `gitnexus://repo/survey-agent/clusters`       | All functional areas                     |
| `gitnexus://repo/survey-agent/processes`      | All execution flows                      |
| `gitnexus://repo/survey-agent/process/{name}` | Step-by-step execution trace             |

## CLI

| Task                                         | Read this skill file                                        |
| -------------------------------------------- | ----------------------------------------------------------- |
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md`       |
| Blast radius / "What breaks if I change X?"  | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?"             | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md`       |
| Rename / extract / split / refactor          | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md`     |
| Tools, resources, schema reference           | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md`           |
| Index, status, clean, wiki CLI commands      | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md`             |

<!-- gitnexus:end -->

## Agent skills

### Issue tracker

GitHub Issues via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five labels: needs-triage, needs-info, ready-for-agent, ready-for-human,
wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context repo. See `docs/agents/domain.md`.

<!-- agenttrail -->

## agenttrail plan convention

Maintain PLAN.md as the living plan. It is read by the project OWNER, not by you
— write it for them.

- nodes are COMPONENTS of the system being built
  (`## Plain-language name {#id}`), not phases or sprints; keep the map at 5-9
  components regardless of repo size — grow tasks, not cards, and split a
  component only when one agent could no longer own it for a session
- naming rule: titles are verb-led, plain-language, and CONCRETE — the owner can
  tell when it is done ("Read alerts out loud", "Watch the repo"). Never
  engineer-speak ("fs watcher + activity signal") and never vague vibes ("Decide
  what matters"); put the engineer phrasing on a `tech:` line under the heading
- tasks inside a component: `- [ ] Plain outcome {#id}`, optional indented
  `tech:` line beneath; mark a task `[~]` BEFORE you start it and save PLAN.md
  immediately — this drives the live in-progress view; flip it to `[x]` the
  moment it completes, `[!]` if stuck (clear once unblocked). Never batch plan
  updates for the end of the session
- when you mark a task `[~]`, add an indented `by: <your name>` line under it
  (claude, codex, cursor, …) and leave it there when done — it is the record of
  who did what
- edges under a component heading: `needs: [id, id]` = must come after those
  components; `links: [id, id]` = interconnected with / talks to
- `files: [src/audio/**, config.py]` under a component declares which paths it
  owns — keep it current; it is how the live view knows which component you are
  really working in, including when you revisit finished work
- `{#id}`s are stable — never rename, only add or remove nodes
- open tasks carry an indented `from:` line naming their provenance —
  `from: agent` when YOU are declaring it as your own imminent build intent (the
  owner corrects these on sight if wrong), `from: roadmap` when it comes from
  planning documents (durable intent, backloggable); omit when neither
- before ending a session, graduate your plan-worthy completed todos into
  PLAN.md as `[x]` tasks (with `by:`) — housekeeping todos stay out of the plan
- record any plan-affecting decision under `## decisions` BEFORE implementing it
