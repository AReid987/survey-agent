"""Focused diagnostic: verify radio .check() triggers React state, find real submit.

Tests:
1. Click tile → capture qualification
2. Use Playwright .check() on radio INPUT directly (not parent label)
3. Log button disabled states before/after
4. Log ALL network requests after each action
5. Try clicking any button that became enabled
6. Screenshot everything
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

    # Track ALL requests
    all_requests = []

    def on_req(req):
        all_requests.append({"method": req.method, "url": req.url[:200]})

    raw.on("request", on_req)

    # ── Step 1: Load home ──
    log("[1] Loading PO home...")
    await page.goto("https://app.primeopinion.com/", wait_until="domcontentloaded")
    await asyncio.sleep(5)

    # Dismiss device modal
    try:
        if await page.evaluate("() => document.body.innerText.includes('Select Devices')"):
            cb = raw.locator('input[type="checkbox"]').first
            if await cb.is_visible(timeout=3000):
                await cb.click(timeout=3000)
            save = raw.locator("button").filter(has_text="Save Selection")
            if await save.first.is_visible(timeout=3000):
                await save.first.click(timeout=3000)
                await asyncio.sleep(2)
    except Exception:
        pass

    # ── Step 2: Click tile + capture qualification ──
    log("[2] Clicking survey tile...")
    qualification = None
    try:
        async with raw.expect_response(
            lambda resp: "qualification" in resp.url, timeout=15000
        ) as resp_info:
            await raw.locator(".list-item.survey-item").first.scroll_into_view_if_needed(timeout=5000)
            await raw.locator(".list-item.survey-item").first.click(timeout=10000)
        resp = await resp_info.value
        qualification = await resp.json()
        log(f"    Status: {qualification.get('status')}")
        q = qualification.get("questions", {}).get("current", {})
        log(f"    Q: {q.get('question', '?')[:80]}")
    except Exception as e:
        log(f"ERROR: {e}")
        await backend.stop()
        return

    # ── Step 3: Wait, dump button states BEFORE clicking radio ──
    await asyncio.sleep(3)
    all_requests.clear()  # Reset to capture only post-radio-click requests

    btn_states_before = await page.evaluate(r"""() => {
        const btns = [...document.querySelectorAll('button')];
        return btns.filter(b => b.offsetWidth > 0).map(b => ({
            text: b.innerText?.trim()?.substring(0, 40),
            cls: b.className?.substring(0, 60),
            disabled: b.disabled,
            type: b.type || ''
        }));
    }""")
    log("[3] Buttons BEFORE radio click:")
    for b in btn_states_before:
        d = "DISABLED" if b["disabled"] else "enabled"
        log(f"    [{d}] text='{b['text']}' cls='{b['cls'][:50]}'")

    # ── Step 4: Use Playwright .check() on the radio INPUT directly ──
    log("[4] Clicking radio via Playwright .check()...")
    
    # Get the answer options for label matching
    answers = q.get("answers", [])
    chosen = answers[0] if answers else None  # Just pick first for diagnostic
    if chosen:
        log(f"    Target answer: {chosen.get('text', '?')[:60]}")

    # Find radio inputs and their labels
    radio_info = await page.evaluate(r"""() => {
        const radios = [...document.querySelectorAll('input[type="radio"]')];
        return radios.filter(r => r.offsetWidth > 0 || r.offsetHeight > 0)
            .map(r => {
                let label = r.labels?.[0]?.textContent?.trim() || '';
                if (!label) {
                    const p = r.closest('label') || r.closest('[class*="answer"]') || r.closest('div');
                    if (p) label = p.innerText?.trim()?.substring(0, 80) || '';
                }
                return { id: r.id, label, checked: r.checked, value: r.value };
            });
    }""")
    log(f"    Visible radios: {len(radio_info)}")
    for ri in radio_info[:6]:
        log(f"      label='{ri['label']}' value='{ri['value']}' checked={ri['checked']}")

    if not radio_info:
        log("    No radios found!")
        await backend.stop()
        return

    # Use Playwright .check() on the FIRST radio input
    target = radio_info[0]
    log(f"    Target: '{target['label']}' (id={target['id'][:30]})")
    
    radio_el = raw.locator(f"#{target['id']}")
    try:
        # Try Playwright .check() first
        await radio_el.check(timeout=5000, force=True)
        log("    ✓ .check() succeeded")
    except Exception as e:
        log(f"    .check() failed ({str(e)[:60]}), falling back to JS label click...")
        try:
            # Fallback: JS click on parent label (verified working in answer_inline.py)
            await page.evaluate(f"""() => {{
                const r = document.getElementById('{target['id']}');
                if (!r) return false;
                const label = r.closest('label') || r.parentElement;
                if (label) {{ label.click(); return true; }}
                r.click();
                return true;
            }}""")
            log("    ✓ JS label click succeeded")
        except Exception as e2:
            log(f"    JS label click also failed: {e2}")
            await backend.stop()
            return

    # Verify
    await asyncio.sleep(1)
    is_checked = await page.evaluate(
        r"""() => document.querySelectorAll('input[type="radio"]:checked').length"""
    )
    log(f"    Checked radios after: {is_checked}")

    # ── Step 5: Wait 3s, dump button states AFTER radio click ──
    await asyncio.sleep(3)

    btn_states_after = await page.evaluate(r"""() => {
        const btns = [...document.querySelectorAll('button')];
        return btns.filter(b => b.offsetWidth > 0).map(b => ({
            text: b.innerText?.trim()?.substring(0, 40),
            cls: b.className?.substring(0, 60),
            disabled: b.disabled,
            type: b.type || ''
        }));
    }""")
    log("[5] Buttons AFTER radio click:")
    for b in btn_states_after:
        d = "DISABLED" if b["disabled"] else "enabled"
        log(f"    [{d}] text='{b['text']}' cls='{b['cls'][:50]}'")

    # Compare: which buttons changed disabled state?
    log("[5b] Button state changes:")
    before_map = {b["cls"]: b["disabled"] for b in btn_states_before}
    for b in btn_states_after:
        was_disabled = before_map.get(b["cls"])
        if was_disabled is not None and was_disabled != b["disabled"]:
            log(f"    CHANGED: '{b['text']}' {was_disabled} → {b['disabled']}")

    # ── Step 6: Network requests after radio click ──
    log(f"[6] Network requests after radio click ({len(all_requests)}):")
    for r in all_requests:
        log(f"    {r['method']} {r['url']}")
    all_requests.clear()

    # ── Step 7: Click the gradient button + log response ──
    log("[7] Clicking gradient button...")
    gradient = raw.locator("button.p-btn--gradient").last
    try:
        if await gradient.count() > 0:
            await gradient.click(timeout=5000)
            log("    Clicked gradient button")
            await asyncio.sleep(5)
            log(f"    Network requests after gradient click ({len(all_requests)}):")
            for r in all_requests:
                log(f"      {r['method']} {r['url']}")
            all_requests.clear()
        else:
            log("    No gradient button found")
    except Exception as e:
        log(f"    Gradient click error: {e}")

    # ── Step 8: Try pressing Enter ──
    log("[8] Trying Enter key on checked radio...")
    try:
        checked_radio = raw.locator('input[type="radio"]:checked').first
        if await checked_radio.count() > 0:
            await checked_radio.press("Enter")
            await asyncio.sleep(5)
            log(f"    Network requests after Enter ({len(all_requests)}):")
            for r in all_requests:
                log(f"      {r['method']} {r['url']}")
    except Exception as e:
        log(f"    Enter failed: {e}")

    # ── Step 9: Try all disabled buttons (maybe one became enabled) ──
    log("[9] Checking all visible buttons for enabled state...")
    enabled_btns = await page.evaluate(r"""() => {
        return [...document.querySelectorAll('button')]
            .filter(b => !b.disabled && b.offsetWidth > 0)
            .map(b => ({
                text: b.innerText?.trim()?.substring(0, 40),
                cls: b.className?.substring(0, 60)
            }));
    }""")
    log(f"    Enabled buttons ({len(enabled_btns)}):")
    for b in enabled_btns:
        log(f"      text='{b['text']}' cls='{b['cls'][:50]}'")

    # ── Step 10: Screenshot ──
    await page.screenshot(path=str(SS_DIR / "diag_submit_test.png"))
    log("[10] Screenshot: data/smoke/diag_submit_test.png")

    # ── Step 11: Try direct API call (bypass UI) ──
    log("[11] Trying direct API call to /qualification...")
    survey_id = "1e57ade0-7549-4c9b-b724-309826b2aade"  # from initial POST
    question_id = q.get("id")
    answer_id = chosen.get("id") if chosen else None
    
    if question_id and answer_id:
        api_result = await page.evaluate(f"""async () => {{
            try {{
                const resp = await fetch('https://api.primesurveys.com/qualification', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    credentials: 'include',
                    body: JSON.stringify({{
                        survey: '{survey_id}',
                        questionId: '{question_id}',
                        answerId: '{answer_id}'
                    }})
                }});
                return {{status: resp.status, body: await resp.text().then(t => t.substring(0, 500))}};
            }} catch(e) {{
                return {{error: e.message}};
            }}
        }}""")
        log(f"    API response: {api_result}")
    else:
        log(f"    Missing question_id or answer_id")

    await backend.stop()
    log("[DONE]")


if __name__ == "__main__":
    asyncio.run(main())
