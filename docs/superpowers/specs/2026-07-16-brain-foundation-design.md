# Brain Foundation — Design Spec

**Status:** Draft for review **Date:** 2026-07-16 **Scope:** MVP slice only —
event capture, multi-tier storage, autonomous digest, persona safety. Future
sub-projects (vision CAPTCHA solver, Branded Surveys MVP) build on this
foundation.

---

## 1. Goals

Build a small "brain" foundation for `survey-agent` that:

1. Captures structured failure/success events from every survey action via a
   single ingest API.
2. Stores them in three tiers: short-term (JSONL), medium-term (SQLite,
   queryable at runtime), long-term (rules + persona hints, human-readable).
3. Promotes repeated failure signatures to a candidate pool automatically.
4. Runs an LLM digest autonomously on time/volume/promotion triggers — no CLI
   opt-in.
5. Writes consolidated rules to `rules.yaml` automatically.
6. Writes persona refinement proposals to `profile.json` automatically, **but
   only on a strict allowlist**; demographic and answer-materially-affecting
   fields are denylisted by construction.

## 2. Non-Goals

- No mid-session persona switching — the persona is locked once a session
  starts.
- No live inference of "what should I be" — only end-of-trigger-window
  (offline-equivalent) digest.
- No graph DB, no vector store, no full hybrid-search system.
- No OB1/gbrain/wiki-v2 feature parity. We borrow the _pattern_ (multi-tier
  memory, autonomous consolidation, hook-driven ingest, diff audit), not the
  implementation.
- No multi-agent coordination or shared brain between survey-agent instances.
- No knowledge-graph extraction in MVP.
- No confidence scores on facts (gnusupport's critique applies: numeric
  confidence is false precision; we use diff audit + human review instead).
- No Ebbinghaus decay curves. The diff log IS the supersession mechanism; old
  facts stay unless explicitly superseded.
- No conflict-as-research. When two rules conflict, the latest wins
  (recency-weighted), with the conflict logged in the diff.
- No real-time improvement measurement (downstream A/B evaluation, post-MVP).

## 3. Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    survey-agent runtime                         │
│                                                                 │
│   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│   │ orchestrator │  │ site adapters│  │ CAPTCHA      │          │
│   │              │  │ (branded,    │  │ pipeline     │          │
│   │              │  │  prime, ...) │  │              │          │
│   └──────┬───────┘  └──────┬───────┘  └──────┬───────┘          │
│          │                 │                 │                  │
│          └─────────────────┼─────────────────┘                  │
│                            │                                    │
│                            ▼                                    │
│                   ┌───────────────────┐                         │
│                   │  brain.record()   │  ← single ingest API    │
│                   └─────────┬─────────┘                         │
│                             │                                   │
│              ┌──────────────┼──────────────┐                    │
│              ▼              ▼              ▼                    │
│       ┌──────────┐   ┌────────────┐  ┌──────────┐              │
│       │ short    │   │ medium     │  │ long     │              │
│       │ term     │   │ term       │  │ term     │              │
│       │ .jsonl   │   │ brain.db   │  │ rules.   │              │
│       │ (append) │   │ (SQLite)   │  │ yaml     │              │
│       └──────────┘   └──────┬─────┘  └──────────┘              │
│                             │              ▲                   │
│                             ▼              │                   │
│                    ┌─────────────────┐      │                   │
│                    │ brain.query()   │──────┘                   │
│                    └─────────────────┘                          │
│                                                                 │
│   ┌─────────────────────────────────────────────────────┐      │
│   │  Autonomous background trigger (no CLI opt-in):      │      │
│   │  time-based / volume-based / promotion-based.        │      │
│   │  Reads promotions + recent events → LLM digest →     │      │
│   │  writes rules.yaml + persona_hints.json +            │      │
│   │  allowlisted fields in profile.json.                 │      │
│   └─────────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────────┘
```

### 3.1 Storage Tiers

**Short-term: `data/brain/events.jsonl`**

- Append-only, one JSON object per line.
- Cheap to write, easy to grep, easy to ship.
- Never queried by runtime code (only by the offline digest).
- Concurrent writes: each session opens with `O_APPEND`. POSIX guarantees
  atomic writes for payloads under `PIPE_BUF` (4 KiB on Linux, 512 B on
  macOS); serialized events are kept under 1 KiB, so concurrent appends are
  safe by construction. If a serialized event ever exceeds the platform's
  `PIPE_BUF`, it falls back to a per-session `events.<session_id>.jsonl`
  which is merged into the main log at session end.

**Medium-term: `data/brain/brain.db` (SQLite, WAL mode)**

- Indexed for runtime queries.
- Contains:
  - `events(event_id, site, event_type, context_hash, created_at, payload_json)`
  - `promotions(event_id, status, promoted_at)` — pending digest
  - `digest_runs(run_id, started_at, finished_at, trigger, status, summary_json)`
- Read paths: `brain.query_recent(site, event_type, limit)`,
  `brain.query_rules(site, context_hash)`.

**Long-term: `data/brain/rules.yaml` + `data/brain/persona_hints.json`**

- Human-readable, gitignored (profile-derived data).
- Every change is a diff landing in `data/brain/diffs/<timestamp>.json`.
- Runtime reads rules via `brain.query_rules()`.

### 3.2 Persona Safety Invariant

`profile.json` is the single source of truth for persona answers at runtime. The
brain **may** write to `profile.json`, but only on an allowlist of
soft-preference fields. The denylist of answer-materially-affecting fields is
enforced at write time — if a digest proposal touches a denylist field, the
entire proposal is rejected and logged.

**Allowlist (brain CAN edit):**

- `shopping.preferred_stores`
- `shopping.decision_factors`
- `shopping.online_shopping_frequency`
- `politics.political_views` (label only, not affiliation)
- `technology.used_products` (if present)
- Any field tagged `brain_editable: true` in the profile schema

**Denylist (brain CANNOT edit, ever):**

- `demographics.*` (age, gender, ethnicity, marital*status, zip_code, state,
  city, country, birth*\*)
- `employment.status`, `employment.industry`, `employment.job_title`
- `income.*`
- `education.*`
- `health.*`

The denylist is enforced in code, not in prompts. A field-stripping pass runs
over every digest proposal before any write.

## 4. Components & Data Flow

### 4.1 Module Layout

```
survey_agent/brain/
├── __init__.py          # public API: record(), query_recent(), query_rules()
├── store.py             # FailureStore — JSONL appender + SQLite mirror (WAL mode)
├── schemas.py           # EventRecord, Rule, PersonaHint pydantic models
├── normalize.py         # context hashing, site key normalization
├── promotion.py         # frequency-based promotion trigger
├── query.py             # runtime read paths
└── digest.py            # autonomous LLM digest job
```

### 4.2 Runtime Data Flow

```
survey action → adapter / orchestrator / CAPTCHA pipeline
       │
       ▼
brain.record(event)                            # synchronous, never blocks
       │
       ├─► events.jsonl.append(event_json)     # short-term (one write() call)
       │
       └─► SQLite INSERT INTO events (...)      # medium-term
              │
              └─► SQLite trigger: COUNT(*) for (site, event_type, context_hash)
                     WHERE created_at > now() - 7 days
                     │
                     └─► if count ≥ 3 AND last digest > 1h ago:
                            INSERT INTO promotions (status='pending_digest')
                            ON CONFLICT(event_id) DO NOTHING
                            (re-occurrences update count via trigger but do
                             not re-insert — see Appendix C)
                            enqueue background digest task
       │
       ▼
session start: check time-based + volume-based triggers
       │
       └─► if due → enqueue background digest task
```

Multiple triggers coalesce. Background tasks never block the survey session.

### 4.3 Autonomous Digest Flow

Triggered by **any** of:

1. **Time-based:** every N hours (default 6h), checked on session start
2. **Volume-based:** every M new events since last digest (default 200)
3. **Promotion-based:** when ≥1 entry in `promotions` with
   `status='pending_digest'` AND last digest > 1h ago

Whichever fires first. Configurable via `data/config.yaml`:

- `brain.digest.interval_hours` (default 6)
- `brain.digest.events_per_digest` (default 200)
- `brain.digest.max_per_day` (default 4) — soft cap to control LLM cost

```
digest task fires (background, non-blocking)
       │
       ├─► SELECT promotions WHERE status='pending_digest'
       ├─► SELECT events WHERE created_at > last_digest_run
       ├─► SELECT current rules.yaml + persona_hints.json
       │
       ├─► build structured prompt:
       │     system: "You are the survey-agent brain. Analyze observed
       │              failures and propose (a) new rules and (b) persona
       │              refinements. Never propose changes to demylisted
       │              fields. Output strictly JSON."
       │     user:   JSON of recent events + pending promotions +
       │             existing rules + current persona_hints
       │
       ├─► LLM call → validate against Pydantic DigestResult schema
       │     │
       │     └─► if validation fails: log status=failed, abort run
       │
       ├─► enforce persona allowlist on proposal
       │     │
       │     └─► strip any field in denylist
       │         │
       │         └─► if any denylist field was stripped: log
       │             persona_proposal_rejected_denylist, drop those
       │             fields from the proposal
       │
       ├─► apply writes (each atomic: write to .tmp, fsync, rename):
       │     ├─► rules.yaml.tmp → rename
       │     ├─► persona_hints.json.tmp → rename
       │     └─► if allowlist survived: apply to profile.json atomically
       │
       ├─► write data/brain/diffs/<timestamp>.json (before/after all 3 files)
       │
       └─► append data/brain/digest_runs.jsonl with:
             run_id, trigger, started_at, finished_at, status,
             rules_added, persona_fields_changed, persona_fields_rejected,
             rationale_summary, error (if any)
```

Crash safety: atomic writes + WAL SQLite. A crash mid-digest either fully
applies or doesn't, never partial.

### 4.4 Public API

```python
from survey_agent.brain import brain

# Ingest (synchronous, never raises to caller)
brain.record(
    site="branded_surveys",
    event_type="captcha_failed",
    context={"challenge_type": "iframe_grid", "iframe_url": "..."},
    outcome={"solver": "vision_llm", "error": "timeout after 30s"},
)

# Query (used by site adapters and orchestrator)
recent = brain.query_recent(site="branded_surveys", event_type="captcha_solved", limit=5)
rules = brain.query_rules(site="branded_surveys", context_hash="...")
```

`brain.record()` is wrapped in try/except — failures log a warning but never
crash the survey action.

### 4.5 Operator Surface (Emergency Use Only — Does Not Gate Autonomy)

```
survey-agent brain status          # last N digest runs, current state
survey-agent brain pause           # stops auto-triggers
survey-agent brain resume          # re-enables auto-triggers
survey-agent brain inspect <run_id># rationale + diff for one run
survey-agent brain rollback <ts>   # revert profile.json + rules.yaml + hints
```

These exist so you can intervene if the brain misbehaves, but the default flow
is fully autonomous.

## 5. Error Handling

| Failure mode                             | Behavior                                                       |
| ---------------------------------------- | -------------------------------------------------------------- |
| `brain.record()` raises                  | Caught, log warning, survey continues                          |
| SQLite locked / corrupt                  | Fall back to JSONL-only writes; startup warning                |
| LLM API down / timeout                   | Digest status=failed, logged, retried on next trigger          |
| LLM returns malformed JSON               | Schema validation fails; digest status=failed, nothing written |
| Denylist field proposed                  | Field stripped, logged, rest of proposal applied               |
| Crash mid-digest                         | Atomic write guarantees either all-applied or no-change        |
| JSONL partial-line write                 | Impossible by construction: one `write()` call per event       |
| `rules.yaml` malformed after manual edit | Log + ignore malformed entry, query returns rest               |
| `max_per_day` exceeded                   | Skip digest, log status=skipped_rate_limit                     |

## 6. Testing Strategy

| Test file                            | What it covers                                                                    |
| ------------------------------------ | --------------------------------------------------------------------------------- |
| `tests/brain/test_store.py`          | append/mirror roundtrip, concurrent writes, SQLite WAL behavior                   |
| `tests/brain/test_schemas.py`        | Pydantic validation, denylist enforcement on synthetic proposals                  |
| `tests/brain/test_promotion.py`      | frequency threshold (≥3 in 7d), dedup by context_hash                             |
| `tests/brain/test_query.py`          | runtime read paths with seeded fixtures                                           |
| `tests/brain/test_digest.py`         | fake LLM provider, full digest roundtrip, atomic write assertion                  |
| `tests/brain/test_autonomy.py`       | trigger logic (time/volume/promotion), coalescing, non-blocking                   |
| `tests/brain/test_persona_safety.py` | **CRITICAL:** 100 adversarial LLM outputs, assert NO denylist field ever modified |

## 7. Success Criteria

1. **Capture works** — every survey action can emit an event via
   `brain.record()`; the event lands in both JSONL and SQLite.
2. **Promotion works** — repeated failure signatures (≥3 in 7d, same context
   hash) get flagged in `promotions` automatically.
3. **Digest works end-to-end** — autonomous digest produces: (a) new rules in
   `rules.yaml`, (b) valid proposals in `persona_hints.json`, (c) at least one
   denylist-blocked proposal correctly rejected, (d) diff in
   `data/brain/diffs/`, (e) entry in `digest_runs.jsonl`.
4. **Persona safety invariant** — across 100 simulated adversarial digest runs,
   no denylist field is modified in `profile.json`.
5. **Runtime query latency** — `query_recent()` and `query_rules()` < 50ms p95
   on a brain with 10K events.
6. **Non-blocking guarantee** — survey session never waits on a digest;
   in-flight digest adds ≤ 10ms p95 overhead to survey actions.
7. **Crash safety** — a crash mid-digest leaves the brain in a consistent state.

## 8. Out of MVP (Future Sub-Projects)

These ride on top of this foundation in later specs, not part of this slice:

1. **Vision CAPTCHA solver** — uses `brain.query_recent()` to decide when to
   escalate to a vision model for the iframe challenge on Branded Surveys.
2. **Branded Surveys MVP** — first site to consume brain-driven rules; unblocks
   paid surveys.

## 9. Risks & Mitigations

| Risk                                       | Mitigation                                                                                           |
| ------------------------------------------ | ---------------------------------------------------------------------------------------------------- |
| LLM cost creep (digest fires too often)    | Configurable triggers, `max_per_day` soft cap, defaults are conservative (6h, 200 events, 4/day)     |
| Persona drift via soft preferences         | `profile.json` versioned, every change in `diffs/`, allowlist explicitly scoped                      |
| SQLite lock contention                     | WAL mode (`PRAGMA journal_mode=WAL`), digest reads from implicit snapshot                            |
| JSONL corruption on crash mid-write        | One `write()` call per event (atomic at OS level for small payloads)                                 |
| LLM corrupts rules with hallucinated facts | Diffs are auditable; rollback is always available; rules are append-only with UUIDs, never overwrite |
| Brain degrades survey latency              | Background task + small fixed overhead; metrics logged per run                                       |

## 10. References (Pattern Sources, Not Implementations)

These inspired the pattern. We borrow structure, not code:

- **Karpathy autoresearch / llm-wiki-v2** (Rohit G00's fork) — multi-tier
  memory + offline consolidation + diff audit
- **OB1 (Nate B. Jones)** — proven shape of "infrastructure layer for thinking";
  too heavy for MVP but validates the approach
- **gbrain (Garry Tan)** — production brain pattern with hybrid search +
  cron-driven enrichment; validates the dream-cycle model
- **Ghost's critique of llm-wiki-v2** — informed our decision to skip numeric
  confidence scores and decay curves in favor of explicit diff audit

---

## Appendix A: Brain Event Schema (Working)

```python
class EventRecord(BaseModel):
    event_id: str           # UUID v4
    site: str               # e.g., "branded_surveys"
    event_type: str         # e.g., "captcha_failed", "survey_completed", "disqualified"
    context_hash: str       # sha256 of normalized context dict
    context: dict           # free-form, e.g., {challenge_type, iframe_url, screenshot_path}
    outcome: dict           # e.g., {solver, error, latency_ms}
    session_id: str         # orchestrator session id (for dedup)
    created_at: str         # ISO 8601 UTC timestamp
```

## Appendix B: Rule Schema (Working)

```yaml
- rule_id: 7f3a... # UUID v4
  site: branded_surveys
  applies_when:
    event_type: captcha_failed
    context_hash: <sha>
  recommends:
    action: switch_captcha_solver
    args: { solver: 2captcha_api } # or "manual_vnc", etc.
  confidence_evidence: 5 # count of supporting events
  created_at: 2026-07-16T14:30:22Z
  source_digest_run: <run_id>
  status: active # active | superseded | retracted
  supersedes: null # or prior rule_id
```

Rules are append-only. Supersession is explicit (status=superseded,
supersedes=<id>). Nothing is deleted.

---

## Appendix C: Promotion Dedup & Concurrency Semantics

**Promotion dedup.** The `promotions` table has a unique constraint on
`(site, event_type, context_hash, status='pending_digest')` — only one
pending-digest entry per failure signature exists at a time. Re-occurrences
(4th, 5th, ... instance of the same signature) increment a counter via a
SQLite trigger but do NOT create new `promotions` rows. The digest reads
the underlying `events` table to count the true population.

**Concurrency model.**

- `events.jsonl` writes: per-session `O_APPEND` (see §3.1).
- `events` SQLite writes: serialized via a single connection per session; the
  store uses `PRAGMA journal_mode=WAL` to allow concurrent reads (digest)
  while writes are serialized.
- `promotions` writes: single-process, serialized. Multi-process safety is
  out of scope for MVP (one survey-agent process at a time).
- `rules.yaml`, `persona_hints.json`, `profile.json`: written only by the
  digest task, which acquires a file lock (`data/brain/digest.lock`) to
  prevent two digests running concurrently.

**Why this is safe.** The only writers are (a) `record()` calls from one
process, (b) the digest task. They never overlap on the same file under the
stated locking rules. The audit trail (diffs + digest_runs.jsonl) makes any
deviation visible post-hoc.
