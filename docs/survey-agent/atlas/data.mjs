// Single source of truth for the survey-agent atlas.
// Build: node docs/survey-agent/atlas/build.mjs  → writes ../SYSTEM.md and ../atlas.html

export const META = {
  title: 'Survey Agent',
  artifactUrl: '',
  sourcePath: 'docs/survey-agent/atlas/data.mjs',
  buildCmd: 'node docs/survey-agent/atlas/build.mjs',
  stats: [{ k: 'System', v: 'survey-agent · v1' }, { k: 'Backends', v: '6' }, { k: 'Sites', v: '8' }],
  intro: `_**This file is the living source of truth for the design.** The interactive atlas is built from the same data._`,
  onePara: `Survey Agent is an autonomous bot that logs into paid survey platforms, discovers available surveys, answers questions consistently using a user-defined profile, handles CAPTCHAs, and manages withdrawals. It runs headlessly with anti-detection measures that make automated interactions indistinguishable from human behavior.`,
  costModel: [],
  deepDive: '',
  platformGives: 'Playwright for browser automation, macOS Keychain for credential storage, Pydantic for config validation, Typer for CLI.',
  weOwn: 'Survey site adapters, answering pipeline, CAPTCHA solving tiers, brain/memory system, fingerprint rotation, human behavior simulation.',
  filesystem: `survey-agent/
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
└── docs/                       # ADRs, atlas, research`,
};

export const DECISIONS = [
  { axis: 'Browser backend', decision: 'Pluggable backends: browser-use (default), browser-harness, camoufox, cloakbrowser, ego-lite, playwright', adr: '' },
  { axis: 'Credential storage', decision: 'macOS Keychain via keyring — never plaintext files', adr: '' },
  { axis: 'Ask-once rule', decision: 'Every escalated question persisted; same text never triggers second owner text', adr: '[0002](../adr/0002-ask-once-text-escalation.md)' },
  { axis: 'CAPTCHA strategy', decision: '4-tier pipeline: arithmetic → LLM vision → external API → VNC fallback', adr: '' },
  { axis: 'Brain autonomy', decision: 'Autonomous digest triggers on interval; non-blocking background thread', adr: '' },
];

export const GROUPS = [
  { id: 'loop', title: 'The main loop' },
  { id: 'browser', title: 'Browser automation' },
  { id: 'brain', title: 'Memory & learning' },
  { id: 'support', title: 'Supporting infrastructure' },
  { id: 'future', title: 'Not yet switched on' },
];

export const NODES = [
  // ── Main loop ──
  { id: 'CLI', code: 'CLI', name: 'Command line', short: 'CLI', group: 'loop', gx: 1, gy: 8, w: 2.5, d: 2, h: 34, kind: 'screen',
    one: 'The entry point — `survey-agent run`, `profile`, `credentials`.',
    what: 'A Typer CLI that parses commands and options, then delegates to the orchestrator. Supports running surveys, managing profiles, storing credentials, and checking status.',
    how: '<code>cli.py</code> defines the Typer app with subcommands. <code>run</code> calls <code>SessionOrchestrator.run()</code>. <code>profile setup</code> walks through 10 categories interactively. <code>credentials set</code> stores in macOS Keychain.',
    steps: [['Parse args', 'Typer parses CLI arguments and options.'], ['Load config', 'Read data/config.yaml via AppConfig.'], ['Delegate', 'Call orchestrator, profile store, or credential store.'], ['Output', 'Rich tables and status display.']],
    cond: [] },

  { id: 'ORC', code: 'ORC', name: 'Orchestrator', short: 'ORCHESTRATOR', group: 'loop', gx: 6, gy: 5, w: 3, d: 3, h: 64, kind: 'tall',
    one: 'Manages session lifecycle, daemon mode, and site routing.',
    what: 'The SessionOrchestrator spins up N browser backends, creates per-site SurveySession instances, and runs them. In daemon mode, it cycles surveys with human-like pauses between runs, prevents macOS sleep, and tracks total earnings.',
    how: '<code>orchestrator.py</code> contains <code>SessionOrchestrator</code> and <code>SurveySession</code>. Uses <code>asyncio</code> for concurrency. Calls <code>create_backend()</code> to instantiate the configured browser backend. Fires brain autonomy triggers in background threads.',
    steps: [['Load config', 'Read AppConfig from YAML.'], ['Create backend', 'Instantiate browser backend via create_backend().'], ['Start session', 'Create SurveySession with backend, site, profile.'], ['Run loop', 'Login → discover → complete → verify → repeat.'], ['Daemon pause', 'Sleep 15-45 min between cycles.']],
    cond: [] },

  { id: 'SES', code: 'SES', name: 'Survey session', short: 'SESSION', group: 'loop', gx: 11, gy: 5, w: 2.5, d: 2.5, h: 44, kind: 'box',
    one: 'Per-site loop: login, discover surveys, complete, verify points.',
    what: 'Each SurveySession handles one site. It logs in, checks initial balance, discovers available surveys, completes them one by one, classifies outcomes (completed/screened out/broken), and verifies points were actually earned.',
    how: '<code>SurveySession.run()</code> in <code>orchestrator.py</code>. Calls backend methods: <code>login()</code>, <code>discover_surveys()</code>, <code>complete_survey()</code>, <code>check_balance()</code>. Uses <code>classify_outcome()</code> to determine survey result. Records events to brain.',
    steps: [['Login', 'Authenticate with stored credentials.'], ['Check balance', 'Read initial points for delta tracking.'], ['Discover', 'Scan dashboard for available surveys.'], ['Complete', 'Answer all questions in the survey.'], ['Classify', 'Determine outcome: completed, screened out, broken.'], ['Verify', 'Confirm points delta matches expectation.']],
    cond: [] },

  // ── Browser automation ──
  { id: 'BRW', code: 'BRW', name: 'Browser backend', short: 'BROWSER', group: 'browser', gx: 6, gy: 10, w: 3.5, d: 2.5, h: 34, kind: 'slab',
    one: 'Pluggable browser engine — six backends for different stealth levels.',
    what: 'The browser abstraction layer with six swappable backends. browser-use uses LLM + Playwright (default). browser-harness drives real Chrome via CDP. camoufox uses stealth Firefox. cloakbrowser passes 30/30 bot detection tests. ego-lite and playwright are simpler alternatives.',
    how: '<code>browser/__init__.py</code> exports <code>create_backend()</code> factory. Each backend implements <code>SurveyAgentBackend</code> protocol: <code>start()</code>, <code>stop()</code>, <code>login()</code>, <code>discover_surveys()</code>, <code>complete_survey()</code>, <code>check_balance()</code>.',
    steps: [['Factory', 'create_backend() selects implementation.'], ['Start', 'Launch browser with fingerprint.'], ['Navigate', 'Login, discover, complete surveys.'], ['Stop', 'Close browser, clean up.']],
    cond: [{ q: 'Which backend is production-ready?', r: 'browser-use is default; cloakbrowser passes 30/30 bot detection tests.' }] },

  { id: 'FPR', code: 'FPR', name: 'Fingerprint', short: 'FINGERPRINT', group: 'browser', gx: 11, gy: 9, w: 2, d: 2, h: 24, kind: 'store',
    one: 'Per-session identity rotation to avoid bot detection.',
    what: 'Generates random but internally consistent browser fingerprints: viewport, user-agent, locale, timezone, WebGL renderer, hardware concurrency. Ensures consistency (Windows UA gets Windows platform).',
    how: '<code>fingerprint.py</code> contains <code>FingerprintGenerator</code>. <code>session_identity.py</code> creates one fingerprint per session. New session = new identity.',
    steps: [['Generate', 'Create random but consistent fingerprint.'], ['Apply', 'Set viewport, UA, locale, timezone on browser.'], ['Rotate', 'New session gets new fingerprint.']],
    cond: [] },

  { id: 'HBV', code: 'HBV', name: 'Human behavior', short: 'HUMAN', group: 'browser', gx: 14, gy: 9, w: 2, d: 2, h: 24, kind: 'box',
    one: 'Simulates realistic interaction timing and patterns.',
    what: 'Adds character-by-character typing with varying speed, occasional "thinking" pauses, pre/post-click delays, natural scrolling, and reading pauses proportional to text length. A fatigue factor increases delays over time.',
    how: '<code>human_behavior.py</code> provides delay functions and interaction simulators. Used by all backends to make interactions look human.',
    steps: [['Type', 'Character-by-character with speed variation.'], ['Click', 'Pre-delay, click, post-delay.'], ['Scroll', 'Natural scroll patterns.'], ['Pause', 'Reading time proportional to text length.']],
    cond: [] },

  // ── Brain / memory ──
  { id: 'PRF', code: 'PRF', name: 'Profile', short: 'PROFILE', group: 'brain', gx: 1, gy: 2, w: 2.5, d: 2.5, h: 24, kind: 'store',
    one: 'The persistent user persona — 10 categories, ~40 fields.',
    what: 'A JSON profile defining demographics, employment, income, education, health, shopping, politics, household, technology, and lifestyle. Every survey question is answered consistently using this persona.',
    how: '<code>profile/store.py</code> manages JSON with versioned history. <code>profile/resolver.py</code> maps questions to profile fields via keyword dictionary. <code>profile/interview.py</code> walks through setup interactively.',
    steps: [['Load', 'Read profile.json from data/.'], ['Match', 'Map question text to profile field via keywords.'], ['Resolve', 'Find best option match (exact, substring, edit distance).'], ['Return', 'Provide consistent answer.']],
    cond: [] },

  { id: 'ANS', code: 'ANS', name: 'Answering pipeline', short: 'ANSWERING', group: 'brain', gx: 6, gy: 1.5, w: 3, d: 3, h: 44, kind: 'cards',
    one: 'Four-stage question processor: detect, classify, match, generate.',
    what: 'Every survey question passes through four stages: (1) attention check detection, (2) question type classification (radio, checkbox, scale, open-ended), (3) profile-to-question matching, (4) open-ended answer generation via LLM.',
    how: '<code>answering/question_classifier.py</code> determines type. <code>answering/matcher.py</code> maps to profile. <code>answering/form_filler.py</code> interacts with DOM. <code>answering/open_ended.py</code> generates text via LLM. <code>answering/attention_check.py</code> detects quality checks.',
    steps: [['Detect attention', 'Check if question is a quality check.'], ['Classify type', 'Determine radio/checkbox/scale/text/etc.'], ['Match profile', 'Map question to profile field.'], ['Fill form', 'Interact with DOM elements.'], ['Generate text', 'LLM generates open-ended answers.']],
    cond: [] },

  { id: 'CAP', code: 'CAP', name: 'CAPTCHA solver', short: 'CAPTCHA', group: 'brain', gx: 11, gy: 1.5, w: 2.5, d: 2.5, h: 34, kind: 'gate',
    one: 'Four-tier CAPTCHA pipeline — try cheap first, escalate.',
    what: 'Tiered solver: (1) arithmetic for simple math, (2) LLM vision for image challenges, (3) external API for reCAPTCHA/hCAPTCHA, (4) VNC manual fallback. Each tier implements CaptchaTier interface.',
    how: '<code>captcha/solver.py</code> chains tiers. ArithmeticSolver handles "10 - 3 = ?". LLMVisionSolver screenshots and sends to vision model. APICaptchaSolver calls 2captcha/CapSolver. ManualVNCFallback alerts user.',
    steps: [['Arithmetic', 'Solve simple math challenges.'], ['LLM vision', 'Screenshot → vision model → extract answer.'], ['API call', 'Send to 2captcha/CapSolver service.'], ['VNC fallback', 'Alert user for manual solving.']],
    cond: [] },

  { id: 'BRN', code: 'BRN', name: 'Brain', short: 'BRAIN', group: 'brain', gx: 1, gy: 5, w: 2.5, d: 2, h: 34, kind: 'store',
    one: 'Event memory, ask-once escalation, autonomous digest.',
    what: 'Records every survey event (login, completion, failure, CAPTCHA). Implements the ask-once rule: before texting the owner, checks if this exact question was already answered. Promotes patterns to rules after repeated occurrences.',
    how: '<code>brain/__init__.py</code> exports <code>record()</code>, <code>lookup()</code>, <code>absorb()</code>. <code>brain/store.py</code> manages FailureStore. <code>brain/autonomy.py</code> triggers digest on interval. <code>brain/promotion.py</code> promotes events to rules.',
    steps: [['Record', 'Log event with site, type, context, outcome.'], ['Promote', 'If pattern repeats 3+ times, create rule.'], ['Digest', 'Autonomous analysis on 6-hour interval.'], ['Lookup', 'Check if owner already answered this question.']],
    cond: [{ q: 'What triggers the autonomous digest?', to: 'Brain autonomy deep dive' }] },

  // ── Supporting infrastructure ──
  { id: 'CRD', code: 'CRD', name: 'Credentials', short: 'CREDENTIALS', group: 'support', gx: 14, gy: 5, w: 2, d: 2, h: 24, kind: 'store',
    one: 'Secure storage via macOS Keychain — never plaintext.',
    what: 'Stores site login credentials in macOS Keychain using Python keyring. Service name pattern: survey-agent/{site_slug}. Tracks last-used username per site.',
    how: '<code>credentials.py</code> uses <code>keyring</code> library. <code>CredentialStore.retrieve()</code> reads from Keychain. <code>store()</code> writes with service name <code>survey-agent/{site}</code>.',
    steps: [['Store', 'Write username/password to Keychain.'], ['Retrieve', 'Read credentials for site.'], ['Check', 'Verify credentials exist.']],
    cond: [] },

  { id: 'CFG', code: 'CFG', name: 'Config', short: 'CONFIG', group: 'support', gx: 14, gy: 2, w: 2, d: 2, h: 24, kind: 'store',
    one: 'Pydantic models + YAML persistence for all settings.',
    what: 'AppConfig root model with ScheduleConfig, AccountConfig, BrainConfig sub-models. Covers LLM provider settings, browser backend selection, payout thresholds, notification preferences, and per-site overrides.',
    how: '<code>config.py</code> defines Pydantic models. <code>load_config()</code> reads from <code>data/config.yaml</code>, creates defaults if missing. <code>save_config()</code> persists.',
    steps: [['Load', 'Read YAML into Pydantic models.'], ['Validate', 'Pydantic validates all fields.'], ['Save', 'Persist back to YAML.']],
    cond: [] },

  // ── Future (ghosts) ──
  { id: 'WDR', code: 'WDR', name: 'Withdrawal', short: 'WITHDRAW', group: 'future', ghost: true, gx: 11, gy: 11, w: 2.5, d: 2, h: 34,
    one: 'Later: automatic PayPal cashout at balance thresholds.',
    what: 'Currently text-notify-only. Future: auto-withdraw when balance crosses threshold. Three modes: auto, accumulate, manual.',
    how: '<code>withdrawal.py</code> exists but is not wired into the main loop. Will use PayPal API.',
    steps: [['Check balance', 'Read current points.'], ['Threshold', 'Compare against configured threshold.'], ['Withdraw', 'Initiate PayPal cashout.'], ['Notify', 'Text owner confirmation.']],
    cond: ['Withdrawal automation scope?'] },

  { id: 'SCH', code: 'SCH', name: 'Scheduler', short: 'SCHEDULER', group: 'future', ghost: true, gx: 1, gy: 11, w: 2.5, d: 2, h: 34,
    one: 'Later: cron-based scheduling with wake/sleep times.',
    what: 'Scheduled execution windows (8am-11pm), lunch breaks, heartbeat monitoring. Currently handled by daemon mode with manual start.',
    how: 'ScheduleConfig exists in config.py but no scheduler implementation yet.',
    steps: [['Wake', 'Start at configured wake time.'], ['Run', 'Execute survey cycles.'], ['Break', 'Pause during lunch.'], ['Sleep', 'Stop at configured sleep time.']],
    cond: ['Scheduler implementation approach?'] },
];

export const FLOWS = [
  { id: 'survey', name: 'One survey', hops: [
    ['CLI', 'ORC', 'run command', { site: 'branded-surveys', max: 5 }, 'yx'],
    ['ORC', 'SES', 'create session', { backend: 'browser-use', profile: 'loaded' }, 'xy'],
    ['SES', 'BRW', 'login', { url: 'https://surveys.gobranded.com' }, 'yx'],
    ['SES', 'BRW', 'discover surveys', { dashboard: true }, 'xy'],
    ['SES', 'ANS', 'answer question', { type: 'radio', question: 'How old are you?' }, 'yx'],
    ['ANS', 'PRF', 'lookup', { field: 'demographics.age', value: '35' }, 'xy'],
    ['SES', 'CAP', 'captcha detected', { type: 'image', tier: 1 }, 'yx'],
    ['SES', 'BRN', 'record outcome', { event: 'survey_completed', points: 150 }, 'xy'],
  ] },
  { id: 'escalation', name: 'Ask-once escalation', hops: [
    ['CAP', 'BRN', 'lookup question', { question: 'What is your favorite color?' }, 'yx'],
    ['BRN', 'CAP', 'no match', { found: false }, 'xy'],
    ['CAP', 'ORC', 'escalate to owner', { method: 'imessage', question: '...' }, 'yx'],
    ['ORC', 'CLI', 'owner replies', { answer: 'Blue' }, 'xy'],
    ['CLI', 'BRN', 'absorb answer', { question: '...', answer: 'Blue' }, 'yx'],
  ] },
  { id: 'daemon', name: 'Daemon cycle', hops: [
    ['ORC', 'SES', 'cycle start', { cycle: 1, surveys: 8 }, 'yx'],
    ['SES', 'BRW', 'run surveys', { count: 8 }, 'xy'],
    ['SES', 'ORC', 'cycle complete', { completed: 6, points: 900 }, 'yx'],
    ['ORC', 'ORC', 'pause', { minutes: 30 }, 'xy'],
  ] },
];

export const CH = [
  { id: 'entry', title: 'You and the agent', reveal: ['CLI', 'ORC'],
    lede: `Strip everything away and this is the system: you type a command, the agent runs.`,
    story: `<p>The CLI is a <mark>Typer app</mark> that parses your command and hands it to the orchestrator. The orchestrator is the brain of operations — it loads config, creates browser backends, and manages survey sessions.</p>`,
    flow: [['CLI', 'ORC', 'run command', { site: 'branded-surveys' }], ['ORC', 'CLI', 'status output', { sessions: 1 }]] },

  { id: 'loop', title: 'The survey loop', reveal: ['SES'],
    lede: `Each site gets its own session that loops: login, discover, complete, verify.`,
    story: `<p>The <mark>SurveySession</mark> is the workhorse. It logs in with stored credentials, checks the initial balance, discovers available surveys, completes them one by one, classifies each outcome (completed vs screened out vs broken), and verifies points were actually earned.</p>`,
    flow: [['ORC', 'SES', 'create session', { backend: 'browser-use' }], ['SES', 'BRW', 'login', { site: 'branded-surveys' }], ['SES', 'ANS', 'answer questions', { count: 12 }], ['SES', 'BRN', 'record outcome', { points: 150 }]] },

  { id: 'browser', title: 'Browser automation', reveal: ['BRW', 'FPR', 'HBV'],
    lede: `Six pluggable backends, each with its own stealth strategy.`,
    story: `<p>The browser layer is <mark>pluggable by design</mark>. browser-use pairs LLM with Playwright for headless automation. browser-harness drives your real Chrome via CDP — zero bot detection. cloakbrowser passes 30/30 detection tests. Each session gets a fresh fingerprint, and every interaction is timed to mimic human behavior.</p>`,
    flow: [['SES', 'BRW', 'start backend', { type: 'browser-use' }], ['BRW', 'FPR', 'generate identity', { viewport: '1920x1080' }], ['BRW', 'HBV', 'simulate typing', { speed: 'variable' }], ['BRW', 'SES', 'page ready', { stealth: true }]] },

  { id: 'answering', title: 'Answering questions', reveal: ['ANS', 'PRF'],
    lede: `Every question is classified, matched to your profile, and answered consistently.`,
    story: `<p>The answering pipeline is a <mark>four-stage processor</mark>. First, detect attention checks (arithmetic, instruction-following). Second, classify the question type. Third, match the question text to your profile using keyword dictionaries and edit distance. Fourth, generate open-ended answers via LLM grounded in your persona.</p>`,
    flow: [['SES', 'ANS', 'question', { type: 'radio', text: 'How old are you?' }], ['ANS', 'PRF', 'lookup', { field: 'demographics.age' }], ['PRF', 'ANS', 'answer', { value: '35' }], ['ANS', 'SES', 'filled', { element: 'radio#option-3' }]] },

  { id: 'captcha', title: 'Handling CAPTCHAs', reveal: ['CAP'],
    lede: `Four-tier solver — try cheap first, escalate only when needed.`,
    story: `<p>CAPTCHAs are solved in order: <mark>arithmetic</mark> for simple math (free), <mark>LLM vision</mark> for image challenges (cheap), external API for reCAPTCHA/hCAPTCHA (paid), and VNC manual fallback as last resort. The ask-once rule means the owner is never texted twice for the same question.</p>`,
    flow: [['SES', 'CAP', 'captcha detected', { type: 'image' }], ['CAP', 'CAP', 'tier 1: arithmetic', { solved: false }], ['CAP', 'CAP', 'tier 2: LLM vision', { solved: true }], ['CAP', 'SES', 'solution', { answer: 'B' }]] },

  { id: 'memory', title: 'Memory & learning', reveal: ['BRN'],
    lede: `The brain remembers every event and learns from patterns.`,
    story: `<p>Every survey event is recorded: login, completion, failure, CAPTCHA encounter. After <mark>3+ occurrences</mark> of the same pattern, the brain promotes it to a rule. The ask-once escalation checks this memory before texting the owner — preventing the same question from being asked twice.</p>`,
    flow: [['SES', 'BRN', 'record event', { type: 'survey_completed', site: 'branded-surveys' }], ['BRN', 'BRN', 'check promotion', { count: 3 }], ['BRN', 'BRN', 'promote to rule', { rule: 'screened out on demographics' }]] },

  { id: 'all', title: 'The whole system', reveal: [],
    lede: `Everything at once, for free exploration.`,
    story: `<p>Choose which flow runs (bottom left). Hover anything; click to pin; → goes inside. The <mark>Open questions</mark> tab lists every question by ID.</p>`,
    flow: null },
];

export const HOW_HTML = `<div class="eyebrow">survey-agent · v1</div><h1 class="t">How it's built</h1><div class="sub">the shape and what sits around it</div>
<h3 class="sec">Filesystem</h3><pre>survey-agent/
├── survey_agent/
│   ├── cli.py                  # Typer CLI
│   ├── config.py               # Pydantic + YAML
│   ├── orchestrator.py         # Session orchestrator
│   ├── credentials.py          # Keychain store
│   ├── browser/                # 6 pluggable backends
│   ├── brain/                  # Memory + digest
│   ├── answering/              # Question pipeline
│   ├── captcha/                # 4-tier solver
│   ├── profile/                # User persona
│   └── sites/                  # Site adapters
├── data/                       # Runtime state
└── docs/                       # ADRs + atlas</pre>
<h3 class="sec">Tech stack</h3><pre>Python 3.11+ · Playwright · Pydantic
Typer · Rich · keyring · asyncio</pre>
<h3 class="sec">Key decisions</h3><ul>
<li><b>Browser backends</b> — 6 pluggable implementations behind one interface</li>
<li><b>Credential storage</b> — macOS Keychain, never plaintext</li>
<li><b>Ask-once rule</b> — owner never texted twice for same question</li>
<li><b>CAPTCHA tiers</b> — arithmetic → vision → API → VNC</li>
<li><b>Brain autonomy</b> — non-blocking digest triggers on interval</li>
</ul>`;
