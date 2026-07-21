"""Definitive single diagnostic: popup-first, log full JSON, test submit.

80 lines. No assumptions about response structure. Run once.
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
    print(f"[DEF] {msg}", flush=True)

async def main():
    backend = create_backend("camoufox", headless=True, persistent=True,
                             user_data_dir=PROFILE_DIR, humanize=False, geoip=False)
    await backend.start()
    page = await backend.get_page()
    raw = page._page

    # Track ALL traffic
    traffic = []
    def on_req(req):
        traffic.append({"m": req.method, "u": req.url[:200]})
    raw.on("request", on_req)

    # ── Remove popup FIRST ──
    await page.goto("https://app.primeopinion.com/", wait_until="domcontentloaded")
    await asyncio.sleep(3)
    await page.evaluate("""
        document.querySelectorAll('.popup.integration-script-popup, [class*="integration-script-popup"]')
            .forEach(el => el.remove());
    """)
    log("Removed popup overlay")

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

    await asyncio.sleep(3)

    # ── Click tile ──
    log("Clicking survey tile...")
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

    # ── Log FULL JSON ──
    log(f"FULL qualification JSON:\n{json.dumps(qualification, indent=2)[:2000]}")

    # ── Parse question (try multiple paths) ──
    q = (qualification.get("questions") or {}).get("current") or {}
    if not q:
        q = qualification.get("current") or {}
    if not q:
        q = qualification.get("question") or {}

    if not q:
        log("No question found in response. Keys: " + str(list(qualification.keys())))
        await backend.stop()
        return

    q_text = q.get("question", "?")
    answers = q.get("answers") or []
    log(f"Q: {q_text[:100]}")
    log(f"Answers: {len(answers)}")

    if not answers:
        log("No answers — stopping")
        await backend.stop()
        return

    # ── Wait for radios to render ──
    await asyncio.sleep(3)

    # ── Find radios and click first one via JS ──
    target_text = answers[0].get("text", "")
    for a in answers:
        if "android" in (a.get("text") or "").lower():
            target_text = a.get("text", "")
            break

    log(f"Target: '{target_text[:60]}'")

    radio_info = await page.evaluate(r"""() => {
        return [...document.querySelectorAll('input[type="radio"]')]
            .filter(r => r.offsetWidth > 0 || r.offsetHeight > 0)
            .map(r => ({
                id: r.id,
                label: r.labels?.[0]?.textContent?.trim()?.substring(0, 80) ||
                       r.closest('label')?.innerText?.trim()?.substring(0, 80) || ''
            }));
    }""")
    log(f"Visible radios: {len(radio_info)}")

    # Click matching radio via JS
    clicked = False
    for ri in radio_info:
        if target_text.lower() in ri["label"].lower() or ri["label"].lower() in target_text.lower():
            rid = ri["id"]
            await page.evaluate(f"""() => {{
                const r = document.getElementById('{rid}');
                if (!r) return;
                const label = r.closest('label') || r.parentElement;
                if (label) label.click(); else r.click();
            }}""")
            log(f"Clicked radio: '{ri['label'][:50]}'")
            clicked = True
            break

    if not clicked and radio_info:
        rid = radio_info[0]["id"]
        await page.evaluate(f"""() => {{
            const r = document.getElementById('{rid}');
            if (!r) return;
            const label = r.closest('label') || r.parentElement;
            if (label) label.click(); else r.click();
        }}""")
        log(f"Clicked first radio: '{radio_info[0]['label'][:50]}'")
        clicked = True

    if not clicked:
        log("No radios found")
        await backend.stop()
        return

    # Dispatch React events
    await page.evaluate("""() => {
        document.querySelectorAll('input[type="radio"]:checked').forEach(r => {
            r.dispatchEvent(new Event('change', {bubbles: true}));
            r.dispatchEvent(new Event('input', {bubbles: true}));
        });
    }""")

    await asyncio.sleep(1)
    checked = await page.evaluate("() => document.querySelectorAll('input[type=\"radio\"]:checked').length")
    log(f"Checked radios: {checked}")

    # ── Remove popup again ──
    await page.evaluate("""
        document.querySelectorAll('.popup.integration-script-popup, [class*="integration-script-popup"]')
            .forEach(el => el.remove());
    """)

    # ── Try p-btn--fill (the real submit) ──
    traffic.clear()
    fill = raw.locator("button.p-btn--fill:visible")
    if await fill.count() > 0:
        log(f"Found p-btn--fill button, clicking with force=True...")
        try:
            await fill.last.click(timeout=5000, force=True)
            await asyncio.sleep(5)
            posts = [t for t in traffic if t["m"] == "POST"]
            quals = [t for t in traffic if "qualification" in t["u"]]
            log(f"Traffic after fill click: {len(traffic)} events, {len(posts)} POSTs, {len(quals)} /qualification")
            for t in posts:
                log(f"  POST {t['u'][:150]}")
            if posts:
                log("✅ p-btn--fill TRIGGERED A POST!")
            else:
                log("❌ p-btn--fill — no POST sent")
        except Exception as e:
            log(f"Fill click error: {e}")
    else:
        log("p-btn--fill not found")

    # ── Try JS click on fill button ──
    traffic.clear()
    js_clicked = await page.evaluate("""() => {
        const btn = document.querySelector('button.p-btn--fill');
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    if js_clicked:
        log("JS click on p-btn--fill succeeded, waiting...")
        await asyncio.sleep(5)
        posts = [t for t in traffic if t["m"] == "POST"]
        log(f"Traffic after JS click: {len(traffic)} events, {len(posts)} POSTs")
        for t in posts:
            log(f"  POST {t['u'][:150]}")
        if posts:
            log("✅ JS p-btn--fill TRIGGERED A POST!")

    # ── Final screenshot ──
    await page.screenshot(path=str(SS_DIR / "definitive_final.png"))
    log("Screenshot: data/smoke/definitive_final.png")

    await backend.stop()
    log("[DONE]")

if __name__ == "__main__":
    asyncio.run(main())
