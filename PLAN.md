# survey-agent

Autonomous agent that logs into paid survey sites, qualifies, answers
consistently as the profile owner, solves CAPTCHAs, and accumulates points
toward PayPal cashout.

## Run the whole loop from one command {#loop}

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
- [ ] Wire survey_agent/scheduler.py into the loop (module written, imported
      nowhere) {#loop-scheduler}
  - from: roadmap
  - tech: survey_agent/scheduler.py (236 lines, zero callers)

## Drive a real browser without getting flagged {#backends}

tech: survey_agent/browser/backend.py (ABC) + pluggable backends files:
[survey_agent/browser/**, survey_agent/cookie_sync.py] links: [captcha,
answering, adapters]

- [x] Chrome-harness backend proven end-to-end on Branded Surveys
      {#backends-harness}
  - tech: survey_agent/browser/harness_backend.py (2810 lines); git a63e1b1
    "100% fifth time"
- [x] Playwright + Camoufox stealth backends on pinned compatible versions
      {#backends-playwright}
  - tech: survey_agent/browser/playwright_backend.py, camoufox_backend.py;
    pyproject pins playwright==1.51.0 for Camoufox compat
- [x] Cross-origin iframe handling and human-like interaction
      {#backends-iframes}
  - tech: survey_agent/browser/iframe_manager.py, human_behavior.py; git a5bdb54
    "iframe scanning"
- [x] Google session reuse for OAuth-only sites {#backends-cookies}
  - tech: survey_agent/cookie_sync.py sync_google_cookies(), used by
    harness_backend.py:25
- [ ] Prove ego-lite and CloakBrowser backends past page snapshot limits
      {#backends-exotic}
  - tech: survey_agent/browser/ego_backend.py, cloakbrowser_backend.py;
    docs/CLOAKBROWSER_FINAL_STATUS.md; ego snapshotText() returns "root" on
    external providers (unresolved)

## Answer questions as the profile owner would {#answering}

tech: survey_agent/answering/, survey_agent/profile/ files:
[survey_agent/answering/**, survey_agent/profile/**, data/profile.json,
tests/answering/**, tests/profile/**] needs: [] links: [adapters, cua, loop]

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
  - tech: flagged HIGH-value in docs/REVIVAL_PLAN.md A1; _get_answers_from_llm
    inconsistent

## Beat CAPTCHAs and bot traps {#captcha}

tech: survey_agent/captcha/solver.py tiered pipeline + vision reading files:
[survey_agent/captcha/**, scripts/test_captcha_vision.py,
scripts/read_image_text.py] links: [backends, cua]

- [x] Read text/image CAPTCHAs via vision model {#captcha-read}
  - tech: survey_agent/captcha/solver.py (CaptchaType enum incl.
    TEXT_CHALLENGE); git a8ce6e8 char-by-char typing fix
- [x] Detect and decline bot-trap questions instead of answering them
      {#captcha-traps}
  - tech: unified detection in harness_backend.py + llm_answers.py; git 989a16c,
    aab7831 (catches selects too)

## Put an adapter on every target site {#adapters}

tech: survey_agent/sites/ auto-discovered via registry.py files:
[survey_agent/sites/**, tests/sites/**] needs: [backends] links: [answering,
payouts]

- [x] Adapter base contract + slug registry with auto-discovery
      {#adapters-registry}
  - tech: survey_agent/sites/base.py, registry.py (pkgutil scan + @register)
- [x] Branded Surveys: full discover→qualify→answer→complete flow
      {#adapters-branded}
  - tech: survey_agent/sites/branded_surveys.py (448 lines); 8 completions in
    one run per docs/SCALING_STATUS.md
- [x] Prime Opinion adapter implementation {#adapters-prime}
  - tech: survey_agent/sites/prime_opinion.py (475 lines, full question loop);
    login depends on #backends-cookies
- [x] Thin adapters registered for six secondary sites {#adapters-thin}
  - tech: qmee.py, surveypop.py, eurekasurveys.py, attapoll.py, yougov.py,
    surveyspin.py (~80-100 lines each; login+balance only, no question loop)
- [ ] Add Prolific adapter — highest $/hr, API-based study discovery
      {#adapters-prolific}
  - from: roadmap
  - tech: planned in docs/REVIVAL_PLAN.md B1 ($9-12/hr, REST API); no
    prolific.py exists yet
- [ ] Promote thin adapters to full question-answering loops {#adapters-full}
  - from: roadmap
  - tech: pattern per docs/REVIVAL_PLAN.md B4/B5

## Let the brain learn from failures on its own {#brain}

tech: survey_agent/brain/ — events → promote → digest → research → fix files:
[survey_agent/brain/**,
docs/superpowers/specs/2026-07-16-brain-foundation-design.md,
docs/superpowers/specs/2026-07-18-brain-autoresearch-design.md, tests/brain/**]
links: [loop]

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
  - tech: survey_agent/brain/fixer.py execute_fix() with TraceEntry ledger;
    digest.py:262 save_proposal()
- [ ] Verify closed-loop fixes actually raise success rates before promotion
      {#brain-verify}
  - from: roadmap
  - tech: spec Phase 4
    (docs/superpowers/specs/2026-07-18-brain-autoresearch-design.md); no
    evidence of verified auto-fixes in git log

## Run the OS-level CUA agent through hostile surveys {#cua}

tech: computer-use driven agent lineage, currently survey_agent/survey_v13.py
files: [scripts/cua_agent*.py, scripts/cua_survey_agent.py,
survey_agent/survey_v13.py, survey_agent/survey_continuous.py,
survey_agent/topsurveys_*.js, data/survey_run.log] needs: [answering] links:
[captcha]

- [x] CUA agent iterations v1→v5 with profile-driven living memory
      {#cua-lineage}
  - tech: scripts/cua_agent_v2.py…v5.py (~4,200 lines); v5 uses ProfileStore +
    answer-consistency tracking
- [x] Pass screeners and qualify on provider funnels (PureSpectrum, Fiftyfive5,
      SSISurveys) {#cua-qualify}
  - tech: survey_agent/survey_v13.py; git 9e0ee26/a63e1b1 (5× perfect screener
    runs), df2d972, 702236c
- [x] Live run loop with rejection detection and tab cleanup {#cua-live}
  - tech: data/survey_run.log (last activity 2026-07-24); git aab7831
- [ ] Complete full surveys end-to-end unattended (still cycling at mid-survey
      walls) {#cua-complete}
  - from: agent
  - tech: survey_v13 answer_survey(); log tail shows repeated modal-dismiss loop
    ("i can't answer this question")

## Cash out earnings to PayPal {#payouts}

tech: survey_agent/withdrawal.py + Gmail IMAP verification files:
[survey_agent/withdrawal.py, survey_agent/email/verification.py,
survey_agent/credentials.py] links: [adapters, loop]

- [x] Read email verification codes for 2FA-gated actions {#payouts-email}
  - tech: survey_agent/email/verification.py; wired into
    cli.py/orchestrator.py/harness_backend.py
- [x] Withdrawal manager with threshold/mode config {#payouts-manager}
  - tech: survey_agent/withdrawal.py
    WithdrawalManager.check_and_withdraw()/force_withdraw()
- [ ] Trigger automatic cashout during runs (manager written, called nowhere)
      {#payouts-wire}
  - from: roadmap
  - tech: WithdrawalManager has zero callers outside withdrawal.py; README
    promises withdrawal management

## decisions

- Backfilled PLAN.md from scratch on 2026-08-24; prior file was the untouched
  scaffold stub ("First task").
- Trust order followed: code layout first, git log second (86 commits since
  2026-07-18, latest work = survey_v13 bot-trap line), decision/in-flight docs
  third (REVIVAL_PLAN, SCALING_STATUS, SCALING_PLAN, PHASE logs, superpowers
  specs), README last (read headers only; its architecture prose was not used to
  tick anything).
- Every [x] above cites its implementing source on the tech: line. Items I could
  not verify in code stay [ ]: scheduler.py and withdrawal.py are written but
  have zero callers; Prolific adapter does not exist; ego/CloakBrowser backends
  exist as code but no commit or log shows a completed run through them.
- [~] none: nothing was in progress by me at write time. The only from: agent
  item (#cua-complete) reflects the in-flight survey_v13 workstream visible in
  the last ~15 commits and the live data/survey_run.log.
- Component count held at 8; the topsurveys JS automation line and the
  Playwright script family were folded into {#cua} and {#backends} respectively
  rather than getting their own cards.
