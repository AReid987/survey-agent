#!/usr/bin/env python3
"""Continuous survey agent — runs until at least one survey is completed.

Connects to ego-lite via CDP. Polls dashboard for surveys.
Handles text, CAPTCHA (ensemble), radio (force click), drag-and-drop (CDP mouse).
Auto-reconnects on CDP connection loss. Logs to data/survey_run.log.
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
    reads = [read_captcha_once(b64) for _ in range(num_reads)]
    max_len = max((len(r) for r in reads), default=0)
    result = []
    for pos in range(max_len):
        chars = [r[pos] for r in reads if pos < len(r)]
        if chars:
            result.append(Counter(chars).most_common(1)[0][0])
    solved = ''.join(result)
    log(f"    CAPTCHA: {reads} -> {solved}")
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
        await page.mouse.move(sx + (tx - sx) * i / 30, sy + (ty - sy) * i / 30)
        await page.wait_for_timeout(20)
    await page.wait_for_timeout(300)
    await page.mouse.up()
    await page.wait_for_timeout(2000)
    return True


async def wait_for_question(page, max_wait=20):
    for _ in range(max_wait):
        try:
            content = await page.content()
            if any(k in content for k in ['textarea', 'input[type="radio"]', 'cdk-drag', 'PS Captcha', '<h1']):
                return content
            text = await page.evaluate('document.body ? document.body.innerText : ""')
            tl = text.lower()
            if "thank you" in tl or "earned" in tl:
                return "completed"
            if "not a match" in tl or "disqualified" in tl:
                return "disqualified"
            if "survey full" in tl or "fill up" in tl:
                return "full"
        except:
            pass
        await asyncio.sleep(1)
    return None


async def try_click(page, sel, timeout=5000, force=False):
    try:
        await page.locator(sel).click(timeout=timeout, force=force)
        return True
    except:
        return False


async def try_click_next(page, timeout=5000):
    return await try_click(page, 'button:has-text("Next")', timeout=timeout)


async def run_survey_questions(page):
    for q in range(200):
        content = await wait_for_question(page)
        if content == "completed":
            log(f"  Q{q+1}: COMPLETED!")
            return "completed"
        if content == "disqualified":
            log(f"  Q{q+1}: Disqualified")
            return "disqualified"
        if content == "full":
            log(f"  Q{q+1}: Full")
            return "full"
        if content is None:
            log(f"  Q{q+1}: Timeout")
            return "timeout"

        text = await page.evaluate('document.body.innerText.substring(0, 2000)')

        if "PS Captcha" in content:
            for attempt in range(5):
                img_bytes = await page.screenshot()
                code = solve_captcha_ensemble(img_bytes)
                log(f"  Q{q+1}: CAPTCHA attempt {attempt+1}: {code}")
                inp = page.locator('input[type="text"]')
                if await inp.count() > 0:
                    await inp.first.fill(code)
                    await page.wait_for_timeout(500)
                if await try_click_next(page):
                    await page.wait_for_timeout(3000)
                    if "PS Captcha" not in await page.content():
                        log(f"    Solved!")
                        break
                    log(f"    Wrong, refreshing...")
                await try_click(page, 'button:has-text("Refresh")')
                await page.wait_for_timeout(2000)
            else:
                return "captcha_failed"
            continue

        if 'cdk-drag' in content:
            match = re.search(r'number (\d+)', text)
            target_num = match.group(1) if match else "62"
            log(f"  Q{q+1}: DRAG #{target_num}")
            if await cdk_drag_drop(page, target_num):
                if await try_click_next(page):
                    await page.wait_for_timeout(3000)
                    log(f"    Drag OK!")
                else:
                    return "drag_failed"
            else:
                return "drag_failed"
            continue

        textarea = page.locator('textarea')
        if await textarea.count() > 0:
            if "ABRACADABRA" in text:
                await textarea.first.fill("ABRACADABRA")
            else:
                await textarea.first.fill(
                    "My family and children bring me the most happiness. "
                    "I enjoy creating music and exploring new technology in Austin Texas.")
            if await try_click_next(page):
                await page.wait_for_timeout(3000)
                log(f"  Q{q+1}: Text")
            continue

        radios = page.locator('input[type="radio"]')
        if await radios.count() > 0:
            await radios.first.click(force=True)
            if await try_click_next(page):
                await page.wait_for_timeout(3000)
                log(f"  Q{q+1}: Radio")
            continue

        if await try_click(page, 'button:has-text("Continue")'):
            await page.wait_for_timeout(3000)
            log(f"  Q{q+1}: Continue")
            continue

        log(f"  Q{q+1}: Unknown")
        if await try_click_next(page):
            await page.wait_for_timeout(3000)
            continue
        return "unknown"
    return "max_questions"


async def run_one_session(p):
    """Connect to ego-lite, poll and complete surveys. Returns completed count."""
    completed = 0
    attempts = 0

    browser = await p.chromium.connect_over_cdp(CDP_URL)
    log("Connected to ego-lite CDP")
    ctx = browser.contexts[0]

    while len(ctx.pages) > 1:
        try: await ctx.pages[-1].close()
        except: break
    page = ctx.pages[0]

    for poll in range(60):
        log(f"\n--- Poll {poll+1} ---")

        try:
            await page.goto("https://surveys.gobranded.com/users/dashboard",
                           wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(10000)
        except Exception as e:
            log(f"Dashboard error: {e}")
            raise

        try:
            pts = await page.evaluate('document.body.innerText.match(/MY POINTS\\s*(\\d+)/)?.[1] || "?"')
            log(f"Points: {pts}")
        except: pass

        take_btns = page.locator('a:has-text("TAKE SURVEY")')
        btn_count = await take_btns.count()
        log(f"Surveys: {btn_count}")

        if btn_count == 0:
            log("No surveys, waiting 60s...")
            await asyncio.sleep(60)
            continue

        survey_done = False
        for i in range(btn_count):
            attempts += 1

            try:
                await page.goto("https://surveys.gobranded.com/users/dashboard",
                               wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(8000)
            except: break

            take_btns = page.locator('a:has-text("TAKE SURVEY")')
            if i >= await take_btns.count():
                break

            log(f"\n  Survey #{attempts} ({i+1}/{btn_count})")
            n_pages = len(ctx.pages)

            try:
                await take_btns.nth(i).click(timeout=10000)
            except Exception as e:
                log(f"  Click err: {str(e)[:60]}")
                continue

            settled = False
            for w in range(20):
                await asyncio.sleep(1)
                if len(ctx.pages) > n_pages:
                    np = ctx.pages[-1]
                    url = np.url
                    if any(s in url for s in ['screener', 'focusvision', 'samplicio', 'yoursurveynow', 'confirmit']):
                        log(f"  Loaded: {url[:60]}")
                        page = np
                        settled = True
                        break
                    if 'dashboard' in url or 'members' in url:
                        log(f"  Full/expired")
                        try: await np.close()
                        except: pass
                        break

            if not settled:
                log(f"  No survey")
                continue

            try:
                c = await page.content()
                if "Agree and Continue" in c:
                    await try_click(page, 'button:has-text("Agree and Continue")')
                    await page.wait_for_timeout(5000)
                    log(f"  Consent OK")
            except: pass

            try:
                if await try_click(page, 'a:has-text("BEGIN SURVEY")'):
                    await page.wait_for_timeout(5000)
                    log(f"  BEGIN OK")
            except: pass

            result = await run_survey_questions(page)
            log(f"  Result: {result}")

            if result == "completed":
                completed += 1
                log(f"\n*** SURVEY COMPLETED! Total: {completed} ***")
                survey_done = True
                try: await page.close()
                except: pass
                page = ctx.pages[0]
                break

            try: await page.close()
            except: pass
            page = ctx.pages[0]
            await asyncio.sleep(10)

        if completed >= 1:
            return completed

        if not survey_done:
            log(f"All full, waiting 90s...")
            await asyncio.sleep(90)

    return completed


async def main():
    from playwright.async_api import async_playwright

    log("=" * 60)
    log("CONTINUOUS SURVEY AGENT v2")
    log("ego-lite CDP + ensemble CAPTCHA + CDP mouse drag")
    log("=" * 60)

    for retry in range(10):
        try:
            async with async_playwright() as p:
                completed = await run_one_session(p)
                if completed >= 1:
                    log(f"\nSESSION DONE: {completed} surveys completed!")
                    return
        except Exception as e:
            log(f"Error: {e}")

        log(f"Reconnecting in 15s (retry {retry+1}/10)...")
        await asyncio.sleep(15)

        if retry >= 2:
            log("Restarting ego-lite...")
            subprocess.run(["osascript", "-e", 'quit app "ego lite"'], capture_output=True)
            await asyncio.sleep(3)
            subprocess.run(["open", "-a", "ego lite", "--args", "--remote-debugging-port=9222"], capture_output=True)
            await asyncio.sleep(8)

    log("Max retries reached.")


if __name__ == "__main__":
    asyncio.run(main())
