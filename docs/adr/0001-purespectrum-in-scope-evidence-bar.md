# PureSpectrum is in scope; success claims require owner-witnessed evidence

Date: 2026-08-26. Status: accepted.

## Context

docs/REVIVAL_PLAN.md (July 2026) said "Skip PureSpectrum permanently" because
per-page text CAPTCHAs made the economics unviable. Later commits in July claim
the funnel completed end to end multiple times ("feat: 100% ON PURESCREENER!
Full pipeline proven end-to-end!", a63e1b1, 9e0ee26 and successors). The owner
has never witnessed any such completion: there is no log-backed completion trail
and no owner-confirmed point-balance delta.

## Decision

PureSpectrum is treated as an in-scope provider. All prior success claims — for
PureSpectrum or any provider funnel — are downgraded to unverified until a run
produces a witnessed completion: a log trail plus a point-balance change the
owner confirms in the site dashboard. Commit messages alone are not evidence.

## Consequences

- PLAN.md ticks that cited commit-message success claims were corrected on
  2026-08-26.
- The reliability bar (#loop-bar) must be ratified before any component can
  claim "works".
- The escalation/vision track (#escalate) exists partly because per-page
  CAPTCHAs remain unsolved; the original economic objection to PureSpectrum
  stands until they are.
