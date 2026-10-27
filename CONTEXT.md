# survey-agent

A single context: an autonomous agent that completes paid surveys as its owner,
accumulates points, and converts them to PayPal cashouts.

## Language

### Surveys and funnels

**Screener**: Qualifying questions a provider asks before letting an agent into
the paid body of a survey. _Avoid_: pre-screen, qualification quiz

**Provider funnel**: The hosted survey flow a platform embeds (PureSpectrum,
Fiftyfive5, SSISurveys, Viewpoint Forum) — distinct from the platform site
itself. _Avoid_: survey vendor wall

**Bot trap**: An answer option planted to detect automation; answering one marks
the session as a bot. Detection must decline, never select. _Avoid_: trick
question

**Unanswerable question**: A question the persistent profile cannot cover.
Policy: escalate to the owner by text, never click the survey's own "I can't
answer" control. _Avoid_: skip question, dismiss modal

**Witnessed completion**: A survey completion evidenced well enough for the
owner to trust it — log trail plus a point-balance delta the owner has seen in
the dashboard. Commit messages alone do not count.

### Execution modes

**DOM mode**: Answering driven by extracted page structure (selectors, refs,
text). The default, fast path.

**Escalation**: Handing a single step to the local vision model because DOM mode
hit a failure condition (image-string CAPTCHA, sparse iframe page, unresolved
click target). Returns the next step and an on-screen target, not a plan.
_Avoid_: vision fallback (it is triggered by conditions, not blanket failure)

**CUA run**: A session driven through OS-level computer control (screenshots,
typed pointer events) rather than page JavaScript. _Avoid_: computer-use mode

**Harness backend**: The Chrome-CDP backend (harness_backend.py) that drives the
owner's real browser profile. _Avoid_: browser-harness, chrome agent

**Ask-once rule**: Every escalated question's owner reply is persisted; the same
question text must never trigger a second text. _Avoid_: human-in-the-loop spam

### Money

**Threshold text**: An iMessage sent to the owner when a site balance reaches
the cashout threshold; the owner either cashes out manually or tells the agent
to continue to the next threshold. _Avoid_: auto-withdrawal

**Points**: Site-denominated earnings convertible to PayPal cash; the unit of
all progress reporting. _Avoid_: credits, dollars (unless quoting a cash figure)
