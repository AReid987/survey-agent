# Scaling Status — 2026-07-02 14:38

## What Worked

**Branded Surveys accumulator** (PID 3576, 60 min runtime):
- 8/20 surveys completed (40% rate)
- 158 → 161 (+3 points earned)
- Framework auto-detected and skipped: sparse iframes, stuck pages, redirected surveys
- Viewpoint Forum surveys completed through the flow

## What Failed: Prime Opinion Google OAuth

User identified: PO uses "Sign in with Google" OAuth. Chrome profile must have Gmail session.

**The blocker:** The framework's Chrome profile (`data/chrome_agent_po/`) is a fresh profile with NO Google session cookies. When the agent clicks "Sign in with Google", it opens the Google account chooser/sign-in page — but since no Google account is signed in, it shows the email entry form (not the account list).

**Patches applied (all on `survey_agent/browser/harness_backend.py`):**
1. `AGENT_CHROME_PROFILE` → env-overridable (`CHROME_PROFILE=/path/to/profile`)
2. `CDP_PORT` → env-overridable (`CDP_PORT=19334`)
3. `login()` method now detects Google OAuth sites and uses `_login_google_oauth()`
4. `_login_google_oauth()` clicks "Sign in with Google", handles account chooser AND email entry
5. Login check now navigates to the target site first, then checks if already on dashboard

**Test results:** Even using the user's actual Chrome profile (`~/Library/Application Support/Google/Chrome/Default`), the Google OAuth flow failed because:
- The user's regular Chrome was still running
- The agent launched a separate Chrome instance with the same profile
- The agent's Chrome had the same cookies but no active Google session
- Google sign-in required entering the email and going through the password step (which the agent can't do without the password)

## What's Needed for PO to Work

The user needs to **close their regular Chrome browser** so the agent can launch a Chrome instance that has the Google session loaded. Or the agent needs the user's Google account password (security risk).

## Files Created This Session
- data/run_branded_*.log — Branded Surveys full run
- data/run_prime_*.log — 5 Prime Opinion attempts (all failed)
- data/run_prime_oauth2_*.log, v3_*, v4_*, v5_* — different PO configurations
- survey_agent/browser/harness_backend.py — 5 patches applied

## Processes
- All exited cleanly
- Ports 19333, 19334 clear
- Stale Chrome killed

## Total This Session
- Branded Surveys: 8 completions, +3 points
- Prime Opinion: 0 completions, 5 failed login attempts
- Code: 5 patches to harness_backend.py, 1 patch to orchestrator.py

## Recommendation
Let Branded Surveys run overnight. For Prime Opinion, user must close Chrome or provide Google password (not recommended).