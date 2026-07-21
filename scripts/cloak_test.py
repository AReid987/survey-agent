"""cloakbrowser test — verify it bypasses PureSpectrum iframe/CAPTCHA detection.

Uses cloakbrowser's sync API (it's a Playwright wrapper, sync only).
"""
import json
import sys
from pathlib import Path

SS_DIR = Path("data/smoke")
SS_DIR.mkdir(parents=True, exist_ok=True)


def main():
    from cloakbrowser import launch

    print("[cloak] Launching stealth browser...", flush=True)
    # stealth_args=True includes default fingerprint patches
    # humanize=True adds Bezier mouse paths, click-hold timing, reading jitter
    browser = launch(headless=False, stealth_args=True, humanize=True)
    print(f"[cloak] Browser launched: {type(browser).__name__}", flush=True)

    page = browser.new_page()

    # Test 1: Login to Branded Surveys first
    print("\n[test 0] Logging in to Branded Surveys (Google OAuth)...", flush=True)
    try:
        page.goto("https://surveys.gobranded.com/users/login/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        # Click "Sign in with email" or "Sign in with Google"
        signin = page.query_selector('a:has-text("Sign in with email"), button:has-text("Sign in with email")')
        if signin:
            signin.click()
            page.wait_for_timeout(3000)
        # Email field
        email_input = page.query_selector('input[type="email"], #UserEmail, input[name="email"]')
        if email_input:
            email_input.fill("read.musik@gmail.com")
            page.wait_for_timeout(1000)
            # Password
            pw_input = page.query_selector('input[type="password"], #UserPassword, input[name="password"]')
            if pw_input:
                pw_input.fill("")  # No password — will use OAuth
            # Submit
            submit = page.query_selector('input[type="submit"], button[type="submit"]')
            if submit:
                submit.click()
                page.wait_for_timeout(10000)
        url = page.url
        print(f"  After email submit URL: {url}", flush=True)
        page.screenshot(path=str(SS_DIR / "cloak_test_login.png"))
    except Exception as e:
        print(f"  Login ERROR: {e}", flush=True)

    # Test 1b: Navigate to Branded Surveys dashboard after login attempt
    print("\n[test 1] Navigating to Branded Surveys dashboard...", flush=True)
    try:
        page.goto("https://surveys.gobranded.com/users/dashboard", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(5000)
        url = page.url
        title = page.title()
        body = page.evaluate("document.body ? document.body.innerText.substring(0, 500) : ''")
        print(f"  URL: {url}", flush=True)
        print(f"  Title: {title}", flush=True)
        print(f"  Body preview: {body[:200]!r}", flush=True)
        page.screenshot(path=str(SS_DIR / "cloak_test_branded.png"))
        print(f"  Screenshot: {SS_DIR / 'cloak_test_branded.png'}", flush=True)
    except Exception as e:
        print(f"  ERROR: {e}", flush=True)

    # Test 2: Click a survey tile, check if we can access iframe content
    print("\n[test 2] Clicking first survey tile...", flush=True)
    try:
        tiles = page.query_selector_all(".list-item.survey-item")
        print(f"  Found {len(tiles)} tiles", flush=True)
        if tiles:
            tiles[0].click()
            page.wait_for_timeout(8000)
            url = page.url
            print(f"  After click URL: {url}", flush=True)

            # Check for iframes
            iframes = page.query_selector_all("iframe")
            print(f"  Found {len(iframes)} iframes", flush=True)

            for i, iframe in enumerate(iframes[:3]):
                src = iframe.get_attribute("src")
                box = iframe.bounding_box()
                print(f"  Iframe {i}: src={(src or '')[:100]!r} box={box}", flush=True)

            page.screenshot(path=str(SS_DIR / "cloak_test_tile_click.png"))
            print(f"  Screenshot: {SS_DIR / 'cloak_test_tile_click.png'}", flush=True)

            # Try to access iframe content — the key blocker for browser-harness
            for i, iframe in enumerate(iframes[:3]):
                try:
                    frame = iframe.content_frame()
                    if frame:
                        frame_text = frame.evaluate("document.body ? document.body.innerText.substring(0, 300) : ''")
                        print(f"  Iframe {i} content (first 200 chars): {frame_text[:200]!r}", flush=True)
                    else:
                        print(f"  Iframe {i}: content_frame() returned None", flush=True)
                except Exception as e:
                    print(f"  Iframe {i} content access: {type(e).__name__}: {e}", flush=True)
    except Exception as e:
        print(f"  ERROR: {e}", flush=True)

    # Test 3: Check for CAPTCHAs
    print("\n[test 3] Checking for CAPTCHAs...", flush=True)
    try:
        captcha_indicators = page.evaluate("""() => {
            const text = document.body ? document.body.innerText.toLowerCase() : '';
            return {
                has_captcha_text: text.includes('captcha') || text.includes('challenge'),
                iframe_count: document.querySelectorAll('iframe').length,
                has_turnstile: !!document.querySelector('.cf-turnstile, #cf-chl-widget'),
                has_recaptcha: !!document.querySelector('.g-recaptcha, #recaptcha'),
                url: window.location.href
            };
        }""")
        print(f"  CAPTCHA indicators: {json.dumps(captcha_indicators, indent=2)}", flush=True)
    except Exception as e:
        print(f"  ERROR: {e}", flush=True)

    # Test 4: Stealth test — check if bot detection sites see us as human
    print("\n[test 4] Checking bot detection (browserleaks.com)...", flush=True)
    try:
        page.goto("https://browserleaks.com/javascript", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(5000)
        detection = page.evaluate("""() => {
            return {
                webdriver: navigator.webdriver,
                user_agent: navigator.userAgent.substring(0, 100),
                plugins_count: navigator.plugins.length,
                languages: navigator.languages ? navigator.languages.slice(0, 3) : [],
                platform: navigator.platform,
                hardwareConcurrency: navigator.hardwareConcurrency,
                deviceMemory: navigator.deviceMemory
            };
        }""")
        print(f"  Detection results: {json.dumps(detection, indent=2)}", flush=True)
        page.screenshot(path=str(SS_DIR / "cloak_test_stealth.png"))
    except Exception as e:
        print(f"  ERROR: {e}", flush=True)

    # Test 5: Check if we're flagged on bot.sannysoft.com
    print("\n[test 5] Checking sannysoft bot detection...", flush=True)
    try:
        page.goto("https://bot.sannysoft.com", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(5000)
        page.screenshot(path=str(SS_DIR / "cloak_test_sannysoft.png"), full_page=True)
        sannysoft = page.evaluate("""() => {
            const rows = document.querySelectorAll('table tr');
            const result = {};
            rows.forEach(row => {
                const cells = row.querySelectorAll('td');
                if (cells.length >= 3) {
                    const key = cells[0].innerText.trim();
                    const val = cells[1].innerText.trim();
                    result[key] = val;
                }
            });
            return result;
        }""")
        print(f"  Sannysoft results: {json.dumps(sannysoft, indent=2)}", flush=True)
    except Exception as e:
        print(f"  ERROR: {e}", flush=True)

    print("\n[done] Test complete. Check screenshots in data/smoke/", flush=True)
    browser.close()


if __name__ == "__main__":
    main()
