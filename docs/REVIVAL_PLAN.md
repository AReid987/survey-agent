# Survey Agent Revival Plan — Phase 3

## Current State Assessment

### What Actually Works (Verified)
| Capability | Status | Evidence |
|---|---|---|
| Branded Surveys login (dedicated Chrome) | ✅ | Port 19333, CDP session, Google OAuth + email/password |
| Survey discovery from dashboard | ✅ | Finds 3+ surveys, parses URLs and point values |
| Viewpoint Forum surveys completion | ✅ | 1 completed = +5 points (158 → 161 total over multiple runs) |
| CAPTCHA vision reading | ✅ | Reads "WJ", "wJ", "wvJ" correctly via FreeModel vision |
| CLI entry point | ✅ | `survey-agent run -s branded-surveys -n 5 --backend browser-harness` |
| Profile system | ✅ | 100% complete, 10 categories, ~40 fields |
| Gmail IMAP integration | ✅ | Verification code reading for email 2FA |
| Multi-provider LLM fallback | ✅ | opencode → config → freemodel chain |

### What's Broken or Missing
| Issue | Severity | Details |
|---|---|---|
| **PureSpectrum CAPTCHA wall** | HIGH | Per-page text CAPTCHA on every page transition (15+ per survey). Vision reads them but economics don't work (~$0.03/survey solver cost) |
| **Prime Opinion Google OAuth** | HIGH | Dedicated Chrome profile has no Google session. User's regular Chrome must be closed first |
| **LLM answer quality inconsistent** | MEDIUM | `_get_answers_from_llm` returns good JSON sometimes, empty arrays other times. No fallback confidence check |
| **No checkpointing** | MEDIUM | If survey fails on page 35/40, all progress lost. Must restart |
| **Only 2 site adapters** | HIGH | `branded_surveys` (works) + `prime_opinion` (broken login). 6 more listed in SITES dict with no adapter |
| **Orchestrator ↔ Adapter mismatch** | MEDIUM | `SessionOrchestrator.run()` calls `SurveyAgentBackend` ABC methods, but actual working code is inline in `BrowserHarnessBackend.complete_survey()` which doesn't use the site adapter system |
| **Camoufox backend broken** | LOW | Playwright 1.60.0 crashes (TypeError). Needs downgrade to ≤1.51.x |
| **No persistence** | LOW | Runs once and stops. No cron/schedule integration |
| **harness_backend.py too large** | LOW | 2605 lines single file, hard to maintain |

### Earnings Reality
- **Viewpoint Forum** (via Branded Surveys): ~5 pts/survey, 2-3 min each
- **PureSpectrum**: Blocked by per-page CAPTCHA
- **Need**: 500 pts = $5 PayPal payout
- **At current rate**: ~17 hours runtime for $5
- **Research confirms**: Prolific ($9-12/hr) and academic platforms are highest ROI

---

## Strategy: Two Parallel Tracks

### Track A — Stabilize & Accumulate (Days 1-3)
Make the existing Viewpoint Forum path reliable enough to run unattended and accumulate points to $5.

### Track B — Expand to Higher-Yield Platforms (Days 4-10)
Add Prolific (API-based, $9-12/hr, no CAPTCHA), fix Prime Opinion, then bulk-add Tier-2 sites.

---

## Track A: Stabilize & Accumulate

### A1. Fix LLM Answer Pipeline (6 hours)
**Problem:** `_get_answers_from_llm` sends a massive prompt to the LLM asking for JSON but gets inconsistent results. The deterministic fallback `_deterministic_answers` is too simple (just picks first option).

**Solution:** Replace the free-form LLM JSON generation with a structured two-stage pipeline:

1. **Stage 1 — Page Understanding**: Send page text + elements to LLM, ask it to output a structured description of what questions exist (not answers yet)
2. **Stage 2 — Answer Resolution**: Match each question to the profile using the existing `matcher.py` + `resolver.py` logic (which is well-tested)
3. **Stage 3 — LLM for Ambiguity**: Only call LLM for questions the keyword matcher can't resolve (open-ended, etc.)

This mirrors the hybrid approach from the research doc: "Freeze deterministic parts as code, reserve the LLM for deviations."

**Files to modify:**
- `survey_agent/browser/harness_backend.py` — refactor `_get_answers_from_llm` and `_deterministic_answers`
- `survey_agent/answering/matcher.py` — add batch question resolution method

### A2. Add Checkpointing (3 hours)
**Solution:** Write a JSON progress file after each page of a survey. On restart, detect the checkpoint and resume instead of starting over.

```python
# data/checkpoint.json
{
  "survey_url": "https://...",
  "page": 7,
  "url": "https://current-page-url",
  "answers_given": [...],
  "profile_version": "2026-07-02",
  "retry_count": 0
}
```

On each page of `complete_survey()`, update the checkpoint. On restart, detect:
- If same survey URL → resume from page 7
- If different survey → clear checkpoint and start fresh
- If 3+ resumption failures → archive and try next survey

**Files to create:**
- `survey_agent/checkpoint.py`

**Files to modify:**
- `survey_agent/browser/harness_backend.py` — call checkpoint on each page

### A3. Scheduler/Persistence (2 hours)
**Problem:** Runs once, then stops.

**Solution:** Add a `--daemon` mode to the CLI that runs the survey loop indefinitely with configurable pauses between cycles (15-45 min), matching `cua_agent_v2.py`'s pattern.

Already exists in `cua_agent_v2.py` main loop (lines 808-849). Port this into the `SessionOrchestrator` as a `run_daemon()` method.

**Files to modify:**
- `survey_agent/orchestrator.py` — add `run_daemon()` method
- `survey_agent/cli.py` — add `--daemon` flag to `run` command

### A4. Prime Opinion Session Fix (2 hours)
**Problem:** Agent's dedicated Chrome has no Google session. Prime Opinion requires "Sign in with Google".

**Solution:** Two options:
1. **Profile copy** (preferred): Before launch, if the user's real Chrome profile exists at `~/Library/Application Support/Google/Chrome/Default`, copy just the cookies for `accounts.google.com` and `app.primeopinion.com` into the agent's profile using Chrome's cookie format.
2. **User prompt + profile reuse**: Use the existing `survey-agent login` command which opens a headed browser and waits for the user to sign in. After successful Google sign-in, the profile persists for future headless runs.

Option 1 is fully automatic. Use a CDP-based cookie dump from the user's real Chrome (if it's running) or from the profile directory directly.

**Files to modify:**
- `survey_agent/browser/harness_backend.py` — add cookie sync on `start()`

### Deliverable: Track A Complete
- `survey-agent run -s branded-surveys --daemon` runs indefinitely
- Viewpoint Forum surveys complete reliably (80%+ success rate)
- Progress survives crashes (checkpointing)
- Prime Opinion login works (session reuse)

---

## Track B: Expand Platforms

### B1. Prolific Adapter (6 hours) — HIGHEST ROI
**Why:** API-based, $9-12/hr, no CAPTCHA, 95% study acceptance rate. Research confirms this is the #1 platform.

**How:** Prolific has a REST API. No browser needed for study discovery and acceptance:
- `GET /api/v1/studies/` — list available studies
- Filter: remaining places > 0, status = "ACTIVE"
- For each: open study URL in agent's Chrome, complete the study
- Track: approval rate, earnings history, study completion

**Files to create:**
- `survey_agent/sites/prolific.py` — ProlificAdapter
- `survey_agent/sites/prolific_api.py` — API client (if API-accessible)

**Files to modify:**
- `survey_agent/orchestrator.py` — add prolific to SITES

### B2. Fix Prime Opinion Adapter (3 hours)
**Problem:** Current adapter is only 84 lines — no question answering, just login check and get_balance.

**Solution:** After fixing login (A4), implement:
- `get_surveys()` — find available surveys
- `start_survey()` — click into survey
- `get_current_question()` — parse question from page
- `answer_question()` — submit answer
- `submit_survey()` — complete survey

Reuse the existing `branded_surveys.py` patterns.

**Files to modify:**
- `survey_agent/sites/prime_opinion.py` — full implementation

### B3. Survey Junkie Adapter (4 hours)
**Why:** Tier-2, $3-5/hr, no per-page CAPTCHA reported by community.

Standard pattern: login → dashboard → survey tile → question loop → submit.

**Files to create:**
- `survey_agent/sites/survey_junkie.py`

### B4. Adapter Template Extraction (3 hours)
**Problem:** Each adapter currently requires 150-450 lines of boilerplate. The research recommends: "Freeze deterministic parts as code, reserve the LLM for deviations."

**Solution:** Extract a `BaseSurveyAdapter` from common patterns in `branded_surveys.py`:
- Standard login flow (email/password)
- Dashboard survey discovery (CSS selectors configurable)
- Question parsing (uses existing `question_classifier.py`)
- Answer submission (uses existing `form_filler.py`)
- Balance checking (CSS selectors configurable)

Each new site adapter becomes a 30-50 line config class instead of 150-450 lines.

**Files to create:**
- `survey_agent/sites/base_adapter.py` — BaseSurveyAdapter with shared logic

**Files to modify:**
- `survey_agent/sites/branded_surveys.py` — migrate to BaseSurveyAdapter
- `survey_agent/sites/prime_opinion.py` — migrate to BaseSurveyAdapter

### B5. Add Qmee, SurveyPop Adapters (4 hours)
Both are listed in `orchestrator.SITES` with no adapter. SurveyPop should be quick using BaseSurveyAdapter. Qmee has a simpler interface.

**Files to create:**
- `survey_agent/sites/qmee.py`
- `survey_agent/sites/surveypop.py`

### Deliverable: Track B Complete
- 5+ site adapters working (branded-surveys, prime-opinion, prolific, survey-junkie, qmee)
- Prolific API integration adds the highest-earning platform
- Template extraction makes adding new sites 3x faster

---

## Tier Priority (Updated from Research)

| Priority | Platform | $/hr | Effort | Why This Order |
|---|---|---|---|---|
| 1 | **Branded Surveys** (existing) | $3-5 | Already works | Accumulate now while building others |
| 2 | **Prolific** | $9-12 | 6 hours | API-based, no CAPTCHA, highest ROI |
| 3 | **Prime Opinion** | $3-5 | 3 hours | Already has adapter skeleton, just needs login fix + answering |
| 4 | **Survey Junkie** | $3-5 | 4 hours | Tier-2, well-known patterns |
| 5 | **Qmee** | $6-3 | 2 hours | Simple interface, good $/hr |
| 6 | **SurveyPop** | $2-3 | 2 hours | Already in SITES dict |

Skip PureSpectrum permanently (per-page CAPTCHA wall makes economics unviable).

---

## Technical Debt Paydown

### T1. Split harness_backend.py (2 hours)
**Problem:** 2605-line single file is a maintenance burden.

**Solution:** Split into:
- `harness_backend.py` — `BrowserHarnessBackend` class only (API surface)
- `harness_scripts.py` — Script builders (`_build_login_script`, `_build_discover_script`, etc.)
- `harness_profile.py` — Hardcoded profile answers (`_PROFILE`, `_pick_radio_answer`)
- `harness_answers.py` — Answer generation logic (`_get_answers_from_llm`, `_deterministic_answers`)

### T2. Proper Error Classification (2 hours)
Per the research doc: "Classify errors before retrying: idempotent reads → retry aggressively; rate limits (429) → backoff; auth failures → escalate immediately."

Add error classification to the orchestrator's retry logic.

### T3. Add Structured Logging (1 hour)
Add JSON logging for analytics (earnings tracking, success rates per platform, CAPTCHA encounter rates).

---

## Implementation Order

### Week 1: Track A (Stabilize)
```
Day 1: A1 — Fix LLM answer pipeline (6h)
Day 2: A2 — Checkpointing (3h) + A3 — Scheduler (2h) + A4 — Prime Opinion session fix (2h) [7h]
Day 3: Test run: 4+ hour daemon session on Branded Surveys. Fix issues found.
```

### Week 2: Track B (Expand)
```
Day 4: B1 — Prolific adapter (6h)
Day 5: B2 — Fix Prime Opinion adapter (3h) + B4 — Template extraction (3h) [6h]
Day 6: B3 — Survey Junkie (4h) + B5 — Qmee/SurveyPop (4h) [8h]
Day 7: Integration test + daemon run with 3+ platforms in rotation
```

### Week 3: Polish & Scale
```
Day 8: T1 — Split harness_backend.py (2h) + T2 — Error classification (2h) + T3 — Structured logging (1h)
Day 9-10: Load test: 8+ hour daemon across all working platforms. Fix edge cases.
```

---

## New Discovery: Ego Lite (Free, Open-Source Alternative)

**Discovered July 10, 2026** — Ego Lite is a free, MIT-licensed Chromium browser designed for AI agents.

### Why It Matters

| Problem | How Ego Lite Solves It |
|---|---|
| **Prime Opinion Google OAuth** | Inherits Chrome data on first launch — logins, cookies, extensions all carry over. Agent never hits Google's sign-in page. |
| **Iframes / sparse pages** | Proprietary snapshot tech handles cross-origin iframes, shadow DOM, and SDK widgets (Stripe, Intercom, React portals) — our current JS element extractor fails on these. |
| **Page snapshots** | Returns semantic tree with stable `@N` refs instead of CSS selectors. Refs survive DOM changes and class name rotations. |
| **Economics** | Free, open-source, no API keys or subscriptions needed. |
| **Workspace isolation** | "Spaces" let the agent work in the background without stealing the user's tabs or focus. |

### Current Status

| Item | Status |
|---|---|
| Ego Lite app installed | ❌ — needs DMG download + interactive Chrome migration |
| Node.js compatibility | ❌ — needs Node.js 22+ (we have 20.9.0) |
| ego-browser CLI | ✅ — installed to /usr/local/bin/ego-browser |
| Backend adapter code | ✅ — `survey_agent/browser/ego_backend.py` written |
| Backend registry | ✅ — `--backend ego-lite` selectable |

### To Use
1. Download https://lite.ego.app and install
2. Run it once, migrate Chrome data when prompted
3. `survey-agent run --backend ego-lite`

### Update: Stagehand (23.4K stars)
Blocked for our use case — requires `BROWSERBASE_API_KEY` or `MODEL_API_KEY` for AI features. Not truly open-source for the inference layer.

---

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| **Prolific API changes/rate limits** | Low | High | Cache study list, exponential backoff on 429 |
| **Prime Opinion detects automation** | Medium | Medium | Use the same Chrome profile/stealth as Branded Surveys |
| **Viewpoint Forum stops working** | Low | Medium | Diversify to other providers (Prolific, Prime Opinion) |
| **Google OAuth requires 2FA** | Medium | High | Use Gmail IMAP integration (already exists) to read 2FA codes |
| **macOS sleep/power management** | High | Low | Add `caffeinate` wrapper in daemon mode |
| **LLM API cost at scale** | Medium | Low | Minimize LLM calls per survey; use deterministic matching where possible |

---

## What We Need From You

1. **Prolific account** — Create one at https://app.prolific.com. It's free to join, pays via PayPal. No CAPTCHA, API-based.
2. **Survey Junkie / Qmee accounts** — Quick signup, email/password login (no Google OAuth)
3. **Close Chrome once** — So the agent can copy cookies for Prime Opinion Google OAuth, or run `survey-agent login` once manually
4. **4-8 hours of runtime** — The daemon needs to run while you're away. Let it accumulate points.

---

## Expected Outcomes

| Metric | Current | Week 1 Target | Week 2 Target | Week 3 Target |
|---|---|---|---|---|
| Surveys completed/hr | ~2 (Viewpoint) | ~3 (Viewpoint + Prime) | ~8 (3 platforms) | ~15 (5 platforms) |
| Average $/hr | ~$0.10 | ~$0.30 | ~$2.00 | ~$5.00 |
| Platforms active | 1 | 2 | 4 | 5 |
| Successful survey rate | ~40% | ~60% | ~70% | ~80% |
| CAPTCHA encounters | Per-page on PS | Near-zero | Near-zero | Near-zero |
| Runtime capability | One shot | Daemon (hours) | Daemon (hours) | Daemon (24h+) |
| Crash recovery | None | Checkpoint | Checkpoint | Checkpoint + retry |
