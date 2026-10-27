# Survey Agent — System Definition

_**This file is the living source of truth for the design.** The interactive
atlas is built from the same data._

_Question status: **2 open · 2 resolved**._

## One paragraph

Survey Agent is an autonomous bot that logs into paid survey platforms,
discovers available surveys, answers questions consistently using a user-defined
profile, handles CAPTCHAs, and manages withdrawals. It runs headlessly with
anti-detection measures that make automated interactions indistinguishable from
human behavior.

## Decisions locked

| Axis               | Decision                                                                                                 | ADR                                             |
| ------------------ | -------------------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| Browser backend    | Pluggable backends: browser-use (default), browser-harness, camoufox, cloakbrowser, ego-lite, playwright |                                                 |
| Credential storage | macOS Keychain via keyring — never plaintext files                                                       |                                                 |
| Ask-once rule      | Every escalated question persisted; same text never triggers second owner text                           | [0002](../adr/0002-ask-once-text-escalation.md) |
| CAPTCHA strategy   | 4-tier pipeline: arithmetic → LLM vision → external API → VNC fallback                                   |                                                 |
| Brain autonomy     | Autonomous digest triggers on interval; non-blocking background thread                                   |                                                 |

## Cost model

## Reading order (the atlas chapters)

1. **You and the agent** — Strip everything away and this is the system: you
   type a command, the agent runs. _(adds CLI, ORC)_
2. **The survey loop** — Each site gets its own session that loops: login,
   discover, complete, verify. _(adds SES)_
3. **Browser automation** — Six pluggable backends, each with its own stealth
   strategy. _(adds BRW, FPR, HBV)_
4. **Answering questions** — Every question is classified, matched to your
   profile, and answered consistently. _(adds ANS, PRF)_
5. **Handling CAPTCHAs** — Four-tier solver — try cheap first, escalate only
   when needed. _(adds CAP)_
6. **Memory & learning** — The brain remembers every event and learns from
   patterns. _(adds BRN)_
7. **The whole system** — Everything at once, for free exploration.

## Structures

### The main loop

#### CLI · Command line

**In one line.** The entry point — `survey-agent run`, `profile`, `credentials`.

**What it does.** A Typer CLI that parses commands and options, then delegates
to the orchestrator. Supports running surveys, managing profiles, storing
credentials, and checking status.

**How it's built.** `cli.py` defines the Typer app with subcommands. `run` calls
`SessionOrchestrator.run()`. `profile setup` walks through 10 categories
interactively. `credentials set` stores in macOS Keychain.

**Steps in execution.**

1. **Parse args** — Typer parses CLI arguments and options.
2. **Load config** — Read data/config.yaml via AppConfig.
3. **Delegate** — Call orchestrator, profile store, or credential store.
4. **Output** — Rich tables and status display.

#### ORC · Orchestrator

**In one line.** Manages session lifecycle, daemon mode, and site routing.

**What it does.** The SessionOrchestrator spins up N browser backends, creates
per-site SurveySession instances, and runs them. In daemon mode, it cycles
surveys with human-like pauses between runs, prevents macOS sleep, and tracks
total earnings.

**How it's built.** `orchestrator.py` contains `SessionOrchestrator` and
`SurveySession`. Uses `asyncio` for concurrency. Calls `create_backend()` to
instantiate the configured browser backend. Fires brain autonomy triggers in
background threads.

**Steps in execution.**

1. **Load config** — Read AppConfig from YAML.
2. **Create backend** — Instantiate browser backend via create_backend().
3. **Start session** — Create SurveySession with backend, site, profile.
4. **Run loop** — Login → discover → complete → verify → repeat.
5. **Daemon pause** — Sleep 15-45 min between cycles.

#### SES · Survey session

**In one line.** Per-site loop: login, discover surveys, complete, verify
points.

**What it does.** Each SurveySession handles one site. It logs in, checks
initial balance, discovers available surveys, completes them one by one,
classifies outcomes (completed/screened out/broken), and verifies points were
actually earned.

**How it's built.** `SurveySession.run()` in `orchestrator.py`. Calls backend
methods: `login()`, `discover_surveys()`, `complete_survey()`,
`check_balance()`. Uses `classify_outcome()` to determine survey result. Records
events to brain.

**Steps in execution.**

1. **Login** — Authenticate with stored credentials.
2. **Check balance** — Read initial points for delta tracking.
3. **Discover** — Scan dashboard for available surveys.
4. **Complete** — Answer all questions in the survey.
5. **Classify** — Determine outcome: completed, screened out, broken.
6. **Verify** — Confirm points delta matches expectation.

### Browser automation

#### BRW · Browser backend

**In one line.** Pluggable browser engine — six backends for different stealth
levels.

**What it does.** The browser abstraction layer with six swappable backends.
browser-use uses LLM + Playwright (default). browser-harness drives real Chrome
via CDP. camoufox uses stealth Firefox. cloakbrowser passes 30/30 bot detection
tests. ego-lite and playwright are simpler alternatives.

**How it's built.** `browser/__init__.py` exports `create_backend()` factory.
Each backend implements `SurveyAgentBackend` protocol: `start()`, `stop()`,
`login()`, `discover_surveys()`, `complete_survey()`, `check_balance()`.

**Steps in execution.**

1. **Factory** — create_backend() selects implementation.
2. **Start** — Launch browser with fingerprint.
3. **Navigate** — Login, discover, complete surveys.
4. **Stop** — Close browser, clean up.

**Questions.**

- ~~**Q-BRW1** Which backend is production-ready?~~ ✓ browser-use is default;
  cloakbrowser passes 30/30 bot detection tests.

#### FPR · Fingerprint

**In one line.** Per-session identity rotation to avoid bot detection.

**What it does.** Generates random but internally consistent browser
fingerprints: viewport, user-agent, locale, timezone, WebGL renderer, hardware
concurrency. Ensures consistency (Windows UA gets Windows platform).

**How it's built.** `fingerprint.py` contains `FingerprintGenerator`.
`session_identity.py` creates one fingerprint per session. New session = new
identity.

**Steps in execution.**

1. **Generate** — Create random but consistent fingerprint.
2. **Apply** — Set viewport, UA, locale, timezone on browser.
3. **Rotate** — New session gets new fingerprint.

#### HBV · Human behavior

**In one line.** Simulates realistic interaction timing and patterns.

**What it does.** Adds character-by-character typing with varying speed,
occasional "thinking" pauses, pre/post-click delays, natural scrolling, and
reading pauses proportional to text length. A fatigue factor increases delays
over time.

**How it's built.** `human_behavior.py` provides delay functions and interaction
simulators. Used by all backends to make interactions look human.

**Steps in execution.**

1. **Type** — Character-by-character with speed variation.
2. **Click** — Pre-delay, click, post-delay.
3. **Scroll** — Natural scroll patterns.
4. **Pause** — Reading time proportional to text length.

### Memory & learning

#### PRF · Profile

**In one line.** The persistent user persona — 10 categories, ~40 fields.

**What it does.** A JSON profile defining demographics, employment, income,
education, health, shopping, politics, household, technology, and lifestyle.
Every survey question is answered consistently using this persona.

**How it's built.** `profile/store.py` manages JSON with versioned history.
`profile/resolver.py` maps questions to profile fields via keyword dictionary.
`profile/interview.py` walks through setup interactively.

**Steps in execution.**

1. **Load** — Read profile.json from data/.
2. **Match** — Map question text to profile field via keywords.
3. **Resolve** — Find best option match (exact, substring, edit distance).
4. **Return** — Provide consistent answer.

#### ANS · Answering pipeline

**In one line.** Four-stage question processor: detect, classify, match,
generate.

**What it does.** Every survey question passes through four stages: (1)
attention check detection, (2) question type classification (radio, checkbox,
scale, open-ended), (3) profile-to-question matching, (4) open-ended answer
generation via LLM.

**How it's built.** `answering/question_classifier.py` determines type.
`answering/matcher.py` maps to profile. `answering/form_filler.py` interacts
with DOM. `answering/open_ended.py` generates text via LLM.
`answering/attention_check.py` detects quality checks.

**Steps in execution.**

1. **Detect attention** — Check if question is a quality check.
2. **Classify type** — Determine radio/checkbox/scale/text/etc.
3. **Match profile** — Map question to profile field.
4. **Fill form** — Interact with DOM elements.
5. **Generate text** — LLM generates open-ended answers.

#### CAP · CAPTCHA solver

**In one line.** Four-tier CAPTCHA pipeline — try cheap first, escalate.

**What it does.** Tiered solver: (1) arithmetic for simple math, (2) LLM vision
for image challenges, (3) external API for reCAPTCHA/hCAPTCHA, (4) VNC manual
fallback. Each tier implements CaptchaTier interface.

**How it's built.** `captcha/solver.py` chains tiers. ArithmeticSolver handles
"10 - 3 = ?". LLMVisionSolver screenshots and sends to vision model.
APICaptchaSolver calls 2captcha/CapSolver. ManualVNCFallback alerts user.

**Steps in execution.**

1. **Arithmetic** — Solve simple math challenges.
2. **LLM vision** — Screenshot → vision model → extract answer.
3. **API call** — Send to 2captcha/CapSolver service.
4. **VNC fallback** — Alert user for manual solving.

#### BRN · Brain

**In one line.** Event memory, ask-once escalation, autonomous digest.

**What it does.** Records every survey event (login, completion, failure,
CAPTCHA). Implements the ask-once rule: before texting the owner, checks if this
exact question was already answered. Promotes patterns to rules after repeated
occurrences.

**How it's built.** `brain/__init__.py` exports `record()`, `lookup()`,
`absorb()`. `brain/store.py` manages FailureStore. `brain/autonomy.py` triggers
digest on interval. `brain/promotion.py` promotes events to rules.

**Steps in execution.**

1. **Record** — Log event with site, type, context, outcome.
2. **Promote** — If pattern repeats 3+ times, create rule.
3. **Digest** — Autonomous analysis on 6-hour interval.
4. **Lookup** — Check if owner already answered this question.

**Questions.**

- **Q-BRN1** What triggers the autonomous digest? → _Brain autonomy deep dive_

### Supporting infrastructure

#### CRD · Credentials

**In one line.** Secure storage via macOS Keychain — never plaintext.

**What it does.** Stores site login credentials in macOS Keychain using Python
keyring. Service name pattern: survey-agent/{site_slug}. Tracks last-used
username per site.

**How it's built.** `credentials.py` uses `keyring` library.
`CredentialStore.retrieve()` reads from Keychain. `store()` writes with service
name `survey-agent/{site}`.

**Steps in execution.**

1. **Store** — Write username/password to Keychain.
2. **Retrieve** — Read credentials for site.
3. **Check** — Verify credentials exist.

#### CFG · Config

**In one line.** Pydantic models + YAML persistence for all settings.

**What it does.** AppConfig root model with ScheduleConfig, AccountConfig,
BrainConfig sub-models. Covers LLM provider settings, browser backend selection,
payout thresholds, notification preferences, and per-site overrides.

**How it's built.** `config.py` defines Pydantic models. `load_config()` reads
from `data/config.yaml`, creates defaults if missing. `save_config()` persists.

**Steps in execution.**

1. **Load** — Read YAML into Pydantic models.
2. **Validate** — Pydantic validates all fields.
3. **Save** — Persist back to YAML.

### Not yet switched on

#### WDR · Withdrawal _(not switched on)_

**In one line.** Later: automatic PayPal cashout at balance thresholds.

**What it does.** Currently text-notify-only. Future: auto-withdraw when balance
crosses threshold. Three modes: auto, accumulate, manual.

**How it's built.** `withdrawal.py` exists but is not wired into the main loop.
Will use PayPal API.

**Steps in execution.**

1. **Check balance** — Read current points.
2. **Threshold** — Compare against configured threshold.
3. **Withdraw** — Initiate PayPal cashout.
4. **Notify** — Text owner confirmation.

**Questions.**

- **Q-WDR1** Withdrawal automation scope?

#### SCH · Scheduler _(not switched on)_

**In one line.** Later: cron-based scheduling with wake/sleep times.

**What it does.** Scheduled execution windows (8am-11pm), lunch breaks,
heartbeat monitoring. Currently handled by daemon mode with manual start.

**How it's built.** ScheduleConfig exists in config.py but no scheduler
implementation yet.

**Steps in execution.**

1. **Wake** — Start at configured wake time.
2. **Run** — Execute survey cycles.
3. **Break** — Pause during lunch.
4. **Sleep** — Stop at configured sleep time.

**Questions.**

- **Q-SCH1** Scheduler implementation approach?

## Flows (representative packets)

Payload shapes are what the design implies, not measured traffic.

### One survey

| #   | From → To | Packet           | Representative payload                           |
| --- | --------- | ---------------- | ------------------------------------------------ |
| 1   | CLI → ORC | run command      | `{"site":"branded-surveys","max":5}`             |
| 2   | ORC → SES | create session   | `{"backend":"browser-use","profile":"loaded"}`   |
| 3   | SES → BRW | login            | `{"url":"https://surveys.gobranded.com"}`        |
| 4   | SES → BRW | discover surveys | `{"dashboard":true}`                             |
| 5   | SES → ANS | answer question  | `{"type":"radio","question":"How old are you?"}` |
| 6   | ANS → PRF | lookup           | `{"field":"demographics.age","value":"35"}`      |
| 7   | SES → CAP | captcha detected | `{"type":"image","tier":1}`                      |
| 8   | SES → BRN | record outcome   | `{"event":"survey_completed","points":150}`      |

### Ask-once escalation

| #   | From → To | Packet            | Representative payload                        |
| --- | --------- | ----------------- | --------------------------------------------- |
| 1   | CAP → BRN | lookup question   | `{"question":"What is your favorite color?"}` |
| 2   | BRN → CAP | no match          | `{"found":false}`                             |
| 3   | CAP → ORC | escalate to owner | `{"method":"imessage","question":"..."}`      |
| 4   | ORC → CLI | owner replies     | `{"answer":"Blue"}`                           |
| 5   | CLI → BRN | absorb answer     | `{"question":"...","answer":"Blue"}`          |

### Daemon cycle

| #   | From → To | Packet         | Representative payload         |
| --- | --------- | -------------- | ------------------------------ |
| 1   | ORC → SES | cycle start    | `{"cycle":1,"surveys":8}`      |
| 2   | SES → BRW | run surveys    | `{"count":8}`                  |
| 3   | SES → ORC | cycle complete | `{"completed":6,"points":900}` |
| 4   | ORC → ORC | pause          | `{"minutes":30}`               |

## Questions — index

Reference by ID. ✓ resolved (with date) · otherwise open.

- ~~**Q-BRW1**~~ (BRW) ✓ browser-use is default; cloakbrowser passes 30/30 bot
  detection tests.
- **Q-BRN1** (BRN) What triggers the autonomous digest?
- **Q-WDR1** (WDR) Withdrawal automation scope?
- **Q-SCH1** (SCH) Scheduler implementation approach?

## What the platform gives vs what we own

**Platform gives:** Playwright for browser automation, macOS Keychain for
credential storage, Pydantic for config validation, Typer for CLI.

**We own:** Survey site adapters, answering pipeline, CAPTCHA solving tiers,
brain/memory system, fingerprint rotation, human behavior simulation.

## Planned filesystem

```
survey-agent/
├── survey_agent/
│   ├── cli.py                  # Typer CLI entry point
│   ├── config.py               # Pydantic config + YAML
│   ├── orchestrator.py         # Session orchestrator + daemon
│   ├── credentials.py          # macOS Keychain store
│   ├── browser/                # Pluggable browser backends
│   ├── brain/                  # Memory + autonomous digest
│   ├── answering/              # Question pipeline
│   ├── captcha/                # Tiered CAPTCHA solver
│   ├── profile/                # User persona management
│   └── sites/                  # Survey site adapters
├── data/                       # Config, profile, brain state
└── docs/                       # ADRs, atlas, research
```

## How this file is maintained

Generated from `docs/survey-agent/atlas/data.mjs` by
`node docs/survey-agent/atlas/build.mjs`, which also builds the interactive
atlas (`atlas.html`). Edit the data file, rebuild, republish — never edit this
file by hand.
