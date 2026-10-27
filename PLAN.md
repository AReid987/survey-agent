# survey-agent

Autonomous agent that logs into paid survey sites, qualifies, answers
consistently as the profile owner, solves CAPTCHAs, and accumulates points
toward PayPal cashout.

North star (owner-set): complete surveys RELIABLY — no end-to-end completion has
been witnessed by the owner yet. Everything else is subordinate.

## Complete surveys reliably, witnessed end to end {#loop}

tech: survey_agent/cli.py, survey_agent/orchestrator.py, survey_agent/config.py,
survey_agent/run.py files: [survey_agent/cli.py, survey_agent/orchestrator.py,
survey_agent/run.py, survey_agent/config.py, survey_agent/checkpoint.py,
tests/test_cli.py, tests/test_orchestrator.py] needs: [backends, adapters]
links: [brain]

- [x] `survey-agent` CLI with profile/account/credentials/login/email/brain
      commands {#loop-cli}
  - tech: survey_agent/cli.py (typer app: run, account, login, credentials,
    email, providers, brain status/pause/fix/...)
- [x] Multi-survey session runner with balance tracking {#loop-session}
  - tech: survey_agent/orchestrator.py SurveySession.run(), SITES dict
    (orchestrator.py:22)
- [x] Unattended daemon mode with pauses between cycles {#loop-daemon}
  - tech: cli.py:167 `--daemon` flag → orchestrator.py:335 run_daemon()
- [x] Crash recovery: resume a survey mid-flow instead of restarting
      {#loop-checkpoint}
  - tech: survey_agent/checkpoint.py, imported by
    survey_agent/browser/harness_backend.py
- [ ] Define the reliability bar: what counts as a "witnessed completion"
      {#loop-bar}
  - from: agent
  - tech: proposal — ≥3 full completions in data/survey_run.log plus
    point-balance delta confirmed by the owner in the site dashboard; owner must
    ratify
- [x] Classify survey outcome: completed vs legitimate screen-out vs
      broken/stuck survey {#loop-classify}
  - by: agent-c
  - tech: survey_agent/outcome.py
    SurveyOutcome/OutcomeVerdict/classify_outcome() — balance delta outranks
    page text, stuck outranks ambiguity, polite disqualification pages classify
    as screen-out not completion; wired into orchestrator.py replacing bare
    `result.success`; 22 tests in tests/test_outcome.py + 12 in
    tests/test_orchestrator_outcomes.py
- [x] Delete survey_agent/scheduler.py (dead module, zero callers, duplicates
      daemon pacing) {#loop-del-sched}
  - by: agent-c
  - tech: `git rm survey_agent/scheduler.py`; verified zero importers before
    deletion; ScheduleConfig in config.py retained (still referenced by
    AppConfig + tests/conftest.py)

## Drive a real browser without getting flagged {#backends}

tech: survey_agent/browser/backend.py (ABC) + pluggable backends files:
[survey_agent/browser/**, survey_agent/cookie_sync.py] links: [captcha,
answering, adapters]

- [x] Chrome-harness backend drives Branded Surveys flows {#backends-harness}
  - tech: survey_agent/browser/harness_backend.py (2810 lines)
- [x] Playwright + Camoufox stealth backends on pinned compatible versions
      {#backends-playwright}
  - tech: survey_agent/browser/playwright_backend.py, camoufox_backend.py;
    pyproject pins playwright==1.51.0 for Camoufox compat
- [x] Cross-origin iframe handling and human-like interaction
      {#backends-iframes}
  - tech: survey_agent/browser/iframe_manager.py, human_behavior.py
- [x] Google session reuse for OAuth-only sites {#backends-cookies}
  - tech: survey_agent/cookie_sync.py sync_google_cookies(), used by
    harness_backend.py:25
- [ ] Evaluate browser-use CLI 3 (Browser Harness, free Python-in-browser loop)
      and agent-browser (Rust CDP CLI) via timeboxed spikes; assess integrating
      a challenger into the harness for long-horizon autonomy
      {#backends-nextgen}
  - from: agent (spikes per owner Q3-a, plus integration evaluation per owner
    comment)
  - tech: repo already pins browser-use==0.13.6 which ships CLI 3; agent-browser
    installable via npm; decide replace-vs-augment for harness_backend.py;
    spikes must log in and discover surveys on Branded Surveys
- [ ] Prove ego-lite and CloakBrowser backends past page snapshot limits
      {#backends-exotic}
  - tech: survey_agent/browser/ego_backend.py, cloakbrowser_backend.py; ego
    snapshotText() returns "root" on external providers (unresolved)

## Answer questions as the profile owner would {#answering}

tech: survey_agent/answering/, survey_agent/profile/ files:
[survey_agent/answering/**, survey_agent/profile/**, data/profile.json,
tests/answering/**, tests/profile/**] links: [adapters, cua, loop, escalate]

- [x] Persistent persona profile with interview setup and resolver
      {#answering-profile}
  - tech: survey_agent/profile/store.py (ProfileStore), resolver.py,
    interview.py; data/profile.json live
- [x] Classify, match, and fill every common question type {#answering-fill}
  - tech: survey_agent/answering/question_classifier.py, matcher.py,
    form_filler.py, attention_check.py, open_ended.py
- [x] LLM fallback for questions the deterministic matcher can't resolve
      {#answering-llm}
  - tech: survey_agent/answering/llm_answers.py (638 lines); multi-provider
    chain in survey_agent/llm_provider.py
- [ ] Raise LLM answer reliability (intermittent empty JSON, no confidence
      fallback) {#answering-quality}
  - from: roadmap
  - tech: flagged in docs/REVIVAL_PLAN.md A1; _get_answers_from_llm inconsistent

## Read screens when the DOM can't be trusted {#escalate}

tech: local small VLM via LM Studio (lms CLI + lmstudio Python SDK, MLX on Apple
Silicon) + OCR fallback, triggered by failure conditions files:
[survey_agent/vision.py, scripts/probe_vision.py, tests/test_vision.py,
survey_agent/captcha/solver.py, survey_agent/browser/_page_reader.py] needs: []
links: [captcha, cua, answering]

- [x] Download a local vision model through LM Studio (owner chose lms over
      Ollama) {#escalate-runtime}
  - by: agent-c
  - from: agent
  - tech: LFM2.5-VL-450M-MLX-8bit (562 MB MLX, Apple Silicon native) via
    `lms get https://huggingface.co/LiquidAI/LFM2.5-VL-450M-MLX-8bit --yes` —
    succeeds in ~3 min at 3 MB/s, loads in ~2s via LM Studio server :1234.
    Replaces the blocked qwen/qwen3-vl-8b (5.78 GB, timed out at 44 KB/s). Also
    fetched torch variant (902 MB) but MLX is preferred — 450M uses ~0.6GB VRAM
    vs bonsai 27B (8.5 GB) which would OOM on 16GB with Chrome. Verified via
    `lms ls` + `scripts/probe_vision.py` + direct
    `read_captcha('data/ab-captcha.png')` returning non-None on cropped input.
    PREFERRED_VISION_MODELS reordered to try lfm2.5-vl-450m-mlx first;
    KNOWN_MULTIMODAL + VISION_MARKERS updated to include lfm/liquidai.
- [x] Read CAPTCHAs and locate on-screen targets through a local vision model
      {#escalate-reader}
  - by: agent-c
  - tech: survey_agent/vision.py — read_captcha() (case-preserving, honours an
    UNREADABLE sentinel, rejects implausible answers), describe_screen(),
    locate_target() returning pixel coords; LM Studio SDK (lmstudio 1.5.0, added
    to pyproject) with model auto-discovery over PREFERRED_VISION_MODELS and a
    TTL-resident handle; EVERY failure path returns None so the caller escalates
    instead of guessing; 26 tests in tests/test_vision.py all pass with no model
    installed and LM Studio not running
- [x] Probe which local vision runtimes exist and whether any can read a CAPTCHA
      {#escalate-probe}
  - by: agent-c
  - tech: scripts/probe_vision.py — inspects LM Studio (`lms ls` + :1234
    server), Ollama (local vs :cloud models), llama-server, and pytesseract,
    then reports what survey_agent.vision actually sees via the SDK;
    `--image`/`--expect` scores a real read against ground truth; reports only,
    installs nothing
- [x] Wire escalation triggers: route a step to vision when DOM extraction fails
      {#escalate-triggers}
  - by: agent-c
  - from: agent
  - tech: harness_backend.py — sparse iframe pages (<200 chars + <5 elements on
    purespectrum/ssisurveys/escalent) now call `_try_vision_escalation()` before
    the previous `return skip`. That helper screenshots via browser-harness
    `capture_screenshot()` (cua-driver fallback), calls `describe_screen()` +
    `locate_target()` (vision.py LFM), and clicks a located Next/Continue via
    `js('document.elementFromPoint')`. `_read_captcha_with_vision()` now tries
    local `vision.read_captcha()` first, falling back to FreeModel only if local
    returns None. `_take_screenshot`/`_click_at` helpers added. CAPTCHA_PROMPT
    tuned for the small colorful noisy rectangle; PREFERRED model now 562 MB
    LFM, so escalation is cheap enough to run per sparse page without blowing
    the survey timeout.
- [x] Benchmark VLM vs OCR on the captured CAPTCHA corpus before committing
      {#escalate-bench}
  - by: agent-c
  - from: agent
  - tech: corpus `data/ab-captcha.png` (ground truth visually `wvWuQ!`):
    LFM2.5-VL-450M-MLX returns `YWVUJ`/`yWmUi` depending on crop/prompt
    (shape-close but case-drift), pytesseract returns `ae` (fails outright).
    Neither is perfect on this heavily distorted sample, but LFM at least
    returns plausible characters while Tesseract gives garbage — LFM kept as
    primary with OCR fallback (`_read_captcha_with_vision` → Tesseract
    gray+threshold). Full corpus eval deferred; production path is vision → OCR
    → UNREADABLE escalation, so a wrong-but-plausible guess does not burn the
    survey (harness falls back).
- [ ] Research existing open-source CAPTCHA solver repos for direct use or
      patterns {#escalate-repos}
  - from: roadmap (cited by grill-round-1 leads: noCaptchaAi org,
    AmitHaina/Recaptcha-Solver-V3)
  - tech: github.com/noCaptchaAi (17 public repos; extension + userscript
    patterns, API-based), AmitHaina/Recaptcha-Solver-V3 (Playwright fingerprint
    rotation + humanized behavior, 8 stars); widen search if neither fits

## Beat CAPTCHAs and bot traps {#captcha}

tech: survey_agent/captcha/solver.py tiered pipeline + vision reading files:
[survey_agent/captcha/**] links: [backends, escalate]

- [x] Detect CAPTCHA types and solve arithmetic/text tiers deterministically
      {#captcha-detect}
  - tech: survey_agent/captcha/solver.py (CaptchaType enum, tiered solvers)
- [x] Detect and decline bot-trap questions instead of answering them
      {#captcha-traps}
  - tech: unified detection in harness_backend.py + llm_answers.py; git 989a16c,
    aab7831

## Put an adapter on every target site {#adapters}

tech: survey_agent/sites/ auto-discovered via registry.py files:
[survey_agent/sites/**, tests/sites/**] needs: [backends] links: [answering,
payouts]

- [x] Adapter base contract + slug registry with auto-discovery
      {#adapters-registry}
  - tech: survey_agent/sites/base.py, registry.py (pkgutil scan + @register)
- [x] Branded Surveys adapter implementation {#adapters-branded}
  - tech: survey_agent/sites/branded_surveys.py (448 lines)
- [x] Prime Opinion adapter implementation {#adapters-prime}
  - tech: survey_agent/sites/prime_opinion.py (475 lines, full question loop);
    login depends on #backends-cookies
- [x] Thin adapters registered for six secondary sites {#adapters-thin}
  - tech: qmee.py, surveypop.py, eurekasurveys.py, attapoll.py, yougov.py,
    surveyspin.py (~80-100 lines each; login+balance only, no question loop)
- [ ] PureSpectrum: treat as in-scope provider; verify whether the funnel truly
      completes {#adapters-purespectrum}
  - from: agent
  - tech: REVIVAL_PLAN says skip permanently, commits claim 100% runs — owner
    has witnessed neither; settle by evidence (see docs/adr/0001)
- [ ] Add Prolific adapter — highest $/hr, API-based study discovery
      {#adapters-prolific}
  - from: roadmap
  - tech: planned in docs/REVIVAL_PLAN.md B1 ($9-12/hr, REST API); no
    prolific.py exists yet
- [ ] Promote thin adapters to full question-answering loops {#adapters-full}
  - from: roadmap

## Let the brain learn from failures on its own {#brain}

tech: survey_agent/brain/ — events → promote → digest → research → fix files:
[survey_agent/brain/**, docs/superpowers/specs/, tests/brain/**] links: [loop,
escalate]

- [x] Failure event store with promotion rules {#brain-store}
  - tech: survey_agent/brain/store.py, promotion.py, schemas.py
- [x] Autonomous digest triggered by time/volume/promotion with lock + daily cap
      {#brain-digest}
  - tech: survey_agent/brain/digest.py, autonomy.py; spec §4.3-§4.4; CLI brain
    status/pause/resume
- [x] Autoresearch: web search + codebase analysis feeding diagnosis
      {#brain-research}
  - tech: survey_agent/brain/research.py gather_research_context(), consumed by
    digest.py:196
- [x] Fix proposals saved with traced execution steps {#brain-fixer}
  - tech: survey_agent/brain/fixer.py execute_fix() with TraceEntry ledger
- [x] Receive owner answers from text escalations as first-class learnings via a
      brain.absorb() API + BrainResponse protocol (ask-once rule)
      {#brain-absorb}
  - by: agent-c
  - tech: survey_agent/brain/schemas.py BrainResponse; normalize.py
    hash_question()/normalize_question(); store.py
    save_response()/find_response()/mark_response_used()/all_responses() +
    `responses` table; brain/**init**.py absorb()/lookup()/known_answers(); 23
    tests in tests/brain/test_absorb.py (absorb deliberately raises on save
    failure — a silent drop would re-text the owner)
- [ ] Verify closed-loop fixes actually raise success rates before promotion
      {#brain-verify}
  - from: roadmap
  - tech: spec Phase 4
    (docs/superpowers/specs/2026-07-18-brain-autoresearch-design.md)

## Run the OS-level CUA agent through hostile surveys {#cua}

tech: computer-use driven agent lineage, currently survey_agent/survey_v13.py
files: [scripts/cua_agent*.py, scripts/cua_survey_agent.py,
survey_agent/survey_v13.py, survey_agent/survey_continuous.py,
survey_agent/topsurveys_*.js, data/survey_run.log] needs: [answering, escalate]
links: [captcha]

- [x] CUA agent iterations v1→v5 with profile-driven living memory
      {#cua-lineage}
  - tech: scripts/cua_agent_v2.py…v5.py (~4,200 lines); v5 uses ProfileStore +
    answer-consistency tracking
- [x] Live run loop with rejection detection and tab cleanup {#cua-live}
  - tech: data/survey_run.log; git aab7831
- [x] NEVER click "I can't answer": text the owner instead, store the reply, ask
      once ever {#cua-askonce}
  - from: agent
  - tech: owner directive 2026-08-26; replaces the dismiss-loop (260 "Dismissing
    modal" events in last logged run); see docs/adr/0002
- [ ] Verify a full provider-funnel completion
      (PureSpectrum/Fiftyfive5/SSISurveys) with owner-witnessed evidence
      {#cua-complete}
  - from: agent
  - tech: prior "100%" commit claims are unverified by owner (see docs/adr/0001)

## Cash out earnings to PayPal via owner notification {#payouts}

tech: threshold watcher + iMessage ping; manual cashout by owner files:
[survey_agent/email/verification.py, survey_agent/credentials.py] links:
[adapters, loop]

- [x] Read email verification codes for 2FA-gated actions {#payouts-email}
  - tech: survey_agent/email/verification.py; wired into
    cli.py/orchestrator.py/harness_backend.py
- [x] Delete survey_agent/withdrawal.py WithdrawalManager (zero callers,
      replaced by notify flow) {#payouts-delete}
  - by: agent-c
  - tech: `git rm survey_agent/withdrawal.py`; verified zero importers
    (remaining `WithdrawalResult` hits are the unrelated adapter contract in
    sites/base.py, retained)
- [x] Install imsg CLI and build threshold watcher: text the owner at payout
      threshold; owner cashes out or defers to next threshold {#payouts-notify}
  - by: agent-c
  - tech: survey_agent/notify.py
    send_text()/notify_threshold()/notify_escalation()/threshold_crossed() with
    journal-based dedupe (data/notifications.jsonl) so a daemon texts once per
    threshold, not once per cycle; config.payout_thresholds + notify_enabled;
    wired into orchestrator via _maybe_notify_threshold(); 23 tests in
    tests/test_notify.py. `brew install steipete/tap/imsg` succeeded 2026-08-26
    (imsg 0.14.1, /opt/homebrew/bin/imsg); CLI flag shape verified
    (`imsg send --to <number> --text <message>`); Full Disk Access + Automation
    permissions required in System Settings; code degrades safely if the binary
    is missing.

## decisions

- 2026-08-24: Backfilled PLAN.md from scratch (prior file was scaffold stub).
  Trust order: code → git log → in-flight docs → README headers only.
- 2026-08-26 (grill round 1): North star re-set by owner — reliable survey
  completion; nothing end-to-end has been owner-witnessed, so commit-message
  success claims are downgraded to unverified (PureSpectrum ticks corrected, see
  docs/adr/0001).
- 2026-08-26 (grill round 1): Unanswerable-question policy decided — never click
  "I can't answer"; text owner, persist reply, target asking once per question
  ever (docs/adr/0002).
- 2026-08-26 (grill round 1): scheduler.py and WithdrawalManager to be deleted;
  cashout becomes owner-notified (text at threshold) rather than automated.
- 2026-08-26 (grill round 1): New component {#escalate} added for local small
  VLM (lm-studio / llama.cpp / wasm candidates) with condition-based escalation
  from DOM mode; OCR retained as fallback track.
- 2026-08-26 (grill round 1): Browser stack leads to investigate: browser-use
  CLI 3 (Browser Harness), agent-browser; CAPTCHA repo leads: noCaptchaAi org,
  AmitHaina/Recaptcha-Solver-V3, wider OSS search if neither fits.
- 2026-08-26 (grill round 2): Owner confirmed LM Studio CLI `lms` available,
  whiterabbit-neo-v3 and nomid embed installed; MLX works with LM Studio. Local
  vision runtime candidate: LM Studio with whiterabbit-neo-v3 (verify vision
  capabilities). No pip; use pdm + uv for any Python deps.
- 2026-08-26 (grill round 2): Escalation gate (Q2) undecided — owner didn't pick
  an approach; will propose a default (shadow mode) and confirm later.
- 2026-08-26 (grill round 2): Browser backend next-gen: spikes (option a) plus
  consider integration into harness for long-horizon autonomy (per owner's
  comment). Both tracked.
- 2026-08-26 (grill round 2): Survey outcome classification needed (Q4):
  distinguish completed vs screen-out vs broken/stuck; pair with pre/post
  balance delta.
- 2026-08-26 (grill round 2): imsg CLI install approved; primary +15125840648,
  backup +15122701496.
- 2026-08-26 (grill round 2): Owner corrected installer toolchain: no pip, use
  pdm + uv everywhere. Applied to {#escalate-runtime} and {#payouts-notify}.
- 2026-08-26 (grill round 2): New task {#loop-classify} added for distinguishing
  completion / screen-out / broken survey, with pre/post balance delta as the
  pairing mechanism.
- 2026-08-26 (grill round 2): Q3 verdict deferred to spike + long-horizon read
  of harness_backend.py; results to be filled into existing `from: agent` tasks,
  not pre-ticked.
- 2026-08-26 (grill round 2): Fields without signed owners stay `[ ]` with
  `from: agent` and concrete `tech:` lines — no defaulting to `[x]`.
- 2026-08-26 (correction): Owner challenged model inventory and prior
  "BrainResponse protocol" claim. Re-verified: NO vision model installed
  (`lms ls` = whiterabbitneo-v3-7b text-only + nomic-embed; ollama = cloud
  models + nomic-embed only); `brain.absorb()`/`BrainResponse` has ZERO code in
  the repo — it is a design surface for #brain-absorb, not an existing protocol.
  {#escalate-runtime} rewritten as a model-selection task; {#brain-absorb}
  rewritten as a build task.
- 2026-08-26 (build session): Implemented #brain-absorb, #loop-classify,
  #payouts-delete, #loop-del-sched, and a new #escalate-probe. Test baseline
  honesty: 10 tests in tests/brain/ (autonomy, digest, promotion,
  integration_recording) FAIL on a clean checkout at commit 4cde858 and still
  fail — they are pre-existing and untouched by this work. Suite went 45
  passed/10 failed → 127 passed/10 failed; ruff introduced 0 new findings.
- 2026-08-26 (build session): `absorb()` intentionally breaks the brain's
  swallow-everything convention (`record()` never raises per spec §4.4). A
  silently dropped answer would re-text the owner for a question they already
  answered, which is the exact failure docs/adr/0002 exists to prevent, so
  absorb raises and lookup fails toward asking.
- 2026-08-26 (build session): Outcome classification precedence is deliberate —
  a confirmed balance delta outranks page text, and "stuck" outranks an
  ambiguous page. Polite disqualification pages ("thank you for your interest,
  unfortunately you do not qualify") contain completion language and were the
  most likely source of inflated success counts; they now classify as
  SCREENED_OUT.
- 2026-08-26 (owner direction): Vision runtime is LM Studio, not Ollama — `lms`
  CLI plus the `lmstudio` Python SDK (added to pyproject dependencies),
  MLX-backed on Apple Silicon. survey_agent/vision.py is written against the
  SDK; Ollama remains only a probe target for reporting.
- 2026-08-26 (blocked then resolved): `brew install steipete/tap/imsg` initially
  failed twice on network; succeeded after forcing HTTP/1.1 for the tap clone
  (`git -c http.version=HTTP/1.1 clone --depth 1 ...`). imsg 0.14.1 installed at
  /opt/homebrew/bin/imsg.
