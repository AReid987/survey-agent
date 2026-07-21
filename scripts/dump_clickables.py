"""One-shot DOM diagnostic: dump all clickable elements after radio click.

Answers the question: what submit mechanism exists on PO after selecting a radio?
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
    print(f"[dump] {msg}", flush=True)


async def main():
    backend = create_backend("camoufox", headless=True, persistent=True,
                             user_data_dir=PROFILE_DIR, humanize=False, geoip=False)
    await backend.start()
    page = await backend.get_page()
    raw = page._page

    # Track ALL network requests
    all_requests = []

    def on_req(req):
        all_requests.append({"method": req.method, "url": req.url[:150]})

    def on_resp(resp):
        pass  # we just track requests

    raw.on("request", on_req)

    # Step 1: Load home
    log("[1] Loading PO home...")
    await page.goto("https://app.primeopinion.com/", wait_until="domcontentloaded")
    await asyncio.sleep(5)

    # Dismiss device modal if present
    try:
        if await page.evaluate("() => document.body.innerText.includes('Select Devices')"):
            log("[2] Dismissing device modal...")
            cb = raw.locator('input[type="checkbox"]').first
            if await cb.is_visible(timeout=3000):
                await cb.click(timeout=3000)
            save = raw.locator("button").filter(has_text="Save Selection")
            if await save.first.is_visible(timeout=3000):
                await save.first.click(timeout=3000)
                await asyncio.sleep(2)
    except Exception:
        pass

    # Step 2: Click first tile
    log("[3] Clicking survey tile...")
    try:
        async with raw.expect_response(
            lambda resp: "qualification" in resp.url, timeout=15000
        ) as resp_info:
            await raw.locator(".list-item.survey-item").first.scroll_into_view_if_needed(timeout=5000)
            await raw.locator(".list-item.survey-item").first.click(timeout=10000)
        resp = await resp_info.value
        qual = await resp.json()
        log(f"    Qualification status: {qual.get('status')}")
        q = qual.get("questions", {}).get("current", {})
        log(f"    Question: {q.get('question', '?')[:80]}")
        answers = q.get("answers", [])
        log(f"    Answers: {len(answers)}")
    except Exception as e:
        log(f"ERROR: {e}")
        await backend.stop()
        return

    # Step 3: Wait and dump state BEFORE clicking radio
    await asyncio.sleep(3)
    pre_state = await page.evaluate(r"""() => {
        // All visible buttons
        const btns = [...document.querySelectorAll('button, [role="button"], input[type="submit"], a.btn, [class*="btn"]')];
        const visibleBtns = btns.filter(b => b.offsetWidth > 0 && b.offsetHeight > 0)
            .map(b => ({
                tag: b.tagName,
                text: b.innerText?.trim()?.substring(0, 60) || '',
                cls: b.className?.substring?.(0, 60) || '',
                id: b.id || '',
                role: b.getAttribute('role') || '',
                disabled: b.disabled || false,
                href: b.href?.substring(0, 80) || ''
            }));

        // All visible radios
        const radios = [...document.querySelectorAll('input[type="radio"]')];
        const visRadios = radios.filter(r => r.offsetWidth > 0 || r.offsetHeight > 0)
            .map(r => ({
                id: r.id,
                label: r.labels?.[0]?.textContent?.trim()?.substring(0, 60) || 
                       r.closest('label')?.innerText?.trim()?.substring(0, 60) ||
                       r.parentElement?.innerText?.trim()?.substring(0, 60) || '',
                checked: r.checked
            }));

        // Check for any text inputs or selects that appeared
        const inputs = [...document.querySelectorAll('input[type="text"], textarea, select')]
            .filter(i => i.offsetWidth > 0)
            .map(i => ({tag: i.tagName, type: i.type, placeholder: i.placeholder || '', id: i.id}));

        // Count cursor:pointer elements
        const allEls = document.querySelectorAll('*');
        let pointerCount = 0;
        for (const el of allEls) {
            if (getComputedStyle(el).cursor === 'pointer' && el.offsetWidth > 0) pointerCount++;
        }

        return {buttons: visibleBtns, radios: visRadios, textInputs: inputs, pointerCount};
    }""")

    log("[4] DOM state BEFORE radio click:")
    log(f"    Visible buttons ({len(pre_state['buttons'])}):")
    for b in pre_state["buttons"]:
        log(f"      [{b['tag']}] text='{b['text']}' class='{b['cls'][:40]}' disabled={b['disabled']}")
    log(f"    Visible radios ({len(pre_state['radios'])}):")
    for r in pre_state["radios"][:8]:
        log(f"      label='{r['label']}' checked={r['checked']}")
    if pre_state["textInputs"]:
        log(f"    Text inputs: {pre_state['textInputs']}")
    log(f"    Cursor:pointer elements: {pre_state['pointerCount']}")

    # Step 4: Click the first visible radio using Playwright NATIVE click
    # This is critical: React synthetic events require real mouse events, not JS .click()
    log("[5] Clicking first visible radio via Playwright native click...")
    first_radio = raw.locator('input[type="radio"]:visible').first
    radio_count = await raw.locator('input[type="radio"]:visible').count()
    log(f"    Visible radios: {radio_count}")

    if radio_count > 0:
        try:
            # Use Playwright's native click (dispatches mousedown/up/click events)
            await first_radio.click(timeout=5000, force=True)
            log("    Playwright native click succeeded")
        except Exception as e:
            log(f"    Native click failed: {e}")
            # Fallback: click the parent label/container
            try:
                label = raw.locator('input[type="radio"]:visible').first.locator("xpath=ancestor::label")
                if await label.count() > 0:
                    await label.first.click(timeout=5000)
                    log("    Clicked parent label instead")
                else:
                    # Try clicking parent div
                    parent = raw.locator('input[type="radio"]:visible').first.locator("xpath=..")
                    await parent.click(timeout=5000)
                    log("    Clicked parent element instead")
            except Exception as e2:
                log(f"    All click strategies failed: {e2}")
    else:
        log("    No visible radios!")

    # Step 5: Wait 5 seconds for any delayed UI updates (animations, state changes)
    log("[6] Waiting 5 seconds for UI reaction...")
    await asyncio.sleep(5)

    # Step 6: Dump state AFTER radio click
    post_state = await page.evaluate(r"""() => {
        const btns = [...document.querySelectorAll('button, [role="button"], input[type="submit"], a.btn, [class*="btn"]')];
        const visibleBtns = btns.filter(b => b.offsetWidth > 0 && b.offsetHeight > 0)
            .map(b => ({
                tag: b.tagName,
                text: b.innerText?.trim()?.substring(0, 60) || '',
                cls: b.className?.substring?.(0, 60) || '',
                disabled: b.disabled || false
            }));

        // Check if any NEW elements appeared
        const radios = [...document.querySelectorAll('input[type="radio"]:checked')];
        const checkedCount = radios.length;

        // Look for any overlay/modal/dialog
        const modals = [...document.querySelectorAll('[role="dialog"], .modal, [class*="Modal"], [class*="overlay"]')];
        const modalVisible = modals.filter(m => m.offsetWidth > 0).map(m => m.innerText?.substring(0, 200));

        // Check active element
        const active = document.activeElement;
        const activeInfo = active ? {
            tag: active.tagName,
            id: active.id,
            class: active.className?.substring?.(0, 40) || '',
            type: active.type || ''
        } : null;

        // Check for form elements
        const forms = [...document.querySelectorAll('form')].map(f => ({
            action: f.action?.substring(0, 100),
            method: f.method,
            visible: f.offsetWidth > 0
        }));

        return {buttons: visibleBtns, checkedRadios: checkedCount, modals: modalVisible, activeElement: activeInfo, forms};
    }""")

    log("[7] DOM state AFTER radio click:")
    log(f"    Checked radios: {post_state['checkedRadios']}")
    log(f"    Visible buttons ({len(post_state['buttons'])}):")
    for b in post_state["buttons"]:
        log(f"      [{b['tag']}] text='{b['text']}' class='{b['cls'][:40]}' disabled={b['disabled']}")
    if post_state["modals"]:
        log(f"    Modals/overlays: {post_state['modals'][:2]}")
    if post_state["activeElement"]:
        log(f"    Active element: {post_state['activeElement']}")
    if post_state["forms"]:
        log(f"    Forms: {post_state['forms']}")

    # Step 7: Try Enter key
    log("[8] Trying Enter key...")
    try:
        await first_radio.press("Enter")
        await asyncio.sleep(3)
    except Exception:
        pass

    # Check if anything changed after Enter
    after_enter = await page.evaluate(r"""() => {
        const radios = [...document.querySelectorAll('input[type="radio"]:checked')].length;
        const btns = [...document.querySelectorAll('button:visible, [role="button"]:visible')]
            .map(b => b.innerText?.trim()?.substring(0, 40))
            .filter(t => t);
        return {checkedRadios: radios, buttons: btns};
    }""")
    log(f"    After Enter: {after_enter['checkedRadios']} checked, buttons: {after_enter['buttons'][:5]}")

    # Step 8: Network requests captured
    log(f"[9] Network requests during test ({len(all_requests)}):")
    qualification_reqs = [r for r in all_requests if "qualification" in r["url"]]
    log(f"    /qualification requests: {len(qualification_reqs)}")
    for r in qualification_reqs:
        log(f"      {r['method']} {r['url']}")
    other_posts = [r for r in all_requests if r["method"] == "POST" and "qualification" not in r["url"]]
    if other_posts:
        log(f"    Other POST requests:")
        for r in other_posts[:5]:
            log(f"      {r['url']}")

    # Step 9: Screenshot
    await page.screenshot(path=str(SS_DIR / "after_radio_native_click.png"))
    log("[10] Screenshot: data/smoke/after_radio_native_click.png")

    # Step 10: Also dump the FULL innerText of the visible area
    body_text = await page.evaluate("() => document.body.innerText.substring(0, 1500)")
    log(f"[11] Page body text (first 1500 chars):")
    log(body_text[:1500])

    await backend.stop()
    log("[DONE]")


if __name__ == "__main__":
    asyncio.run(main())
