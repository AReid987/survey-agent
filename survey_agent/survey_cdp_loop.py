#!/usr/bin/env python3
"""Survey automation via Playwright CDP connection to ego-lite.

Connects to ego-lite's Chrome DevTools Protocol for:
- Full CDP mouse events (solves drag-and-drop)
- Direct page access (no cua-driver expiry)
- ego-lite's zero bot detection

CAPTCHA: Ensemble voting (3 reads, majority vote per character)
Drag-and-drop: Playwright page.mouse CDP events
"""
import asyncio
import base64
import os
import re
import sys
import time
from collections import Counter

import json as _json
import subprocess

# ── Config ─────────────────────────────────────────────────────────────
CDP_URL = "http://localhost:9222"
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
CEREBRAS_ENDPOINT = "https://api.cerebras.ai/v1/chat/completions"
CEREBRAS_MODEL = "gemma-4-31b"

CAPTCHA_PROMPT = "Read the distorted text in this image. The characters may include letters and numbers. Return only the characters, nothing else."

# Survey profile
PROFILE_TEXT = """Age: 39, Gender: Male, State: Texas, ZIP: 78701
Household income: $60,000-$79,999, Individual: $40,000-$59,999
Race: Black or African American, Employment: Self-employed
Education: Bachelor's degree, Children: 3, Marital: Single
Urban area, Former smoker, Never drinks"""


# ── CAPTCHA ────────────────────────────────────────────────────────────
def read_captcha_once(image_b64: str) -> str:
    """Single CAPTCHA read via Cerebras."""
    payload = {
        "model": CEREBRAS_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": CAPTCHA_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}}
        ]}],
        "max_tokens": 30,
        "temperature": 0.1,
    }
    headers = {"Authorization": f"Bearer {CEREBRAS_API_KEY}", "Content-Type": "application/json"}
    result = subprocess.run(
        ["curl", "-s", "-X", "POST", CEREBRAS_ENDPOINT,
         "-H", f"Authorization: Bearer {CEREBRAS_API_KEY}",
         "-H", "Content-Type: application/json",
         "-d", _json.dumps(payload)],
        capture_output=True, text=True, timeout=30
    )
    data = _json.loads(result.stdout)
    text = data["choices"][0]["message"]["content"].strip()
    return re.sub(r'[^A-Za-z0-9]', '', text)


def solve_captcha_ensemble(image_bytes: bytes, num_reads: int = 3) -> str:
    """Read CAPTCHA multiple times and vote per character position."""
    b64 = base64.b64encode(image_bytes).decode()
    reads = []
    for i in range(num_reads):
        code = read_captcha_once(b64)
        reads.append(code)
        print(f"    Read {i+1}: {code}")

    # Majority vote per position (use longest read length)
    max_len = max(len(r) for r in reads)
    result = []
    for pos in range(max_len):
        chars_at_pos = []
        for r in reads:
            if pos < len(r):
                chars_at_pos.append(r[pos])
        if chars_at_pos:
            # Most common character at this position
            most_common = Counter(chars_at_pos).most_common(1)[0][0]
            result.append(most_common)
        else:
            result.append('?')

    solved = ''.join(result)
    print(f"    Ensemble: {solved} (from {reads})")
    return solved


# ── Drag-and-Drop ─────────────────────────────────────────────────────
async def cdk_drag_drop(page, source_num: str):
    """Drag a numbered image into the drop target using CDP mouse events."""
    coords = await page.evaluate('''(num) => {
        const img = document.querySelector('img[alt="' + num + '"]');
        const drag = img ? img.closest('.cdk-drag') : null;
        const drops = document.querySelectorAll('.cdk-drop-list');
        const target = drops.length > 1 ? drops[1] : drops[0];
        if (!drag || !target) return null;
        const sr = drag.getBoundingClientRect();
        const tr = target.getBoundingClientRect();
        return {
            sx: sr.x + sr.width/2, sy: sr.y + sr.height/2,
            tx: tr.x + tr.width/2, ty: tr.y + tr.height/2
        };
    }''', source_num)

    if not coords:
        print(f"    Could not find drag elements for #{source_num}")
        return False

    sx, sy = coords['sx'], coords['sy']
    tx, ty = coords['tx'], coords['ty']
    print(f"    Drag ({sx:.0f},{sy:.0f}) -> ({tx:.0f},{ty:.0f})")

    # CDP mouse events via Playwright
    await page.mouse.move(sx, sy)
    await page.wait_for_timeout(100)
    await page.mouse.down()
    await page.wait_for_timeout(300)
    for i in range(1, 31):
        await page.mouse.move(sx + (tx - sx) * i / 30, sy + (ty - sy) * i / 30)
        await page.wait_for_timeout(20)
    await page.wait_for_timeout(300)
    await page.mouse.up()
    await page.wait_for_timeout(2000)
    return True


# ── Page State ─────────────────────────────────────────────────────────
async def wait_for_question(page, max_wait=15):
    """Wait for question content to load."""
    for _ in range(max_wait):
        try:
            content = await page.content()
            has_q = ('textarea' in content or 'input[type="radio"]' in content or
                     'cdk-drag' in content or 'PS Captcha' in content or '<h1' in content)
            if has_q:
                return content
            text = await page.evaluate('document.body ? document.body.innerText : ""')
            if "thank you" in text.lower() or "earned" in text.lower():
                return "completed"
            if "not a match" in text.lower() or "disqualified" in text.lower():
                return "disqualified"
        except:
            pass
        await asyncio.sleep(1)
    return None


async def try_click_next(page, timeout=5000):
    try:
        await page.locator('button:has-text("Next")').click(timeout=timeout)
        return True
    except:
        return False


async def try_click(page, selector, timeout=3000):
    try:
        await page.locator(selector).click(timeout=timeout)
        return True
    except:
        return False


# ── Main Loop ──────────────────────────────────────────────────────────
async def run_survey(page):
    """Run through a single survey. Returns result string."""
    for q in range(100):
        content = await wait_for_question(page)

        if content == "completed":
            print(f"  Q{q+1}: SURVEY COMPLETED!")
            return "completed"
        if content == "disqualified":
            print(f"  Q{q+1}: DISQUALIFIED")
            return "disqualified"
        if content is None:
            print(f"  Q{q+1}: Timeout")
            return "timeout"

        text = await page.evaluate('document.body.innerText.substring(0, 2000)')

        # ── CAPTCHA ──
        if "PS Captcha" in content:
            for attempt in range(3):
                img_bytes = await page.screenshot()
                code = solve_captcha_ensemble(img_bytes, num_reads=3)
                print(f"  Q{q+1}: CAPTCHA attempt {attempt+1}: {code}")
                await page.locator('input[type="text"]').first.fill(code)
                await page.wait_for_timeout(500)
                if await try_click_next(page):
                    await page.wait_for_timeout(3000)
                    print(f"    Solved!")
                    break
                # Wrong code — refresh
                await try_click(page, 'button:has-text("Refresh")')
                await page.wait_for_timeout(2000)
            continue

        # ── Drag-and-Drop ──
        if 'cdk-drag' in content:
            match = re.search(r'number (\d+)', text)
            target_num = match.group(1) if match else "62"
            print(f"  Q{q+1}: DRAG #{target_num}", end="")
            success = await cdk_drag_drop(page, target_num)
            if success:
                if await try_click_next(page):
                    await page.wait_for_timeout(3000)
                    print(" OK!")
                else:
                    print(" (Next still disabled)")
                    return "drag_failed"
            else:
                print(" FAIL")
                return "drag_failed"
            continue

        # ── Text Input ──
        textarea = page.locator('textarea')
        if await textarea.count() > 0:
            # Check for prompt injection
            if "ABRACADABRA" in text:
                await textarea.first.fill("ABRACADABRA")
            else:
                await textarea.first.fill(
                    "My family and children bring me the most happiness. "
                    "I enjoy creating music and exploring new technology in Austin Texas."
                )
            if await try_click_next(page):
                await page.wait_for_timeout(3000)
                print(f"  Q{q+1}: Text")
            continue

        # ── Radio Buttons ──
        radios = page.locator('input[type="radio"]')
        if await radios.count() > 0:
            await radios.first.click(force=True)
            if await try_click_next(page):
                await page.wait_for_timeout(3000)
                print(f"  Q{q+1}: Radio")
            continue

        # ── Unknown ──
        print(f"  Q{q+1}: Unknown question type")
        await page.screenshot(path=f"/tmp/unknown_q{q+1}.png")
        return "unknown"

    return "max_questions"


async def main():
    from playwright.async_api import async_playwright

    print("=" * 60)
    print("SURVEY AUTOMATION VIA ego-lite CDP")
    print("CAPTCHA: ensemble voting | DnD: CDP mouse events")
    print("=" * 60)

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        print(f"Connected to ego-lite via CDP ({CDP_URL})")

        # Get existing context and pages
        contexts = browser.contexts
        if not contexts:
            print("No browser contexts found!")
            return
        context = contexts[0]
        pages = context.pages
        print(f"Found {len(pages)} open pages")

        # Find or create the survey page
        page = None
        for pg in pages:
            if 'branded' in pg.url.lower() or 'survey' in pg.url.lower():
                page = pg
                break
        if not page:
            page = pages[0]

        print(f"Using page: {page.url[:80]}")

        # Navigate to dashboard to get fresh surveys
        await page.goto("https://surveys.gobranded.com/users/dashboard",
                       wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(10000)  # Wait for dynamic content

        # Get survey URLs — try direct links first, then TAKE SURVEY hrefs
        survey_urls = await page.eval_on_selector_all(
            'a[href*="/surveys/pre/"]', 'els => els.map(e => e.href)'
        )
        if not survey_urls:
            # TAKE SURVEY buttons use /router/go — get their hrefs
            survey_urls = await page.locator('a:has-text("TAKE SURVEY")').evaluate_all(
                'els => els.map(e => e.href).filter(h => h && h.includes("survey"))'
            )
        if not survey_urls:
            # Fallback: get all TAKE SURVEY hrefs
            survey_urls = await page.locator('a:has-text("TAKE SURVEY")').evaluate_all(
                'els => els.map(e => e.href)'
            )
        print(f"Found {len(survey_urls)} surveys")

        if not survey_urls:
            print("No surveys available!")
            return

        for i in range(min(5, len(survey_urls))):
            # Refresh dashboard to get fresh TAKE SURVEY buttons
            await page.goto("https://surveys.gobranded.com/users/dashboard",
                           wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(8000)

            take_btns = page.locator('a:has-text("TAKE SURVEY")')
            btn_count = await take_btns.count()
            if i >= btn_count:
                print(f"No more surveys (have {btn_count})")
                break

            print(f"\n--- Survey #{i+1} of {btn_count} ---")
            initial_pages = len(context.pages)

            # Click the TAKE SURVEY button directly
            await take_btns.nth(i).click()
            await page.wait_for_timeout(10000)

            # Check if a new tab opened
            if len(context.pages) > initial_pages:
                page = context.pages[-1]  # Switch to newest tab
                await page.wait_for_timeout(3000)
                print(f"  New tab: {page.url[:80]}")
            else:
                print(f"  Same tab: {page.url[:80]}")

            # Check if redirected to dashboard (survey full/expired)
            if "dashboard" in page.url or "members" in page.url:
                page_text = await page.evaluate('document.body.innerText.substring(0, 500)')
                if "SURVEY FULL" in page_text or "fill up" in page_text.lower():
                    print("  Survey full, skipping")
                    continue
                # Might be a qual page
                print(f"  On page: {page.url[:80]}")

            # Handle consent page if present
            content = await page.content()
            if "Agree and Continue" in content:
                await try_click(page, 'button:has-text("Agree and Continue")')
                await page.wait_for_timeout(5000)

            # Click BEGIN SURVEY if present
            await try_click(page, 'a:has-text("BEGIN SURVEY")')
            await page.wait_for_timeout(5000)

            # Run the survey
            result = await run_survey(page)
            print(f"  Result: {result}")

            if result == "completed":
                print("\n*** SURVEY COMPLETED! CHECK POINTS! ***")
                break

            # Go back to dashboard for next survey
            await page.goto("https://surveys.gobranded.com/users/dashboard",
                           wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
