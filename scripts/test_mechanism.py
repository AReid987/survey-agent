"""Minimal end-to-end mechanism test: click radio + click p-btn--fill.

Proven working: get_by_text().click() on radio → checked=1
Untested: p-btn--fill button (appeared after radio click, was skipped by test loop)
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
    print(f"[test] {msg}", flush=True)


async def main():
    backend = create_backend("camoufox", headless=True, persistent=True,
                             user_data_dir=PROFILE_DIR, humanize=False, geoip=False)
    await backend.start()
    page = await backend.get_page()
    raw = page._page

    # Track ALL traffic — no URL filter
    all_events = []

    def on_req(req):
        all_events.append({"type": "req", "method": req.method, "url": req.url[:200]})

    def on_resp(resp):
        all_events.append({"type": "resp", "method": resp.request.method, "url": resp.url[:200], "status": resp.status})

    raw.on("request", on_req)
    raw.on("response", on_resp)

    # Load home
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

    # Click tile
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
    except Exception as e:
        log(f"ERROR: {e}")

    if not qualification:
        log("FATAL: No qualification response")
        await backend.stop()
        return

    questions = qualification.get("questions") or {}
    q = questions.get("current") or {}
    answers = q.get("answers") or []
    log(f"    Q: {q.get('question', '?')[:80]}")
    log(f"    Answers: {len(answers)}")

    await asyncio.sleep(3)
    all_events.clear()  # Reset tracker

    # Pick first answer (or "Android" if smartphone question)
    target = answers[0] if answers else None
    for a in answers:
        if "android" in (a.get("text") or "").lower():
            target = a
            break
    target_text = target.get("text", "") if target else ""
    log(f"[3] Target: '{target_text}'")

    # Click radio via get_by_text (proven working)
    log("[4] Clicking radio via get_by_text()...")
    try:
        label_el = raw.get_by_text(target_text, exact=True)
        count = await label_el.count()
        if count > 0:
            await label_el.first.click(timeout=5000)
            log("    ✓ get_by_text click succeeded")
        else:
            log(f"    get_by_text found 0 elements for '{target_text}'")
            # Fallback: JS click
            radio_info = await page.evaluate(r"""() => {
                return [...document.querySelectorAll('input[type="radio"]')]
                    .filter(r => r.offsetWidth > 0 || r.offsetHeight > 0)
                    .map(r => ({
                        id: r.id,
                        label: r.labels?.[0]?.textContent?.trim()?.substring(0, 80) || ''
                    }));
            }""")
            target_radio = next((r for r in radio_info if target_text.lower() in r["label"].lower()), radio_info[0] if radio_info else None)
            if target_radio:
                rid = target_radio["id"]
                await page.evaluate(f"""() => {{
                    const r = document.getElementById('{rid}');
                    if (!r) return;
                    const label = r.closest('label') || r.parentElement;
                    if (label) label.click(); else r.click();
                }}""")
                log(f"    ✓ JS label click for '{target_radio['label'][:50]}'")
    except Exception as e:
        log(f"    Click failed: {e}")
        await backend.stop()
        return

    # Wait for React to process
    await asyncio.sleep(2)

    checked = await page.evaluate(
        r"""() => document.querySelectorAll('input[type="radio"]:checked').length"""
    )
    log(f"[5] Checked radios: {checked}")

    # Remove popup overlay
    await page.evaluate("""
        document.querySelectorAll('.popup.integration-script-popup, [class*="integration-script-popup"]')
            .forEach(el => el.remove());
    """)
    log("[6] Removed popup overlay")

    # Enumerate ALL buttons with their disabled state
    btns = await page.evaluate(r"""() => {
        return [...document.querySelectorAll('button')]
            .filter(b => b.offsetWidth > 0 || b.offsetHeight > 0)
            .map(b => ({
                text: b.innerText?.trim()?.substring(0, 40),
                cls: b.className?.substring(0, 60),
                disabled: b.disabled,
                dataTestId: b.getAttribute('data-test-id') || ''
            }));
    }""")
    log("[7] ALL visible buttons:")
    for b in btns:
        d = "DISABLED" if b["disabled"] else "enabled"
        test_id = f" data-test-id='{b['dataTestId']}'" if b["dataTestId"] else ""
        log(f"    [{d}] text='{b['text']}' cls='{b['cls'][:45]}'{test_id}")

    # Try each button and log traffic
    log("[8] Testing each enabled button...")

    # First: p-btn--fill (the NEW button that appeared after radio click)
    log("    --- Testing p-btn--fill specifically ---")
    all_events.clear()
    try:
        fill = raw.locator("button.p-btn--fill:visible")
        if await fill.count() > 0:
            await fill.last.click(timeout=5000, force=True)
            log("    ✓ Clicked p-btn--fill (force=True)")
            await asyncio.sleep(5)
            posts = [e for e in all_events if e["type"] == "req" and e["method"] == "POST"]
            quals = [e for e in all_events if "qualification" in e["url"]]
            log(f"    Traffic: {len(all_events)} events, {len(posts)} POSTs, {len(quals)} qualification")
            for e in posts:
                log(f"      POST {e['url'][:120]}")
            for e in quals:
                log(f"      QUAL {e['method']} {e['url'][:120]} → {e.get('status', '?')}")
            if posts:
                log("    ✅ p-btn--fill triggered a POST!")
            else:
                log("    ❌ p-btn--fill — no POST")
        else:
            log("    p-btn--fill not found")
    except Exception as e:
        log(f"    p-btn--fill error: {str(e)[:100]}")

    # Second: p-btn--link p-btn--primary (the one that changed state)
    log("    --- Testing p-btn--link p-btn--primary ---")
    all_events.clear()
    try:
        link_btn = raw.locator("button.p-btn--link.p-btn--primary:visible")
        count = await link_btn.count()
        log(f"    Found {count} p-btn--link buttons")
        for i in range(count):
            btn = link_btn.nth(i)
            is_disabled = await btn.evaluate("b => b.disabled")
            if is_disabled:
                log(f"    Button {i}: DISABLED — skipping")
                continue
            await btn.click(timeout=5000, force=True)
            log(f"    ✓ Clicked p-btn--link button {i}")
            await asyncio.sleep(5)
            posts = [e for e in all_events if e["type"] == "req" and e["method"] == "POST"]
            log(f"    Traffic: {len(all_events)} events, {len(posts)} POSTs")
            for e in posts:
                log(f"      POST {e['url'][:120]}")
            if posts:
                log("    ✅ p-btn--link triggered a POST!")
            else:
                log("    ❌ p-btn--link — no POST")
            all_events.clear()
    except Exception as e:
        log(f"    p-btn--link error: {str(e)[:100]}")

    # Third: CLAIM REWARD with force=True
    log("    --- Testing CLAIM REWARD ---")
    all_events.clear()
    try:
        claim = raw.locator("button.p-btn--gradient:visible")
        if await claim.count() > 0:
            await claim.last.click(timeout=5000, force=True)
            log("    ✓ Clicked CLAIM REWARD")
            await asyncio.sleep(5)
            posts = [e for e in all_events if e["type"] == "req" and e["method"] == "POST"]
            log(f"    Traffic: {len(all_events)} events, {len(posts)} POSTs")
            for e in posts:
                log(f"      POST {e['url'][:120]}")
            if posts:
                log("    ✅ CLAIM REWARD triggered a POST!")
            else:
                log("    ❌ CLAIM REWARD — no POST")
        else:
            log("    CLAIM REWARD not found")
    except Exception as e:
        log(f"    CLAIM REWARD error: {str(e)[:100]}")

    # Screenshot
    await page.screenshot(path=str(SS_DIR / "mechanism_test_final.png"))
    log("[9] Screenshot: data/smoke/mechanism_test_final.png")

    await backend.stop()
    log("[DONE]")


if __name__ == "__main__":
    asyncio.run(main())
