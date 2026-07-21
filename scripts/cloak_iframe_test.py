"""Minimal cloakbrowser iframe test — does cloakbrowser + Playwright unlock iframes?

This is the critical question. If yes → build the full backend. If no → look elsewhere.

Test: Log in to Branded Surveys via Google OAuth, click a PureSpectrum tile,
switch into the iframe, read the question, fill the answer, submit.
"""
import json
import sys
import time
from pathlib import Path

SS_DIR = Path("data/smoke")
SS_DIR.mkdir(parents=True, exist_ok=True)

# Config — same as harness
GOOGLE_EMAIL = "read.musik@gmail.com"


def main():
    from cloakbrowser import launch

    print("[cloak] Launching stealth browser...", flush=True)
    # stealth_args=True: C++-level fingerprint patches
    # humanize=True: Bezier mouse paths, click-hold timing
    browser = launch(
        headless=False,
        stealth_args=True,
        humanize=True,
        timezone="America/New_York",
        locale="en-US",
    )
    print(f"[cloak] Browser: {type(browser).__name__}", flush=True)

    page = browser.new_page()

    # Step 1: Try to detect if already logged in (existing session cookies)
    print("\n[1] Checking for existing Branded Surveys session...", flush=True)
    page.goto("https://surveys.gobranded.com/users/dashboard", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)
    url = page.url
    print(f"  URL: {url}", flush=True)
    if "login" in url or "sign" in url:
        print("  Not logged in — attempting Google OAuth login...", flush=True)
        # Click Google sign-in button
        try:
            google_btn = page.query_selector('a:has-text("Google"), button:has-text("Google"), [aria-label*="Google" i]')
            if google_btn:
                google_btn.click()
                page.wait_for_timeout(5000)
                print(f"  After Google click URL: {page.url}", flush=True)
                page.screenshot(path=str(SS_DIR / "cloak_iframe_oauth.png"))
        except Exception as e:
            print(f"  Google OAuth error: {e}", flush=True)
            print("  ** NEEDS MANUAL: Close your regular Chrome, then run this script **", flush=True)
            page.screenshot(path=str(SS_DIR / "cloak_iframe_oauth_failed.png"))
    else:
        print("  Already logged in!", flush=True)

    # Step 2: Discover surveys
    print("\n[2] Discovering surveys...", flush=True)
    page.goto("https://surveys.gobranded.com/users/dashboard", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)
    tiles = page.query_selector_all(".list-item.survey-item")
    print(f"  Found {len(tiles)} survey tiles", flush=True)
    page.screenshot(path=str(SS_DIR / "cloak_iframe_dashboard.png"))

    if not tiles:
        print("  No tiles — login probably failed", flush=True)
        browser.close()
        return

    # Step 3: Click first tile
    print("\n[3] Clicking first survey tile...", flush=True)
    tiles[0].click()
    page.wait_for_timeout(8000)
    url = page.url
    print(f"  After click URL: {url}", flush=True)
    page.screenshot(path=str(SS_DIR / "cloak_iframe_after_click.png"))

    # Step 4: Check for iframes — THE critical test
    print("\n[4] Checking for iframes (the blocker)...", flush=True)
    iframes = page.query_selector_all("iframe")
    print(f"  Found {len(iframes)} iframes", flush=True)
    for i, iframe in enumerate(iframes[:5]):
        src = iframe.get_attribute("src")
        box = iframe.bounding_box()
        print(f"  Iframe {i}: src={(src or '')[:100]!r} box={box}", flush=True)

    if not iframes:
        print("  No iframes — this survey is accessible directly!", flush=True)
        body = page.evaluate("document.body ? document.body.innerText.substring(0, 500) : ''")
        print(f"  Body: {body[:200]!r}", flush=True)
    else:
        # Step 5: Try to access iframe content via Playwright's frame API
        print("\n[5] Trying to access iframe content via Playwright frame API...", flush=True)
        for i, iframe in enumerate(iframes[:3]):
            try:
                frame = iframe.content_frame()
                if frame:
                    frame_text = frame.evaluate("document.body ? document.body.innerText.substring(0, 500) : ''")
                    frame_html_len = frame.evaluate("document.body ? document.body.innerHTML.length : 0")
                    print(f"  Iframe {i} content (first 300 chars):", flush=True)
                    print(f"    {frame_text[:300]!r}", flush=True)
                    print(f"  Iframe {i} HTML length: {frame_html_len}", flush=True)

                    # Check for question/answer elements inside iframe
                    question_info = frame.evaluate("""() => {
                        const radios = document.querySelectorAll('input[type="radio"]');
                        const textareas = document.querySelectorAll('textarea');
                        const selects = document.querySelectorAll('select');
                        const buttons = document.querySelectorAll('button, input[type="submit"]');
                        const labels = [];
                        radios.forEach((r, idx) => {
                            const label = r.labels && r.labels[0] ? r.labels[0].innerText : '';
                            labels.push({idx, id: r.id, label: label.substring(0, 50), checked: r.checked});
                        });
                        return {
                            radio_count: radios.length,
                            textarea_count: textareas.length,
                            select_count: selects.length,
                            button_count: buttons.length,
                            radios: labels.slice(0, 5)
                        };
                    }""")
                    print(f"  Iframe {i} question elements: {json.dumps(question_info, indent=2)}", flush=True)

                    # Try to click first radio and submit
                    if question_info.get("radio_count", 0) > 0:
                        print(f"  Attempting to click first radio in iframe {i}...", flush=True)
                        radios = frame.query_selector_all("input[type='radio']")
                        if radios:
                            radios[0].check(force=True)
                            page.wait_for_timeout(1000)
                            # Click submit
                            submit = frame.query_selector("button[type='submit'], input[type='submit'], button:has-text('Next'), button:has-text('Submit')")
                            if submit:
                                submit.click(force=True)
                                page.wait_for_timeout(3000)
                                page.screenshot(path=str(SS_DIR / f"cloak_iframe_submitted_{i}.png"))
                                print(f"  Iframe {i} submitted successfully!", flush=True)
                            else:
                                print(f"  No submit button found in iframe {i}", flush=True)
                else:
                    print(f"  Iframe {i}: content_frame() returned None", flush=True)
            except Exception as e:
                print(f"  Iframe {i} error: {type(e).__name__}: {e}", flush=True)

    # Step 6: Check final state
    print("\n[6] Final check...", flush=True)
    page.wait_for_timeout(3000)
    print(f"  Final URL: {page.url}", flush=True)
    page.screenshot(path=str(SS_DIR / "cloak_iframe_final.png"))

    print("\n[done] Test complete. Check screenshots in data/smoke/cloak_iframe_*.png", flush=True)
    browser.close()


if __name__ == "__main__":
    main()
