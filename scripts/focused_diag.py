"""Focused diagnostic: capture full qualification JSON + PO page state.

Clicks ONE tile, dumps the COMPLETE qualification response (no truncation),
screenshots the PO page, checks for modals/forms, and lists all network
requests for 10 seconds. Does NOT navigate, fill, or submit.
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

    # Track all network requests after click
    post_click_requests = []

    async def on_resp(resp):
        post_click_requests.append({
            "method": resp.request.method if hasattr(resp, 'request') else "?",
            "url": resp.url[:150],
            "status": resp.status
        })

    # Load home
    log("[1] Loading PO home...")
    await page.goto("https://app.primeopinion.com/", wait_until="domcontentloaded")
    await asyncio.sleep(5)

    # Dismiss device modal
    try:
        modal = await page.evaluate("() => document.body.innerText.includes('Select Devices')")
        if modal:
            log("[2] Dismissing device modal...")
            cb = raw.locator('input[type="checkbox"]').first
            if await cb.is_visible(timeout=3000):
                await cb.click(timeout=3000)
            save = raw.locator("button").filter(has_text="Save Selection")
            if await save.first.is_visible(timeout=3000):
                await save.first.click(timeout=3000)
                await asyncio.sleep(2)
    except Exception as e:
        log(f"[2] Modal: {e}")

    # Count tiles
    tile_count = await page.evaluate('() => document.querySelectorAll(".list-item.survey-item").length')
    log(f"[3] Survey tiles: {tile_count}")

    if tile_count == 0:
        log("ERROR: No tiles!")
        await backend.stop()
        return

    # Click ONE tile, capture FULL qualification response
    log("[4] Clicking first tile...")
    qualification = None

    raw.on("response", lambda r: asyncio.ensure_future(on_resp(r)))

    try:
        async with raw.expect_response(
            lambda resp: "qualification" in resp.url, timeout=15000
        ) as resp_info:
            await raw.locator(".list-item.survey-item").first.scroll_into_view_if_needed(timeout=5000)
            await raw.locator(".list-item.survey-item").first.click(timeout=10000)

        response = await resp_info.value
        qualification = await response.json()
        log("[5] FULL QUALIFICATION RESPONSE:")
        log(json.dumps(qualification, indent=2))
    except Exception as e:
        log(f"ERROR capturing qualification: {str(e)[:200]}")
        await backend.stop()
        return

    # Wait a few seconds for any UI changes
    log("[6] Waiting 5s for UI to react...")
    await asyncio.sleep(5)

    # Screenshot the PO page AFTER the click
    await page.screenshot(path=str(SS_DIR / "after_tile_click.png"))
    log("  Screenshot: data/smoke/after_tile_click.png")

    # Check for modals, overlays, question UI
    ui_state = await page.evaluate("""() => {
        const body = document.body.innerText;
        const hasModal = !!document.querySelector('[role="dialog"], .p-modal, .modal, [class*="Modal"], [class*="overlay"]');
        const modals = [...document.querySelectorAll('[role="dialog"], .p-modal, .modal, [class*="Modal"], [class*="overlay"]')];
        const modalTexts = modals.map(m => m.innerText.trim().substring(0, 300)).filter(t => t);

        // Check for radio buttons, checkboxes, text inputs that appeared
        const radios = document.querySelectorAll('input[type="radio"]').length;
        const checkboxes = document.querySelectorAll('input[type="checkbox"]').length;
        const textInputs = document.querySelectorAll('input[type="text"], textarea').length;
        const selects = document.querySelectorAll('select').length;
        const allButtons = [...document.querySelectorAll('button')].map(b => b.textContent.trim()).filter(t => t);

        // Check if body text changed (new content appeared)
        const newContent = body.substring(0, 1000);

        return {
            hasModal, modalTexts, radios, checkboxes, textInputs, selects,
            buttons: allButtons.slice(0, 15), bodyPreview: newContent
        };
    }""")

    log(f"[7] UI state after click:")
    log(f"  Modal present: {ui_state['hasModal']}")
    log(f"  Modal texts: {ui_state['modalTexts'][:3]}")
    log(f"  Radios: {ui_state['radios']}, Checkboxes: {ui_state['checkboxes']}, TextInputs: {ui_state['textInputs']}, Selects: {ui_state['selects']}")
    log(f"  Buttons: {ui_state['buttons']}")
    log(f"  Body preview: {ui_state['bodyPreview'][:500]}")

    # Network requests captured
    log(f"[8] Network requests after click ({len(post_click_requests)}):")
    for r in post_click_requests[:20]:
        log(f"  {r['method']} {r['url']} → {r['status']}")

    # Check qualification status details
    status = qualification.get("status")
    questions = qualification.get("questions", {})
    current_q = questions.get("current")
    if current_q:
        log(f"[9] Current question from API:")
        log(f"  ID: {current_q.get('id')}")
        log(f"  Question: {current_q.get('question')}")
        log(f"  Type: {current_q.get('type')}")
        log(f"  Skippable: {current_q.get('is_skippable')}")
        answers = current_q.get("answers", [])
        log(f"  Answers ({len(answers)}):")
        for a in answers[:10]:
            log(f"    - {a.get('id')}: {a.get('text', a.get('label', a.get('value', '?')))}")
    else:
        log(f"[9] No current question in response")
        url = qualification.get("url")
        if url:
            log(f"  URL: {url[:100]}")

    await backend.stop()
    log("[DONE]")

if __name__ == "__main__":
    asyncio.run(main())
