"""Diagnostic: inspect a survey provider page without interacting.

Clicks a tile, captures qualification, opens the URL, waits 15s for
SPA hydration, then dumps:
- URL, title, body text
- All frames and their URLs
- Full page HTML (first 5000 chars)
- All form elements inside each frame
- Screenshot

Does NOT fill, click, or submit anything.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from survey_agent.browser import create_backend

PROFILE_DIR = Path("data/camoufox_session/profile")
SS_DIR = Path("data/smoke")


def log(msg):
    print(f"[diag] {msg}", flush=True)


async def main():
    backend = create_backend("camoufox", headless=True, persistent=True,
                             user_data_dir=PROFILE_DIR, humanize=False, geoip=False)
    await backend.start()
    page = await backend.get_page()
    raw = page._page

    # Load home
    log("[1] Loading PO home...")
    await page.goto("https://app.primeopinion.com/", wait_until="domcontentloaded")
    await asyncio.sleep(5)

    # Dismiss device modal if present
    try:
        modal_present = await page.evaluate(
            "() => document.body.innerText.includes('Select Devices')"
        )
        if modal_present:
            log("[2] Dismissing device modal...")
            checkbox = raw.locator('input[type="checkbox"]').first
            if await checkbox.is_visible(timeout=3000):
                await checkbox.click(timeout=3000)
                await asyncio.sleep(0.5)
            save = raw.locator("button").filter(has_text="Save Selection")
            if await save.first.is_visible(timeout=3000):
                await save.first.click(timeout=3000)
                await asyncio.sleep(2)
    except Exception as e:
        log(f"[2] Modal handling: {e}")

    # Click first tile + capture qualification
    log("[3] Clicking survey tile...")
    qualification = None

    try:
        async with raw.expect_response(
            lambda resp: "qualification" in resp.url, timeout=15000
        ) as resp_info:
            await raw.locator(".list-item.survey-item").first.scroll_into_view_if_needed(timeout=5000)
            await raw.locator(".list-item.survey-item").first.click(timeout=10000)

        response = await resp_info.value
        qualification = await response.json()
        status = qualification.get("status")
        survey_url = qualification.get("url")
        log(f"    Status: {status}")
        log(f"    URL: {survey_url[:100] if survey_url else 'None'}")

        if not survey_url:
            log("ERROR: No URL in qualification response!")
            log(f"    Full response: {json.dumps(qualification)[:300]}")
            await backend.stop()
            return
    except Exception as e:
        log(f"ERROR: {str(e)[:100]}")
        await backend.stop()
        return

    # Open survey in new page
    log("[4] Opening survey in new page...")
    new_page = None
    pages_before = set(raw.context.pages)
    await page.evaluate(f'window.open("{survey_url}", "_blank")')

    for _ in range(30):
        new_pages = set(raw.context.pages) - pages_before
        if new_pages:
            new_page = new_pages.pop()
            break
        await asyncio.sleep(0.5)

    if not new_page:
        log("    No popup — using context.new_page()")
        new_page = await raw.context.new_page()
        await new_page.goto(survey_url, wait_until="domcontentloaded", timeout=60000)

    # Wait 15s for SPA hydration
    log("[5] Waiting 15s for SPA hydration...")
    try:
        await new_page.wait_for_load_state("networkidle", timeout=60000)
    except Exception:
        pass
    await asyncio.sleep(15)

    # Dump page state
    log("[6] Page state:")
    current_url = new_page.url
    title = await new_page.title()
    log(f"    URL: {current_url[:100]}")
    log(f"    Title: {title}")

    # Body text
    body_text = await new_page.evaluate(
        "() => document.body?.innerText?.substring(0, 3000) || 'EMPTY'"
    )
    log(f"    Body text ({len(body_text)} chars):")
    log(f"    {body_text[:1000]}")

    # All frames
    log("[7] Frames:")
    for i, frame in enumerate(new_page.frames):
        f_url = frame.url
        log(f"    Frame[{i}]: {f_url[:120]}")
        if i > 0:  # Skip main frame
            try:
                f_body = await frame.evaluate(
                    "() => document.body?.innerText?.substring(0, 2000) || ''"
                )
                f_forms = await frame.evaluate("""() => ({
                    radios: document.querySelectorAll('input[type="radio"]').length,
                    checkboxes: document.querySelectorAll('input[type="checkbox"]').length,
                    texts: document.querySelectorAll('input[type="text"], textarea').length,
                    selects: document.querySelectorAll('select').length,
                    buttons: [...document.querySelectorAll('button, input[type="submit"], a.btn, [role="button"]')]
                        .map(b => b.textContent?.trim() || b.value || '')
                        .filter(t => t)
                        .slice(0, 10)
                })""")
                log(f"      Body ({len(f_body)} chars): {f_body[:500]}")
                log(f"      Forms: {json.dumps(f_forms)}")
            except Exception as e:
                log(f"      Error: {str(e)[:100]}")

    # Main frame form elements
    log("[8] Main frame form elements:")
    main_forms = await new_page.evaluate("""() => ({
        radios: document.querySelectorAll('input[type="radio"]').length,
        checkboxes: document.querySelectorAll('input[type="checkbox"]').length,
        texts: document.querySelectorAll('input[type="text"], textarea').length,
        selects: document.querySelectorAll('select').length,
        buttons: [...document.querySelectorAll('button, input[type="submit"], [role="button"]')]
            .map(b => b.textContent?.trim() || b.value || '')
            .filter(t => t)
            .slice(0, 10),
        iframes: document.querySelectorAll('iframe').length
    })""")
    log(f"    {json.dumps(main_forms)}")

    # Full HTML preview
    log("[9] HTML preview (first 2000 chars):")
    html = await new_page.evaluate("() => document.body?.innerHTML?.substring(0, 2000) || ''")
    log(f"    {html[:2000]}")

    # Screenshot
    await new_page.screenshot(path=str(SS_DIR / "survey_page_debug.png"))
    log("[10] Screenshot: data/smoke/survey_page_debug.png")

    await new_page.close()
    await backend.stop()
    log("[DONE]")


if __name__ == "__main__":
    asyncio.run(main())
