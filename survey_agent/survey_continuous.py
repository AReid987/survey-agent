#!/usr/bin/env python3
"""Continuous survey agent — runs until at least one survey is completed.

Connects to ego-lite via CDP. Polls dashboard for surveys.
Handles text, CAPTCHA (ensemble), radio (force click), drag-and-drop (CDP mouse).
Logs everything to data/survey_run.log.
"""
import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime

CDP_URL = "http://localhost:9222"
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
CEREBRAS_ENDPOINT = "https://api.cerebras.ai/v1/chat/completions"
CEREBRAS_MODEL = "gemma-4-31b"
CAPTCHA_PROMPT = "Read the distorted text in this image. The characters may include letters and numbers. Return only the characters, nothing else."
LOG_PATH = os.path.expanduser("~/survey-agent/data/survey_run.log")

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def read_captcha_once(image_b64):
    payload = {
        "model": CEREBRAS_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": CAPTCHA_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}}
        ]}],
        "max_tokens": 30, "temperature": 0.1,
    }
    result = subprocess.run(
        ["curl", "-s", "-X", "POST", CEREBRAS_ENDPOINT,
         "-H", f"Authorization: Bearer {CEREBRAS_API_KEY}",
         "-H", "Content-Type: application/json",
         "-d", json.dumps(payload)],
        capture_output=True, text=True, timeout=30
    )
    data = json.loads(result.stdout)
    text = data["choices"][0]["message"]["content"].strip()
    return re.sub(r'[^A-Za-z0-9]', '', text)


def solve_captcha_ensemble(image_bytes, num_reads=3):
    b64 = base64.b64encode(image_bytes).decode()
    reads = []
    for i in range(num_reads):
        code = read_captcha_once(b64)
        reads.append(code)
    max_len = max(len(r) for r in reads) if reads else 0
    result = []
    for pos in range(max_len):
        chars = [r[pos] for r in reads if pos < len(r)]
        if chars:
            result.append(Counter(chars).most_common(1)[0][0])
    solved = ''.join(result)
    log(f"    CAPTCHA reads: {reads} -> ensemble: {solved}")
    return solved


async def cdk_drag_drop(page, source_num):
    coords = await page.evaluate('''(num) => {
        const img = document.querySelector('img[alt="' + num + '"]');
        const drag = img ? img.closest('.cdk-drag') : null;
        const drops = document.querySelectorAll('.cdk-drop-list');
        const target = drops.length > 1 ? drops[1] : drops[0];
        if (!drag || !target) return null;
        const sr = drag.getBoundingClientRect();
        const tr = target.getBoundingClientRect();
        return {sx: sr.x+sr.width/2, sy: sr.y+sr.height/2, tx: tr.x+tr.width/2, ty: tr.y+tr.height/2};
    }''', source_num)
    if not coords:
        return False
    sx, sy, tx, ty = coords['sx'], coords['sy'], coords['tx'], coords['ty']
    await page.mouse.move(sx, sy)
    await page.wait_for_timeout(100)
    await page.mouse.down()
    await page.wait_for_timeout(300)
    for i in range(1, 31):
        await page.mouse.move(sx + (tx-sx)*i/30, sy + (ty-sy)*i/30)
        await page.wait_for_timeout(20)
    await page.wait_for_timeout(300)
    await page.mouse.up()
    await page.wait_for_timeout(2000)
    return True


async def wait_for_question(page, max_wait=20):
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
            if "survey full" in text.lower() or "fill up" in text.lower():
                return "full"
        except:
            pass
        await asyncio.sleep(1)
    return None


async def try_click(page, selector, timeout=5000, force=False):
    try:
        await page.locator(selector).click(timeout=timeout, force=force)
        return True
    except:
        return False


async def try_click_next(page, timeout=5000):
    return await try_click(page, 'button:has-text("Next")', timeout=timeout)


async def run_survey_questions(page):
    """Run through all questions in a survey. Returns result."""
    for q in range(200):
        content = await wait_for_question(page)
        if content == "completed":
            log(f"  Q{q+1}: SURVEY COMPLETED!")
            return "completed"
        if content == "disqualified":
            log(f"  Q{q+1}: Disqualified")
            return "disqualified"
        if content == "full":
            log(f"  Q{q+1}: Survey full")
            return "full"
        if content is None:
            log(f"  Q{q+1}: Timeout waiting for question")
            return "timeout"

        text = await page.evaluate('document.body.innerText.substring(0, 2000)')

        # CAPTCHA
        if "PS Captcha" in content:
            for attempt in range(5):
                img_bytes = await page.screenshot()
                code = solve_captcha_ensemble(img_bytes, num_reads=3)
                log(f"  Q{q+1}: CAPTCHA attempt {attempt+1}: {code}")
                inp = page.locator('input[type="text"]')
                if await inp.count() > 0:
                    await inp.first.fill(code)
                    await page.wait_for_timeout(500)
                if await try_click_next(page):
                    await page.wait_for_timeout(3000)
                    # Check if still on CAPTCHA (wrong code)
                    new_content = await page.content()
                    if "PS Captcha" not in new_content:
                        log(f"    Solved!")
                        break
                    log(f"    Wrong code, refreshing...")
                await try_click(page, 'button:has-text("Refresh")')
                await page.wait_for_timeout(2000)
            else:
                log(f"    CAPTCHA failed after 5 attempts")
                return "captcha_failed"
            continue

        # Drag-and-drop
        if 'cdk-drag' in content:
            match = re.search(r'number (\d+)', text)
            target_num = match.group(1) if match else "62"
            log(f"  Q{q+1}: DRAG #{target_num}")
            success = await cdk_drag_drop(page, target_num)
            if success:
                if await try_click_next(page):
                    await page.wait_for_timeout(3000)
                    log(f"    Drag OK!")
                else:
                    log(f"    Drag done but Next disabled")
                    return "drag_failed"
            else:
                log(f"    Drag elements not found")
                return "drag_failed"
            continue

        # Text input
        textarea = page.locator('textarea')
        if await textarea.count() > 0:
            if "ABRACADABRA" in text:
                await textarea.first.fill("ABRACADABRA")
            else:
                await textarea.first.fill(
                    "My family and children bring me the most happiness. "
                    "I enjoy creating music and exploring new technology in Austin Texas."
                )
            if await try_click_next(page):
                await page.wait_for_timeout(3000)
                log(f"  Q{q+1}: Text answered")
            continue

        # Radio buttons
        radios = page.locator('input[type="radio"]')
        if await radios.count() > 0:
            await radios.first.click(force=True)
            if await try_click_next(page):
                await page.wait_for_timeout(3000)
                log(f"  Q{q+1}: Radio selected")
            continue

        # Continue button (for consent/offer pages)
        if await try_click(page, 'button:has-text("Continue")'):
            await page.wait_for_timeout(3000)
            log(f"  Q{q+1}: Continue clicked")
            continue

        # Unknown
        log(f"  Q{q+1}: Unknown question type")
        await page.screenshot(path=f"/tmp/unknown_q{q+1}.png")
        # Try clicking Next as fallback
        if await try_click_next(page):
            await page.wait_for_timeout(3000)
            continue
        return "unknown"

    return "max_questions"


async def main():
    from playwright.async_api import async_playwright

    log("=" * 60)
    log("CONTINUOUS SURVEY AGENT")
    log("ego-lite CDP + ensemble CAPTCHA + CDP mouse drag")
    log("=" * 60)

    completed_surveys = 0
    total_attempts = 0
    max_poll_attempts = 60  # Poll for up to 60 minutes

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        log(f"Connected to ego-lite via CDP")

        ctx = browser.contexts[0]

        # Close extra tabs
        while len(ctx.pages) > 1:
            try:
                await ctx.pages[-1].close()
            except:
                break
        page = ctx.pages[0]
        log(f"Page: {page.url[:60]}")

        for poll in range(max_poll_attempts):
            log(f"\n--- Poll {poll+1}/{max_poll_attempts} ---")

            # Navigate to dashboard
            try:
                await page.goto("https://surveys.gobranded.com/users/dashboard",
                               wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(10000)
            except Exception as e:
                log(f"Dashboard error: {e}")
                await asyncio.sleep(30)
                continue

            # Get points
            try:
                points_text = await page.evaluate('''
                    () => {
                        const els = document.querySelectorAll('.points-value, [class*="point"]');
                        for (const el of els) {
                            const t = el.textContent.trim();
                            if (/^\\d+$/.test(t)) return t;
                        }
                        return "unknown";
                    }
                ''')
                log(f"Points: {points_text}")
            except:
                pass

            # Find TAKE SURVEY buttons
            take_btns = page.locator('a:has-text("TAKE SURVEY")')
            btn_count = await take_btns.count()
            log(f"TAKE SURVEY buttons: {btn_count}")

            if btn_count == 0:
                log("No surveys available, waiting 60s...")
                await asyncio.sleep(60)
                continue

            # Try each survey
            survey_taken = False
            for i in range(btn_count):
                total_attempts += 1

                # Refresh dashboard
                await page.goto("https://surveys.gobranded.com/users/dashboard",
                               wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(8000)

                take_btns = page.locator('a:has-text("TAKE SURVEY")')
                if i >= await take_btns.count():
                    break

                log(f"\n  Survey #{total_attempts} (button {i+1}/{btn_count})")

                initial_pages = len(ctx.pages)
                try:
                    await take_btns.nth(i).click(timeout=10000)
                except Exception as e:
                    log(f"  Click failed: {e}")
                    continue

                # Wait for redirect chain
                settled = False
                for w in range(20):
                    await asyncio.sleep(1)
                    if len(ctx.pages) > initial_pages:
                        np = ctx.pages[-1]
                        url = np.url

                        # Check for survey platform URLs
                        if any(s in url for s in ['screener', 'focusvision', 'samplicio', 'yoursurveynow', 'confirmit']):
                            log(f"  Survey loaded: {url[:80]}")
                            page = np
                            settled = True
                            break

                        # Redirected to dashboard = full
                        if 'dashboard' in url or 'members' in url:
                            log(f"  Survey full/expired")
                            try: await np.close()
                            except: pass
                            break

                if not settled:
                    log(f"  No survey loaded, continuing")
                    continue

                # Handle consent page
                try:
                    content = await page.content()
                    if "Agree and Continue" in content:
                        await try_click(page, 'button:has-text("Agree and Continue")')
                        await page.wait_for_timeout(5000)
                        log(f"  Consent accepted")
                except:
                    pass

                # Click BEGIN SURVEY if present
                try:
                    if await try_click(page, 'a:has-text("BEGIN SURVEY")'):
                        await page.wait_for_timeout(5000)
                        log(f"  BEGIN SURVEY clicked")
                except:
                    pass

                # Run the survey
                result = await run_survey_questions(page)
                log(f"  Result: {result}")

                if result == "completed":
                    completed_surveys += 1
                    log(f"\n*** SURVEY COMPLETED! Total: {completed_surveys} ***")
                    survey_taken = True

                    # Close survey tab, go back to dashboard
                    try:
                        await page.close()
                    except:
                        pass
                    page = ctx.pages[0]
                    break

                # Close survey tab
                try:
                    await page.close()
                except:
                    pass
                page = ctx.pages[0]

                # Small delay between surveys
                await asyncio.sleep(10)

            if completed_surveys >= 1:
                log(f"\n{'='*60}")
                log(f"SESSION COMPLETE: {completed_surveys} surveys completed")
                log(f"Total attempts: {total_attempts}")
                log(f"{'='*60}")
                break

            if not survey_taken:
                log(f"All {btn_count} surveys full, waiting 90s for new ones...")
                await asyncio.sleep(90)

    log("Agent finished.")


if __name__ == "__main__":
    asyncio.run(main())
