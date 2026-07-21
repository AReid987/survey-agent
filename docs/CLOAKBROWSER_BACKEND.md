# cloakbrowser Backend — Complete

## What I Built

**New file:** `survey_agent/browser/cloakbrowser_backend.py` (530 lines)
**Registered in factory:** `survey_agent/browser/__init__.py` now accepts `cloakbrowser` as a backend
**New test script:** `scripts/cloak_oauth_test.py`

## The Architecture

Cloakbrowser's sync Playwright API uses greenlets bound to a specific thread.
Mixing it with asyncio directly causes "Cannot switch to a different thread" errors.
The fix: a dedicated worker thread owns the Playwright Browser. The main async backend
dispatches commands via a thread-safe queue.

## Bugs Found & Fixed During Development

### 1. Google sign-in button is in a GSI iframe (not top frame)
The harness backend's button clicker queries `document.querySelectorAll('a, button, div')`
in the top frame — but Branded Surveys renders the Google button inside an iframe from
`accounts.google.com/gsi/button?type=standard&...`. The top frame can't see inside.

**Fix:** Switch into the iframe via `iframe.content_frame()`, get the button's screen
coordinates relative to the iframe, translate to top-page coords, then `page.mouse.click()`.

### 2. JS click on GSI button is untrusted
`btn.click()` via JS doesn't generate a trusted user gesture, so Google's OAuth popup
doesn't open.

**Fix:** Use Playwright's `page.mouse.click(x, y)` with real coordinates — generates
trusted input events via CDP.

### 3. Google OAuth opens in a NEW TAB, not the same page
After clicking the GSI button, Google opens a popup tab to `accounts.google.com/v3/signin/...`.
The backend was checking only `self._page.url` — it never saw the new tab.

**Fix:** Capture initial page count before click, check `page.context.pages` after click
to detect new tab and get its URL.

### 4. cloakbrowser sync API refuses asyncio loops
`cloakbrowser.launch()` calls `sync_playwright().start()` which detects asyncio loops
and raises Error. The worker thread runs in its own plain thread (no asyncio loop).

## Test Results

### Standalone OAuth test
- GSI iframe: 1 found
- Button coords: (150, 22) inside iframe
- Click position: (600, 131) on top page
- New popup opened to: `https://accounts.google.com/v3/signin/challenge/pk?...`
- Login result: **success=True, output='oauth_account_click'**

### Bot detection (sannysoft.com)
All 20+ tests show "ok":
- PHANTOM_UA, PHANTOM_PROPERTIES, PHANTOM_ETSL, PHANTOM_LANGUAGE, PHANTOM_WEBSOCKET
- MQ_SCREEN, PHANTOM_OVERFLOW, PHANTOM_WINDOW_HEIGHT
- HEADCHR_UA, HEADCHR_CHROME_OBJ, HEADCHR_PERMISSIONS, HEADCHR_PLUGINS, HEADCHR_IFRAME
- CHR_DEBUG_TOOLS, SELENIUM_DRIVER, CHR_BATTERY, CHR_MEMORY
- TRANSPARENT_PIXEL, SEQUENTUM, VIDEO_CODECS

### Browserleaks.com
- `navigator.webdriver`: false (the key tell — passes)
- User agent: Real Chrome 145 on Mac, no automation signature

## How to Use

### Run a survey site with cloakbrowser
```bash
pdm run python -m survey_agent.cli run -s branded-surveys -n 5 --backend cloakbrowser
```

### Test the OAuth flow standalone
```bash
pdm run python scripts/cloak_oauth_test.py
```

## What's Still TODO

1. **complete_survey** is a minimal stub — click consent, navigate, return state
2. **iframe access in surveys** — Playwright's frame API can access cross-origin frames;
   need to integrate the persona_pick_radio logic from harness_backend
3. **withdraw** — placeholder, just navigates to redeem page
4. **Run both backends simultaneously** — confirmed possible (different binaries, ports, profiles)

## Files Created/Modified

- `survey_agent/browser/cloakbrowser_backend.py` (NEW, 530 lines)
- `survey_agent/browser/__init__.py` (modified, added cloakbrowser to factory)
- `scripts/cloak_oauth_test.py` (NEW, 70 lines)
- `data/cloak_oauth_test_run10.log` (test output showing OAuth flow working)