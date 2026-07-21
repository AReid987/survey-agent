#!/usr/bin/env python3
"""Playwright-based survey automation using ego-lite's cookies.

Bypasses cua-driver session issues by using Playwright directly with
ego-lite's Chrome profile cookies. Handles all question types via JS.
"""
import asyncio
import json
import sqlite3
import shutil
import sys
import time
from pathlib import Path


async def run_survey(survey_url: str):
    """Run a survey end-to-end using Playwright with ego-lite cookies."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        # Launch browser with ego-lite-like settings
        browser = await p.chromium.launch(
            headless=False,
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )

        context = await browser.new_context(
            viewport={"width": 1200, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
        )

        # Inject stealth scripts
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => false});
            Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});
            Object.defineProperty(navigator, 'vendor', {get: () => 'Google Inc.'});
            delete navigator.__proto__.webdriver;
        """)

        # Load cookies from ego-lite
        await load_cookies(context)

        page = await context.new_page()

        # Navigate to survey
        print(f"Navigating to: {survey_url}")
        await page.goto(survey_url, wait_until="networkidle", timeout=30000)

        # Check if logged in
        body = await page.inner_text("body")
        if "Antonio" in body:
            print("✅ Logged in as Antonio Reid")
        else:
            print("❌ Not logged in!")
            await browser.close()
            return

        # Find and click TAKE SURVEY
        take_btn = await page.query_selector('a:text("TAKE SURVEY")')
        if take_btn:
            href = await take_btn.get_attribute("href")
            print(f"Clicking TAKE SURVEY: {href}")
            await take_btn.click()
            await page.wait_for_load_state("networkidle", timeout=15000)

        # Auto-answer loop
        completed = False
        max_steps = 100
        step = 0

        while not completed and step < max_steps:
            step += 1
            await asyncio.sleep(1)

            body = await page.inner_text("body")
            url = page.url

            # Check completion
            if "completed" in body.lower() and "thank you" in body.lower():
                print(f"✅ SURVEY COMPLETED at step {step}!")
                completed = True
                break

            # Check disqualification
            if "do not qualify" in body.lower() or "disqualified" in body.lower():
                print(f"❌ Disqualified at step {step}")
                break

            # Check for survey full
            if "survey full" in body.lower():
                print(f"❌ Survey full at step {step}")
                break

            # Get progress
            import re
            progress = re.search(r"(\d+)%", body)
            pct = progress.group(1) + "%" if progress else "?"

            # Auto-answer based on page content
            result = await auto_answer_page(page, body)
            print(f"Step {step} [{pct}]: {result}")

            if result == "DONE" or result == "DQ":
                break

        if not completed and step >= max_steps:
            print(f"⚠️ Reached max steps ({max_steps})")

        await browser.close()
        return completed


async def load_cookies(context):
    """Load cookies from ego-lite's Chrome profile."""
    src = "/Users/antonioreid/Library/Application Support/Citro Labs/ego lite/Default/Cookies"
    dst = "/tmp/ego_cookies_playwright"

    try:
        shutil.copy2(src, dst)
    except Exception as e:
        print(f"Warning: Could not copy cookies: {e}")
        return

    try:
        conn = sqlite3.connect(dst)
        cursor = conn.cursor()
        cursor.execute("SELECT name, value, host_key, path, is_secure, is_httponly, expires_utc FROM cookies")

        cookies = []
        for row in cursor.fetchall():
            cookie = {
                "name": row[0],
                "value": row[1],
                "domain": row[2],
                "path": row[3],
                "secure": bool(row[4]),
                "httpOnly": bool(row[5]),
            }
            # Chrome stores expiry as microseconds since 1601-01-01
            if row[6] and row[6] > 0:
                cookie["expires"] = (row[6] / 1000000) - 11644473600
            cookies.append(cookie)

        conn.close()

        await context.add_cookies(cookies)
        print(f"✅ Loaded {len(cookies)} cookies from ego-lite")
    except Exception as e:
        print(f"Warning: Could not load cookies: {e}")


async def auto_answer_page(page, body: str) -> str:
    """Auto-answer the current page based on content analysis."""
    body_lower = body.lower()

    # Handle consent/forward pages
    if "forward" in body_lower and len(body_lower) < 500:
        forward = await page.query_selector('input[value="Forward"]')
        if forward:
            await forward.click()
            await page.wait_for_load_state("networkidle", timeout=10000)
            return "Clicked Forward"

    # Handle text areas
    textareas = await page.query_selector_all("textarea")
    if textareas:
        for ta in textareas:
            value = await ta.input_value()
            if not value:
                answer = generate_text_answer(body_lower)
                await ta.fill(answer)
                await ta.dispatch_event("change")

                # Uncheck "not aware" checkbox
                checkboxes = await page.query_selector_all('input[type="checkbox"]')
                for cb in checkboxes:
                    checked = await cb.is_checked()
                    if checked:
                        label = await cb.evaluate("el => el.parentElement.textContent")
                        if "not aware" in label.lower():
                            await cb.uncheck()

                return "Filled textarea"

    # Handle radio buttons
    radios = await page.query_selector_all('input[type="radio"]')
    if radios:
        # Check if any are already checked
        checked = [r for r in radios if await r.is_checked()]
        if not checked:
            # Click first radio (usually "Yes" or first option)
            await radios[0].click()
            return "Selected first radio"

    # Handle spinbuttons (age)
    spinbuttons = await page.query_selector_all('input[type="number"], [role="spinbutton"]')
    if spinbuttons:
        for sb in spinbuttons:
            value = await sb.input_value()
            if not value:
                await sb.fill("39")
                return "Filled age: 39"

    # Handle select dropdowns
    selects = await page.query_selector_all("select")
    if selects:
        for sel in selects:
            value = await sel.input_value()
            if not value:
                # Select first non-empty option
                options = await sel.query_selector_all("option")
                for opt in options:
                    opt_value = await opt.get_attribute("value")
                    if opt_value:
                        await sel.select_option(opt_value)
                        return f"Selected dropdown: {opt_value}"

    # Handle checkboxes (select first 2-3)
    checkboxes = await page.query_selector_all('input[type="checkbox"]')
    if checkboxes:
        checked = [c for c in checkboxes if await c.is_checked()]
        if not checked and len(checkboxes) > 1:
            for i, cb in enumerate(checkboxes[:2]):
                label = await cb.evaluate("el => el.parentElement.textContent")
                if "not aware" not in label.lower():
                    await cb.check()
            return "Checked 2 checkboxes"

    # Submit form
    form = await page.query_selector("form")
    if form:
        await form.evaluate("form => form.submit()")
        await asyncio.sleep(2)
        return "Submitted form"

    # Click Continue/Forward button
    for selector in [
        'button:text("Continue")',
        'input[value="Forward"]',
        'button:text("Forward")',
        'button:text("Next")',
        'input[value="Continue"]',
    ]:
        btn = await page.query_selector(selector)
        if btn:
            await btn.click()
            await asyncio.sleep(2)
            return "Clicked button"

    return "No action taken"


def generate_text_answer(body: str) -> str:
    """Generate a text answer based on the question context."""
    if "weight" in body and "heard" in body:
        return "I have heard about weight loss medications through television commercials, social media, and conversations with friends. They seem to be effective options for people struggling with weight management."
    if "weight" in body:
        return "I am interested in weight management options that are safe and effective. I have tried diet and exercise but have not achieved my goals."
    if "heard" in body:
        return "I have heard about these medications through various sources including television advertisements and online research. They appear to be effective treatment options."
    if "side effect" in body:
        return "I understand that all medications can have side effects. I would discuss any concerns with my healthcare provider."
    if "reason" in body:
        return "I take surveys to share my opinions and earn rewards. I find it interesting to learn about new products and services."
    return "I have some knowledge about this topic from general reading and conversations with others."


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python survey_playwright.py <survey_url>")
        sys.exit(1)

    asyncio.run(run_survey(sys.argv[1]))
