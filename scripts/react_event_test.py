"""Focused test: React-compatible radio click + submit.

Tests THREE click strategies and logs ALL network traffic.
Does NOT run a full answer loop — just one question end-to-end.
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

    # Track ALL network traffic
    all_responses = []

    def on_resp(resp):
        all_responses.append({
            "method": resp.request.method,
            "url": resp.url[:200],
            "status": resp.status,
        })

    raw.on("response", on_resp)

    # ── Load home ──
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

    # ── Click tile ──
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
        log("FATAL: No qualification response — cannot test")
        await backend.stop()
        return

    q = qualification.get("questions", {}).get("current", {})
    log(f"    Q: {q.get('question', '?')[:80]}")

    await asyncio.sleep(3)
    all_responses.clear()  # Reset traffic tracker

    # ── Get answer options ──
    answers = q.get("answers", [])
    if not answers:
        log("    No answers!")
        await backend.stop()
        return

    # Pick "Android" for smartphone question, or first answer otherwise
    target_answer = answers[0]
    for a in answers:
        text = (a.get("text") or "").lower()
        if "android" in text:
            target_answer = a
            break
    target_text = target_answer.get("text", "")
    log(f"[3] Target answer: '{target_text}'")

    # ── Get button states BEFORE click ──
    btn_before = await page.evaluate(r"""() => {
        return [...document.querySelectorAll('button')]
            .filter(b => b.offsetWidth > 0)
            .map(b => ({ text: b.innerText?.trim()?.substring(0, 40), disabled: b.disabled,
                         cls: b.className?.substring(0, 50) }));
    }""")
    log("[4] Buttons BEFORE radio click:")
    disabled_before = {b["cls"]: b["disabled"] for b in btn_before}
    for b in btn_before:
        d = "DISABLED" if b["disabled"] else "enabled"
        log(f"    [{d}] '{b['text']}' cls='{b['cls'][:40]}'")

    # ── Strategy A: Playwright native click on VISIBLE label text ──
    log("[5] Strategy A: Playwright get_by_text() click...")
    try:
        label_el = raw.get_by_text(target_text, exact=True)
        if await label_el.count() > 0:
            await label_el.first.click(timeout=5000)
            log("    ✓ get_by_text click succeeded")
        else:
            log("    get_by_text found 0 elements, trying locator...")
            label_el = raw.locator(f"text='{target_text}'").first
            await label_el.click(timeout=5000)
            log("    ✓ locator text click succeeded")
    except Exception as e:
        log(f"    Strategy A failed: {str(e)[:100]}")
        # Strategy B: JS getElementById + label click (proven working)
        log("[5b] Strategy B: JS label click...")
        try:
            radio_info = await page.evaluate(r"""() => {
                const radios = [...document.querySelectorAll('input[type="radio"]')];
                return radios.filter(r => r.offsetWidth > 0 || r.offsetHeight > 0)
                    .map(r => ({
                        id: r.id,
                        label: r.labels?.[0]?.textContent?.trim()?.substring(0, 80) || ''
                    }));
            }""")
            target_radio = next((r for r in radio_info if target_text.lower() in r["label"].lower()), radio_info[0])
            rid = target_radio["id"]
            await page.evaluate(f"""() => {{
                const r = document.getElementById('{rid}');
                if (!r) return;
                const label = r.closest('label') || r.parentElement;
                if (label) label.click(); else r.click();
            }}""")
            log(f"    ✓ JS label click succeeded for '{target_radio['label'][:50]}'")
        except Exception as e2:
            log(f"    Strategy B also failed: {e2}")
            await backend.stop()
            return

    # ── Wait for React to update ──
    await asyncio.sleep(2)

    # ── Check if React state updated ──
    checked = await page.evaluate(
        r"""() => document.querySelectorAll('input[type="radio"]:checked').length"""
    )
    log(f"[6] Checked radios: {checked}")

    # ── Check button states AFTER click ──
    btn_after = await page.evaluate(r"""() => {
        return [...document.querySelectorAll('button')]
            .filter(b => b.offsetWidth > 0)
            .map(b => ({ text: b.innerText?.trim()?.substring(0, 40), disabled: b.disabled,
                         cls: b.className?.substring(0, 50) }));
    }""")
    log("[7] Buttons AFTER radio click:")
    for b in btn_after:
        d = "DISABLED" if b["disabled"] else "enabled"
        was = disabled_before.get(b["cls"])
        changed = " ← CHANGED!" if (was is not None and was != b["disabled"]) else ""
        log(f"    [{d}] '{b['text']}' cls='{b['cls'][:40]}'{changed}")

    # ── Remove popup overlay ──
    await page.evaluate("""
        document.querySelectorAll('.popup.integration-script-popup, [class*="integration-script-popup"]')
            .forEach(el => el.remove());
    """)
    log("[8] Removed popup overlay")

    # ── Try clicking ALL enabled buttons, one at a time, logging traffic ──
    log("[9] Testing submit buttons...")
    for b in btn_after:
        if b["disabled"]:
            continue
        btn_text = b["text"]
        if not btn_text or btn_text in ("999+", "0", "6 Level"):
            continue

        all_responses.clear()
        log(f"    Trying '{btn_text}'...")
        try:
            btn = raw.locator(f"button:has-text('{btn_text}')").last
            await btn.click(timeout=5000, force=True)
            await asyncio.sleep(3)

            posts = [r for r in all_responses if r["method"] == "POST"]
            quals = [r for r in all_responses if "qualification" in r["url"]]
            log(f"      Traffic: {len(all_responses)} responses, {len(posts)} POSTs, {len(quals)} qualification")
            for r in posts:
                log(f"      POST {r['url'][:100]} → {r['status']}")

            if posts:
                log(f"    ✅ '{btn_text}' triggered a POST!")
                break
            else:
                log(f"    ❌ '{btn_text}' — no POST")
        except Exception as e:
            log(f"    ❌ '{btn_text}' — click failed: {str(e)[:60]}")

    # ── Final state ──
    await page.screenshot(path=str(SS_DIR / "react_test_final.png"))
    log("[10] Screenshot: data/smoke/react_test_final.png")

    log(f"[11] Total responses captured: {len(all_responses)}")
    for r in all_responses[-10:]:
        log(f"    {r['method']} {r['url'][:120]} → {r['status']}")

    await backend.stop()
    log("[DONE]")


if __name__ == "__main__":
    asyncio.run(main())
