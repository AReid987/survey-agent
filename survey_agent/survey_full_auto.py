#!/usr/bin/env python3
"""Full survey automation: Playwright for questions + PyAutoGUI for drag-and-drop.

No cua-driver dependency. Runs as a continuous loop.
Uses Playwright with stealth for login + question answering.
Uses PyAutoGUI for OS-level drag-and-drop operations.
"""

import asyncio
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

# PyAutoGUI for drag-and-drop
try:
    import pyautogui
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0.1
except ImportError:
    pyautogui = None

# Profile data
PROFILE = {
    "age": "39",
    "gender": "Male",
    "state": "Texas",
    "zip": "78701",
    "income_household": "$60,000 ~ $79,999",
    "income_individual": "$40,000 ~ $59,999",
    "race": ["Black or African American"],
    "employment": "Self-employed",
    "marital": "Single",
    "education": "Bachelor's degree",
    "children": "3",
    "smoker": "Former smoker",
    "drinker": "Never",
}


async def login_branded(page):
    """Login to Branded Surveys."""
    print("Logging in to Branded Surveys...")
    await page.goto("https://surveys.gobranded.com/users/login/", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(2000)
    
    # Close cookie banner if present
    try:
        close_btn = page.locator('button.banner-close-button, button:has-text("×")')
        if await close_btn.count() > 0:
            await close_btn.first.click()
            await page.wait_for_timeout(500)
    except Exception:
        pass
    
    # Fill email
    email_input = page.locator('#UserEmail, input[name="email"], input[type="email"]')
    await email_input.fill("")
    await email_input.type("read.musik@gmail.com", delay=30)
    
    # Fill password
    pwd_input = page.locator('#UserPassword, input[name="password"], input[type="password"]')
    await pwd_input.fill("")
    await pwd_input.type("Th3wayto12!", delay=30)
    
    # Click sign in
    submit_btn = page.locator('#_evh-ric-c, button:has-text("Sign in"), button[type="submit"]')
    await submit_btn.click()
    
    # Wait for dashboard
    await page.wait_for_timeout(5000)
    
    url = page.url
    title = await page.title()
    print(f"  After login: {url} | {title}")
    
    if "dashboard" in url.lower() or "dashboard" in title.lower():
        print("  Login SUCCESS")
        return True
    
    # Check if already logged in
    if "login" not in url.lower():
        print("  Login likely success (redirected)")
        return True
    
    print("  Login FAILED")
    return False


async def get_survey_urls(page):
    """Get survey URLs from the dashboard."""
    print("Getting survey URLs from dashboard...")
    await page.goto("https://surveys.gobranded.com/users/dashboard", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)
    
    # Find survey links
    links = await page.eval_on_selector_all(
        'a[href*="/surveys/pre/"]',
        'els => els.map(e => e.href)'
    )
    
    print(f"  Found {len(links)} survey URLs")
    return links


async def take_survey(page, url):
    """Navigate to survey pre-page and click TAKE SURVEY."""
    print(f"  Navigating to survey: {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)
    
    title = await page.title()
    print(f"  Page: {title}")
    
    # Click TAKE SURVEY
    take_btn = page.locator('a:has-text("TAKE SURVEY"), button:has-text("TAKE SURVEY")')
    if await take_btn.count() > 0:
        await take_btn.first.click()
        await page.wait_for_timeout(8000)
        print(f"  Clicked TAKE SURVEY, now at: {page.url}")
        return True
    
    print("  No TAKE SURVEY button found")
    return False


async def click_begin_survey(page):
    """Click BEGIN SURVEY on the survey offer page."""
    begin_btn = page.locator('a:has-text("BEGIN SURVEY"), button:has-text("BEGIN SURVEY")')
    if await begin_btn.count() > 0:
        await begin_btn.first.click()
        await page.wait_for_timeout(5000)
        print("  Clicked BEGIN SURVEY")
        return True
    return False


async def auto_answer_page(page):
    """Auto-answer all question types on the current page.
    
    Returns True if a question was answered, False if no question found.
    """
    url = page.url
    
    # Check for disqualification / completion
    content = await page.content()
    lower_content = content.lower()
    
    if "not a match" in lower_content or "disqualified" in lower_content:
        print("    -> NOT A MATCH / disqualified")
        return "disqualified"
    
    if "thank you" in lower_content and "complete" in lower_content:
        print("    -> SURVEY COMPLETED!")
        return "completed"
    
    if "you have earned" in lower_content or "points earned" in lower_content:
        print("    -> EARNED POINTS!")
        return "completed"
    
    # Try to auto-answer via JS
    answered = await page.evaluate('''() => {
        let answered = false;
        
        // 1. Radio buttons - click first option (Yes) or specific profile match
        const radios = document.querySelectorAll('input[type="radio"]');
        const labels = document.querySelectorAll('label');
        
        if (radios.length > 0) {
            // Find "Male" label
            for (const label of labels) {
                const text = label.textContent.trim();
                if (text === "Male" || text === "Yes") {
                    label.click();
                    answered = true;
                    break;
                }
            }
            // If no match, click first radio's label
            if (!answered && radios.length > 0) {
                const firstRadio = radios[0];
                const id = firstRadio.id;
                if (id) {
                    const label = document.querySelector(`label[for="${id}"]`);
                    if (label) {
                        label.click();
                        answered = true;
                    }
                }
                if (!answered) {
                    firstRadio.click();
                    answered = true;
                }
            }
        }
        
        // 2. Text inputs - fill age, zip, etc.
        const textInputs = document.querySelectorAll('input[type="text"], input[type="number"], input:not([type])');
        for (const input of textInputs) {
            const name = (input.name || "").toLowerCase();
            const id = (input.id || "").toLowerCase();
            const placeholder = (input.placeholder || "").toLowerCase();
            
            if (name.includes("age") || id.includes("age") || placeholder.includes("age")) {
                input.value = "39";
                input.dispatchEvent(new Event("input", {bubbles: true}));
                input.dispatchEvent(new Event("change", {bubbles: true}));
                answered = true;
            } else if (name.includes("zip") || id.includes("zip") || placeholder.includes("zip")) {
                input.value = "78701";
                input.dispatchEvent(new Event("input", {bubbles: true}));
                input.dispatchEvent(new Event("change", {bubbles: true}));
                answered = true;
            }
        }
        
        // 3. Select dropdowns
        const selects = document.querySelectorAll("select");
        for (const sel of selects) {
            const name = (sel.name || "").toLowerCase();
            const id = (sel.id || "").toLowerCase();
            
            if (name.includes("state") || id.includes("state")) {
                for (const opt of sel.options) {
                    if (opt.text === "Texas" || opt.value === "TX") {
                        sel.value = opt.value;
                        sel.dispatchEvent(new Event("change", {bubbles: true}));
                        answered = true;
                        break;
                    }
                }
            }
        }
        
        // 4. Click Continue / Next button
        const continueBtns = document.querySelectorAll(
            'button:has-text("Continue"), a:has-text("Continue"), input[type="submit"], ' +
            'button:has-text("Next"), a:has-text("Next"), button:has-text(">>")'
        );
        // Also try by text content
        const allButtons = document.querySelectorAll("button, a, input[type='submit']");
        for (const btn of allButtons) {
            const text = btn.textContent.trim();
            if (text.includes("Continue") || text === "Next" || text === ">>") {
                btn.click();
                break;
            }
        }
        
        return answered;
    }''')
    
    if answered:
        await page.wait_for_timeout(3000)
        return "answered"
    
    return "no_question"


async def detect_drag_and_drop(page):
    """Check if the current page has a drag-and-drop question."""
    content = await page.content()
    lower = content.lower()
    
    has_dnd = ("cdk-drag" in lower or "cdk-drop" in lower or 
               "yui3-dd" in lower or "draggable" in lower or
               "drag" in lower and "drop" in lower and "rank" in lower)
    
    return has_dnd


async def handle_drag_and_drop(page):
    """Handle drag-and-drop using PyAutoGUI foreground mouse events."""
    if pyautogui is None:
        print("    -> PyAutoGUI not available, cannot do drag-and-drop")
        return False
    
    # Get drag-and-drop elements from the page
    dnd_info = await page.evaluate('''() => {
        // Find draggable elements and drop targets
        const draggables = document.querySelectorAll('[cdkdrag], [cdkDrag], .cdk-drag, [draggable="true"], .yui3-dd-draggable');
        const dropLists = document.querySelectorAll('[cdkdroplist], [cdkDropList], .cdk-drop-list, .yui3-dd-drop');
        
        const items = [];
        for (const el of draggables) {
            const rect = el.getBoundingClientRect();
            items.push({
                text: el.textContent.trim().substring(0, 50),
                x: rect.x + rect.width / 2,
                y: rect.y + rect.height / 2,
                width: rect.width,
                height: rect.height
            });
        }
        
        const targets = [];
        for (const el of dropLists) {
            const rect = el.getBoundingClientRect();
            targets.push({
                text: el.textContent.trim().substring(0, 50),
                x: rect.x + rect.width / 2,
                y: rect.y + rect.height / 2,
                width: rect.width,
                height: rect.height
            });
        }
        
        return {items, targets};
    }''')
    
    items = dnd_info.get("items", [])
    targets = dnd_info.get("targets", [])
    
    if not items:
        print("    -> No draggable elements found")
        return False
    
    print(f"    -> Found {len(items)} draggable items, {len(targets)} drop targets")
    
    # Get the viewport position in screen coordinates
    # The ego-lite window is at (0, 34) with web area at (8, 129)
    # We need to convert page coordinates to screen coordinates
    window_x, window_y = 0, 34  # ego-lite window position
    chrome_height = 129 - 34  # toolbar height = 95px
    web_area_x = 8
    
    for i, item in enumerate(items):
        screen_x = window_x + web_area_x + item["x"]
        screen_y = window_y + chrome_height + item["y"]
        
        if i < len(targets):
            target = targets[i]
            target_screen_x = window_x + web_area_x + target["x"]
            target_screen_y = window_y + chrome_height + target["y"]
            
            print(f"    -> Dragging '{item['text'][:30]}' to target '{target['text'][:30]}'")
            print(f"       From ({screen_x:.0f}, {screen_y:.0f}) to ({target_screen_x:.0f}, {target_screen_y:.0f})")
            
            # Perform foreground drag with PyAutoGUI
            pyautogui.moveTo(int(screen_x), int(screen_y))
            time.sleep(0.2)
            pyautogui.mouseDown()
            time.sleep(0.3)
            # Move in steps for Angular CDK
            steps = 10
            for step in range(1, steps + 1):
                ix = int(screen_x + (target_screen_x - screen_x) * step / steps)
                iy = int(screen_y + (target_screen_y - screen_y) * step / steps)
                pyautogui.moveTo(ix, iy)
                time.sleep(0.05)
            time.sleep(0.3)
            pyautogui.mouseUp()
            time.sleep(0.5)
    
    print("    -> Drag-and-drop completed")
    return True


async def run_survey_loop():
    """Main survey automation loop."""
    from playwright.async_api import async_playwright
    
    print("=" * 60)
    print("SURVEY AUTOMATION LOOP")
    print("Playwright for questions + PyAutoGUI for drag-and-drop")
    print("=" * 60)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ]
        )
        
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        
        # Apply stealth
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});
            Object.defineProperty(navigator, 'vendor', {get: () => 'Google Inc.'});
        """)
        
        page = await context.new_page()
        
        # Login
        logged_in = await login_branded(page)
        if not logged_in:
            print("Login failed, exiting")
            await browser.close()
            return
        
        surveys_attempted = 0
        surveys_completed = 0
        total_points = 0
        
        while surveys_attempted < 10:
            # Get survey URLs
            urls = await get_survey_urls(page)
            if not urls:
                print("No surveys available, waiting 5 min...")
                await asyncio.sleep(300)
                continue
            
            for url in urls:
                surveys_attempted += 1
                print(f"\n--- Survey #{surveys_attempted} ---")
                
                # Take survey
                taken = await take_survey(page, url)
                if not taken:
                    continue
                
                # Click BEGIN SURVEY if on offer page
                await click_begin_survey(page)
                
                # Answer questions in a loop
                max_questions = 100
                for q in range(max_questions):
                    # Check for DnD
                    has_dnd = await detect_drag_and_drop(page)
                    if has_dnd:
                        print(f"    Q{q+1}: DRAG-AND-DETECTED! Using PyAutoGUI...")
                        success = await handle_drag_and_drop(page)
                        if success:
                            await page.wait_for_timeout(2000)
                            continue
                    
                    # Auto-answer
                    result = await auto_answer_page(page)
                    
                    if result == "disqualified":
                        print(f"    Q{q+1}: Disqualified")
                        break
                    elif result == "completed":
                        surveys_completed += 1
                        print(f"    Q{q+1}: COMPLETED! Total completed: {surveys_completed}")
                        break
                    elif result == "answered":
                        print(f"    Q{q+1}: Answered")
                        await page.wait_for_timeout(2000)
                    elif result == "no_question":
                        print(f"    Q{q+1}: No question found, checking page...")
                        await page.wait_for_timeout(2000)
                        # Try clicking any visible continue/next button
                        btn = page.locator('button:has-text("Continue"), a:has-text("Continue"), button:has-text("Next")')
                        if await btn.count() > 0:
                            await btn.first.click()
                            await page.wait_for_timeout(3000)
                        else:
                            print(f"    Q{q+1}: Stuck, moving to next survey")
                            break
                
                # Go back to dashboard for next survey
                await page.goto("https://surveys.gobranded.com/users/dashboard", wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)
                
                # Random delay between surveys
                delay = random.randint(30, 90)
                print(f"  Waiting {delay}s before next survey...")
                await asyncio.sleep(delay)
        
        print(f"\n{'='*60}")
        print(f"RESULTS: {surveys_completed}/{surveys_attempted} surveys completed")
        print(f"{'='*60}")
        
        await browser.close()


if __name__ == "__main__":
    asyncio.run(run_survey_loop())
