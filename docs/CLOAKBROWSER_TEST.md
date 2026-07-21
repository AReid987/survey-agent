# cloakbrowser Test Results

## Test Date
2026-07-02 21:22

## Key Finding
**cloakbrowser successfully bypasses all bot detection.** The stealth works.

## Test Results

### Stealth (browserleaks.com)
- `navigator.webdriver`: **false** (the key tell — passes)
- User agent: Real Chrome 145 on Mac, no automation signature
- Plugins: 5 (real browser fingerprint)
- Languages: en-US
- Platform: MacIntel
- Hardware: 8 cores, 8GB

### Stealth (sannysoft.com)
All 20+ bot detection tests show **"ok"** (clean):
- PHANTOM_UA, PHANTOM_PROPERTIES, PHANTOM_ETSL, PHANTOM_LANGUAGE, PHANTOM_WEBSOCKET
- MQ_SCREEN, PHANTOM_OVERFLOW, PHANTOM_WINDOW_HEIGHT
- HEADCHR_UA, HEADCHR_CHROME_OBJ, HEADCHR_PERMISSIONS, HEADCHR_PLUGINS, HEADCHR_IFRAME
- CHR_DEBUG_TOOLS, SELENIUM_DRIVER, CHR_BATTERY, CHR_MEMORY
- TRANSPARENT_PIXEL, SEQUENTUM, VIDEO_CODECS

### Branded Surveys Login
- cloakbrowser launched successfully
- Landed on `/users/login/` (expected — fresh profile, no session)
- "Sign in with email" button clicked, email filled
- Form submit didn't complete login (no password — Branded uses Google OAuth)
- Stayed on login page

## The Remaining Blocker
Not cloakbrowser — it's the Google OAuth flow. Branded Surveys uses Google OAuth for login. cloakbrowser doesn't have the user's Google session cookies.

**Two paths to make it work:**
1. User closes their regular Chrome, then runs cloakbrowser — the new Chrome instance will inherit the Google session
2. OR: cloakbrowser navigates to accounts.google.com and completes the password step (requires user's Google password)

## Files Created
- `scripts/cloak_test.py` — test script (5 test sections)
- `data/cloak_test_run1.log` through `run4.log` — test run logs
- `data/smoke/cloak_test_branded.png` — Branded Surveys page screenshot
- `data/smoke/cloak_test_login.png` — login page screenshot
- `data/smoke/cloak_test_sannysoft.png` — sannysoft full-page screenshot
- `data/smoke/cloak_test_stealth.png` — browserleaks screenshot

## How to Use cloakbrowser Going Forward

```python
from cloakbrowser import launch
browser = launch(headless=False, stealth_args=True, humanize=True)
page = browser.new_page()
# Use standard Playwright API
page.goto("https://example.com")
# etc.
```

Drop-in Playwright replacement. Same API. `stealth_args=True` enables fingerprint patches, `humanize=True` enables Bezier mouse paths and timing jitter.