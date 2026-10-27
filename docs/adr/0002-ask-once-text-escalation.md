# Never click "I can't answer"; escalate unknown questions by text with ask-once persistence

Date: 2026-08-26. Status: accepted.

## Context

When a survey question falls outside the persistent profile, current runners
click the survey's own "I can't answer" / skip control — sometimes 260 times in
a single logged run (data/survey_run.log, July 24). This burns the survey, risks
rejection, and teaches the system nothing. The alternative in cua_agent_v5.py
(texts the owner and waits) was never wired into the main loop.

## Decision

The agent will never click "I can't answer". For an unanswerable question it:

1. texts the owner the question and options (iMessage),
2. persists the owner's reply into profile/brain memory,
3. applies the ask-once rule: that question text must never trigger a second
   text. The end state is a profile that covers every recurring question, so
   texts trend to zero. Automated withdrawal is dropped in favor of a threshold
   text: at payout threshold the owner is notified and either cashes out
   manually or defers.

## Considered options

- Dismiss-and-abandon per unanswerable question: protects hourly rate but wastes
  the survey and learns nothing. Rejected.
- Fully automatic withdrawal via WithdrawalManager: module exists but has zero
  callers; owner prefers manual control of money movement. Rejected; manager to
  be deleted.

## Consequences

- Requires the imsg CLI (brew install steipete/tap/imsg; not yet installed).
- Brain store gains a new first-class event type: answered escalations are
  learnings.
- The dismiss-loop code paths in the runners are to be removed, not configured
  away.
