"""Robust answer gate with wait_for_response and modal diagnostics.

Fixes:
- Uses page.wait_for_response (Playwright-native, race-free) instead of
  ensure_future + polling loop
- Verbose modal detection and dismissal with verification
- Logs every network response for debugging
- Checks balance before AND after
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
    print(f"[gate] {msg}", flush=True)

async def get_balance(page) -> float | None:
    data = await page.evaluate("""() => {
        const el = document.querySelector('.user-balance');
        if (!el) return null;
        const m = el.innerText.match(/\\$\\s*([\\d.]+)/);
        return m ? m[1] : null;
    }""")
    return float(data) if data else None

async def main():
    backend = create_backend("camoufox", headless=True, persistent=True,
                             user_data_dir=PROFILE_DIR, humanize=False, geoip=False)
    await backend.start()
    page = await backend.get_page()
    raw = page._page

    # ── Step 1: Load home + check balance ──
    log("Step 1: Loading PO home...")
    await page.goto("https://app.primeopinion.com/", wait_until="domcontentloaded")
    await asyncio.sleep(5)
    start_balance = await get_balance(page)
    log(f"Starting balance: ${start_balance}")

    # ── Step 2: Modal detection with full diagnostics ──
    log("Step 2: Checking device modal...")
    modal_info = await page.evaluate("""() => {
        const text = document.body.innerText;
        const hasModal = text.includes('Select Devices') && text.includes('devices do you own');
        // Find modal container
        const modal = document.querySelector('[class*="modal"], [class*="Modal"], [role="dialog"]');
        // Find all checkboxes
        const checkboxes = [...document.querySelectorAll('input[type="checkbox"]')].map(c => ({
            id: c.id, name: c.name, checked: c.checked, cls: c.className
        }));
        // Find all buttons with Save text
        const saveBtns = [...document.querySelectorAll('button')].filter(b => 
            b.textContent.trim().toLowerCase().includes('save')
        ).map(b => ({
            text: b.textContent.trim(), id: b.id, cls: b.className.substring(0, 60)
        }));
        return {hasModal, modalExists: !!modal, checkboxes, saveBtns, bodyPreview: text.substring(0, 300)};
    }""")
    log(f"  Modal present: {modal_info['hasModal']}")
    log(f"  Modal element exists: {modal_info['modalExists']}")
    log(f"  Checkboxes: {json.dumps(modal_info['checkboxes'])}")
    log(f"  Save buttons: {json.dumps(modal_info['saveBtns'])}")

    if modal_info['hasModal']:
        log("  Attempting modal dismissal...")
        
        # Strategy 1: Playwright native locator for Desktop text
        try:
            desktop = raw.get_by_text("Desktop", exact=True)
            vis = await desktop.is_visible(timeout=3000)
            log(f"    Desktop visible: {vis}")
            if vis:
                await desktop.click(timeout=3000)
                log("    ✓ Desktop clicked (Playwright locator)")
                await asyncio.sleep(0.5)
        except Exception as e:
            log(f"    Desktop locator failed: {str(e)[:80]}")

        # Strategy 2: Find and click checkbox
        try:
            cb = raw.locator('input[type="checkbox"]').first
            vis = await cb.is_visible(timeout=2000)
            log(f"    Checkbox visible: {vis}")
            if vis:
                await cb.click(timeout=3000)
                log("    ✓ Checkbox clicked")
                await asyncio.sleep(0.5)
        except Exception as e:
            log(f"    Checkbox failed: {str(e)[:80]}")

        # Strategy 3: Click Save button
        try:
            save = raw.locator("button").filter(has_text="Save Selection")
            vis = await save.first.is_visible(timeout=3000)
            log(f"    Save Selection visible: {vis}")
            if vis:
                await save.first.click(timeout=3000)
                log("    ✓ Save Selection clicked")
                await asyncio.sleep(3)
            else:
                # Fallback: any Save button
                save2 = raw.locator("button").filter(has_text="Save")
                vis2 = await save2.first.is_visible(timeout=2000)
                log(f"    Save (generic) visible: {vis2}")
                if vis2:
                    await save2.first.click(timeout=3000)
                    log("    ✓ Save (generic) clicked")
                    await asyncio.sleep(3)
        except Exception as e:
            log(f"    Save button failed: {str(e)[:80]}")

        # Verify modal dismissed
        modal_gone = await page.evaluate("() => !document.body.innerText.includes('Select Devices')")
        log(f"  Modal dismissed: {modal_gone}")
    else:
        log("  No modal present — skipping")

    # ── Step 3: Verify tiles are visible and clickable ──
    log("Step 3: Checking survey tiles...")
    tile_count = await page.evaluate('() => document.querySelectorAll(".list-item.survey-item").length')
    first_tile_text = await page.evaluate('() => document.querySelector(".list-item.survey-item")?.innerText?.trim()?.substring(0, 80) || "NO TILE"')
    log(f"  Tile count: {tile_count}")
    log(f"  First tile text: {first_tile_text}")

    if tile_count == 0:
        log("ERROR: No survey tiles found!")
        await page.screenshot(path=str(SS_DIR / "debug_no_tiles.png"))
        await backend.stop()
        return

    # ── Step 4: Iterate tiles until one returns a qualified URL ──
    log("Step 4: Finding a fresh survey tile...")

    qualification = None
    survey_url = None
    tiles_per_batch = 5  # Keep small to avoid timeout
    num_batches = 2

    for attempt in range(num_batches):
        if attempt > 0:
            log(f"  Reloading page to refresh tiles...")
            await page.goto("https://app.primeopinion.com/", wait_until="domcontentloaded")
            await asyncio.sleep(5)
            tile_count = await page.evaluate('() => document.querySelectorAll(".list-item.survey-item").length')
            log(f"  Tiles after reload: {tile_count}")

        tiles = raw.locator(".list-item.survey-item")

        for tile_idx in range(tiles_per_batch):
            log(f"  Trying tile {tile_idx}...")
            locator = tiles.nth(tile_idx)

            try:
                await locator.scroll_into_view_if_needed(timeout=3000)

                async with raw.expect_response(
                    lambda resp: "qualification" in resp.url,
                    timeout=8000
                ) as resp_info:
                    await locator.click(timeout=5000)

                response = await resp_info.value
                qualification = await response.json()
                status = qualification.get("status")
                survey_url = qualification.get("url")
                log(f"    Status: {status}")

                if status == "qualified" and survey_url:
                    log(f"    ✓ Qualified! URL: {survey_url[:80]}...")
                    break
                elif status == "accepted":
                    survey_url = None
                    await asyncio.sleep(1)
                    continue
                else:
                    survey_url = None
                    await asyncio.sleep(1)
                    continue

            except Exception as e:
                log(f"    No response: {str(e)[:60]}")
                await asyncio.sleep(1)
                continue

        if survey_url:
            break

    if not survey_url:
        log(f"ERROR: No qualified survey found after {tiles_per_batch} tiles × {num_batches} batches!")
        await page.screenshot(path=str(SS_DIR / "debug_no_qual.png"))
        await backend.stop()
        return

    # ── Step 5: Open survey URL in new page ──
    log("Step 5: Opening survey in new page...")
    pages_before = set(raw.context.pages)
    await page.evaluate(f'window.open("{survey_url}", "_blank")')

    new_page = None
    for _ in range(30):
        new_pages = set(raw.context.pages) - pages_before
        if new_pages:
            new_page = new_pages.pop()
            break
        await asyncio.sleep(0.5)

    if not new_page:
        log("  No popup — using context.new_page() fallback")
        new_page = await raw.context.new_page()
        await new_page.goto(survey_url, wait_until="domcontentloaded", timeout=60000)

    log("  Waiting for load...")
    try:
        await new_page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass
    await asyncio.sleep(5)

    # ── Step 6: Inspect what we landed on ──
    current_url = new_page.url
    title = await new_page.title()
    body_text = await new_page.evaluate("() => document.body?.innerText?.substring(0, 2000) || ''")
    log(f"Step 6: Landed at {current_url[:80]}")
    log(f"  Title: {title}")
    log(f"  Body: {body_text[:200]}")

    # Check for interstitial "click to continue"
    if "please click to continue" in body_text.lower():
        log("  Interstitial detected — clicking continue...")
        try:
            await new_page.locator("a[href]").first.click(force=True, timeout=5000)
            await asyncio.sleep(10)
            current_url = new_page.url
            body_text = await new_page.evaluate("() => document.body?.innerText?.substring(0, 2000) || ''")
            log(f"  After click URL: {current_url[:80]}")
            log(f"  After click body: {body_text[:200]}")
        except Exception as e:
            log(f"  Continue click failed: {str(e)[:80]}")

    # Check for quality gate
    if "quality reasons" in body_text.lower() or "following question" in body_text.lower():
        log("  ✓ Quality gate detected!")
    else:
        log(f"  Unknown page state — dumping body")
        log(f"  {body_text[:500]}")

    # ── Step 7: Fill textarea ──
    log("Step 7: Filling answer...")
    filled = False
    for sel in ["#free_text_answer", "textarea", "textarea[name='free_text_answer']"]:
        try:
            el = new_page.locator(sel)
            if await el.count() > 0:
                await el.first.fill(
                    "I would live in Kyoto, Japan because of its rich cultural heritage, "
                    "beautiful temples, amazing food, and the perfect blend of traditional "
                    "and modern lifestyle."
                )
                log(f"  ✓ Filled via {sel}")
                filled = True
                break
        except Exception as e:
            log(f"  {sel} failed: {str(e)[:60]}")

    if not filled:
        log("  Trying evaluate fallback...")
        filled = await new_page.evaluate("""() => {
            const ta = document.querySelector('textarea, input[type="text"]');
            if (!ta) return false;
            ta.value = "I would live in Kyoto, Japan because of its rich cultural heritage, beautiful temples, amazing food, and the perfect blend of traditional and modern lifestyle.";
            ta.dispatchEvent(new Event('input', {bubbles: true}));
            ta.dispatchEvent(new Event('change', {bubbles: true}));
            return true;
        }""")
        log(f"  Evaluate: {'ok' if filled else 'failed'}")

    if not filled:
        log("ERROR: Could not fill textarea!")
        await new_page.screenshot(path=str(SS_DIR / "debug_no_textarea.png"))
        await new_page.close()
        await backend.stop()
        return

    await asyncio.sleep(1)

    # ── Step 8: Click Submit ──
    log("Step 8: Clicking Submit...")
    submitted = False
    for sel in ["#submit_button", "button:has-text('Submit')", "button[type='submit']"]:
        try:
            btn = new_page.locator(sel)
            if await btn.count() > 0:
                await btn.first.click(timeout=5000)
                log(f"  ✓ Clicked via {sel}")
                submitted = True
                break
        except Exception as e:
            log(f"  {sel} failed: {str(e)[:60]}")

    if not submitted:
        submitted = await new_page.evaluate("""() => {
            for (const btn of document.querySelectorAll('button')) {
                if (btn.textContent.trim().toLowerCase().includes('submit')) {
                    btn.click(); return true;
                }
            }
            return false;
        }""")
        log(f"  Evaluate fallback: {'ok' if submitted else 'failed'}")

    if not submitted:
        log("ERROR: Could not click Submit!")
        await new_page.screenshot(path=str(SS_DIR / "debug_no_submit.png"))
        await new_page.close()
        await backend.stop()
        return

    # ── Step 9: Wait for result ──
    log("Step 9: Waiting for result...")
    await asyncio.sleep(10)
    result_url = new_page.url
    result_body = await new_page.evaluate("() => document.body?.innerText?.substring(0, 2000) || ''")
    log(f"  Result URL: {result_url[:100]}")
    log(f"  Result body: {result_body[:500]}")

    success = any(kw in result_body.lower() for kw in ["thank you", "earned", "completed", "response recorded"])
    log(f"  Success: {success}")

    await new_page.screenshot(path=str(SS_DIR / "after_submit.png"))
    await new_page.close()

    # ── Step 10: Check ending balance ──
    log("Step 10: Checking ending balance...")
    await page.goto("https://app.primeopinion.com/", wait_until="domcontentloaded")
    await asyncio.sleep(5)
    end_balance = await get_balance(page)

    log("=" * 60)
    log(f"BEFORE:  ${start_balance}")
    log(f"AFTER:   ${end_balance}")
    if start_balance is not None and end_balance is not None:
        delta = end_balance - start_balance
        log(f"DELTA:   ${delta:.2f}")
        if delta > 0:
            log("✅ SUCCESS: Survey credited!")
        else:
            log("❌ Balance unchanged")
    log("=" * 60)

    await backend.stop()
    log("[DONE]")

if __name__ == "__main__":
    asyncio.run(main())
