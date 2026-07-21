"""Consolidated survey automation for Prime Opinion.

Handles TWO tile types:
  1. status="qualified" → navigate to external URL immediately
  2. status="accepted"  → answer inline questions via radio click + submit

Key verified mechanics:
  - Popup overlay must be removed BEFORE and AFTER tile click
  - Radio IDs start with digits → use getElementById, not CSS #id
  - React requires dispatchEvent(change/input) after DOM click
  - p-btn--fill is the real submit button (appears after radio selection)
  - Poll page.url and DOM state after submit — don't wait for POST
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
    print(f"[auto] {msg}", flush=True)


def pick_answer(q_text: str, answers: list) -> str:
    """Pick answer text based on profile. Returns answer text (not ID)."""
    qt = q_text.lower()
    
    rules = [
        ("smartphone", "android"),
        ("occupation" in qt or "sector" in qt or "industry" in qt, "technology"),
        ("company" in qt and ("employ" in qt or "size" in qt), "1 to 4"),
        ("age" in qt, "35"),
        ("gender" in qt or "sex" in qt, "male"),
        ("income" in qt or "household" in qt, "50,000"),
        ("state" in qt or "reside" in qt, "texas"),
        ("how many" in qt and "child" in qt, "3"),
        ("children" in qt or "child" in qt, "yes"),
        ("education" in qt or "degree" in qt, "bachelor"),
        ("race" in qt or "ethnicity" in qt, "black"),
        ("marital" in qt or "married" in qt, "married"),
        ("own or rent" in qt or "housing" in qt, "rent"),
        ("employment" in qt or "are you employed" in qt, "self-employed"),
        ("smok" in qt, "no"),
        ("alcohol" in qt or "drink" in qt, "no"),
        ("webcam" in qt, "willing"),
        ("area you live" in qt or "urban" in qt, "urban"),
    ]
    
    for cond, keyword in rules:
        if cond:
            for a in answers:
                if keyword in (a.get("text") or "").lower():
                    return a["text"]
    
    # Fallbacks
    for a in answers:
        txt = (a.get("text") or "").lower()
        if "does not apply" in txt or "none of the above" in txt:
            return a["text"]
    for a in answers:
        if "other" in (a.get("text") or "").lower():
            return a["text"]
    return answers[0]["text"] if answers else ""


def remove_popup_js():
    return """
        document.querySelectorAll(
            '.popup.integration-script-popup, [class*="integration-script-popup"]'
        ).forEach(el => el.remove());
    """


async def dismiss_modal(page, raw):
    """Dismiss device selection modal if present."""
    try:
        if await page.evaluate("() => document.body.innerText.includes('Select Devices')"):
            log("  Dismissing device modal...")
            cb = raw.locator('input[type="checkbox"]').first
            if await cb.is_visible(timeout=3000):
                await cb.click(timeout=3000)
            save = raw.locator("button").filter(has_text="Save Selection")
            if await save.first.is_visible(timeout=3000):
                await save.first.click(timeout=3000)
                await asyncio.sleep(2)
    except Exception:
        pass


async def select_radio(page, answer_text: str) -> bool:
    """Select a radio by matching label text. Returns True if clicked."""
    radio_info = await page.evaluate(r"""() => {
        return [...document.querySelectorAll('input[type="radio"]')]
            .filter(r => r.offsetWidth > 0 || r.offsetHeight > 0)
            .map(r => ({
                id: r.id,
                label: r.labels?.[0]?.textContent?.trim()?.substring(0, 100) ||
                       r.closest('label')?.innerText?.trim()?.substring(0, 100) || ''
            }));
    }""")
    
    if not radio_info:
        log("  No visible radios")
        return False
    
    log(f"  {len(radio_info)} radios, matching '{answer_text[:40]}'")
    
    # Try get_by_text first (Playwright native click — best for React)
    try:
        el = page.get_by_text(answer_text, exact=False)
        if await el.count() > 0:
            await el.first.click(timeout=3000)
            log(f"  ✓ get_by_text clicked")
            await asyncio.sleep(0.5)
            # Dispatch React events
            await page.evaluate("""() => {
                document.querySelectorAll('input[type="radio"]:checked').forEach(r => {
                    r.dispatchEvent(new Event('change', {bubbles: true}));
                    r.dispatchEvent(new Event('input', {bubbles: true}));
                });
            }""")
            checked = await page.evaluate(
                "() => document.querySelectorAll('input[type=\"radio\"]:checked').length"
            )
            log(f"  Checked: {checked}")
            return checked > 0
    except Exception as e:
        log(f"  get_by_text failed: {str(e)[:80]}")
    
    # Fallback: JS label click (for digit-first IDs)
    for ri in radio_info:
        if answer_text.lower() in ri["label"].lower():
            rid = ri["id"]
            await page.evaluate(f"""() => {{
                const r = document.getElementById('{rid}');
                if (!r) return;
                const label = r.closest('label') || r.parentElement;
                if (label) label.click(); else r.click();
            }}""")
            log(f"  ✓ JS clicked: '{ri['label'][:50]}'")
            await asyncio.sleep(0.5)
            await page.evaluate("""() => {
                document.querySelectorAll('input[type="radio"]:checked').forEach(r => {
                    r.dispatchEvent(new Event('change', {bubbles: true}));
                    r.dispatchEvent(new Event('input', {bubbles: true}));
                });
            }""")
            checked = await page.evaluate(
                "() => document.querySelectorAll('input[type=\"radio\"]:checked').length"
            )
            log(f"  Checked: {checked}")
            return checked > 0
    
    log("  No matching radio found")
    return False


async def try_submit(page, raw) -> bool:
    """Try to submit the current answer. Returns True if navigation occurred."""
    # Remove popup
    await page.evaluate(remove_popup_js())
    
    # Try p-btn--fill (real submit)
    try:
        fill = raw.locator("button.p-btn--fill:visible")
        if await fill.count() > 0:
            await fill.last.click(timeout=3000, force=True)
            log("  Clicked p-btn--fill")
            # Wait and check for state change
            for i in range(10):
                await asyncio.sleep(1)
                url = page.url
                if "qualification" not in url and "primeopinion" not in url:
                    log(f"  → Navigated: {url[:100]}")
                    return True
                # Check if question changed
                new_q = await page.evaluate(
                    "() => document.querySelector('.question-text, [class*=\"question\"]')?.innerText?.substring(0, 60) || ''"
                )
                if new_q:
                    log(f"  New question detected: '{new_q[:50]}'")
                    return True
    except Exception as e:
        log(f"  p-btn--fill error: {str(e)[:80]}")
    
    # Try JS click on fill
    try:
        await page.evaluate("document.querySelector('button.p-btn--fill')?.click()")
        log("  JS clicked p-btn--fill")
        await asyncio.sleep(3)
    except Exception:
        pass
    
    # Try gradient button as last resort
    try:
        grad = raw.locator("button.p-btn--gradient:visible")
        if await grad.count() > 0:
            await grad.last.click(timeout=3000, force=True)
            log("  Clicked gradient button")
            await asyncio.sleep(3)
    except Exception:
        pass
    
    return False


async def main():
    backend = create_backend("camoufox", headless=True, persistent=True,
                             user_data_dir=PROFILE_DIR, humanize=False, geoip=False)
    await backend.start()
    page = await backend.get_page()
    raw = page._page

    # Track traffic
    traffic = []
    def on_resp(resp):
        traffic.append({"m": resp.request.method, "u": resp.url[:200], "s": resp.status})
    raw.on("response", on_resp)

    # ── Load page ──
    log("[1] Loading PO home...")
    await page.goto("https://app.primeopinion.com/", wait_until="domcontentloaded")
    await asyncio.sleep(3)
    
    # Remove popup BEFORE anything
    await page.evaluate(remove_popup_js())
    log("  Popup removed")
    
    await dismiss_modal(page, raw)
    await asyncio.sleep(2)

    # ── Check balance ──
    balance = await page.evaluate(r"""() => {
        const el = document.querySelector('.user-balance');
        if (!el) return null;
        const m = el.innerText.match(/\$\s*([\d.]+)/);
        return m ? m[1] : null;
    }""")
    log(f"[2] Balance: ${balance}")

    # ── Click tile ──
    log("[3] Clicking survey tile...")
    traffic.clear()
    
    qualification = None
    try:
        async with raw.expect_response(
            lambda resp: "qualification" in resp.url, timeout=15000
        ) as resp_info:
            await raw.locator(".list-item.survey-item").first.scroll_into_view_if_needed(timeout=5000)
            await raw.locator(".list-item.survey-item").first.click(timeout=10000)
        resp = await resp_info.value
        qualification = await resp.json()
        log(f"  Status: {qualification.get('status')}")
    except Exception as e:
        log(f"  ERROR: {str(e)[:150]}")

    if not qualification:
        log("  FATAL: No qualification response")
        await backend.stop()
        return

    # ── Handle QUALIFIED immediately ──
    if qualification.get("status") == "qualified":
        url = qualification.get("url", "")
        log(f"[4] QUALIFIED! URL: {url[:120]}")
        if url:
            new_page = await raw.context.new_page()
            await new_page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(10)
            body = await new_page.evaluate("() => document.body?.innerText?.substring(0, 500) || ''")
            log(f"  Survey page: {body[:300]}")
            await new_page.screenshot(path=str(SS_DIR / "qualified_survey.png"))
            log("  Screenshot: data/smoke/qualified_survey.png")
            await new_page.close()
        else:
            log("  Qualified but no URL")
        await backend.stop()
        return

    # ── Handle ACCEPTED (question loop) ──
    if qualification.get("status") != "accepted":
        log(f"  Unexpected status: {qualification.get('status')}")
        await backend.stop()
        return

    max_questions = 15
    for q_num in range(max_questions):
        # Remove popup
        await page.evaluate(remove_popup_js())
        
        questions = (qualification.get("questions") or {})
        current_q = questions.get("current") or {}
        
        if not current_q:
            log(f"[Q{q_num+1}] No question — checking for qualified URL...")
            url = qualification.get("url", "")
            if url:
                log(f"  ✓ QUALIFIED! URL: {url[:120]}")
                new_page = await raw.context.new_page()
                await new_page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(10)
                await new_page.screenshot(path=str(SS_DIR / "qualified_survey.png"))
                await new_page.close()
            break

        status = qualification.get("status", "?")
        q_text = current_q.get("question", "?")
        answers = current_q.get("answers") or []
        
        log(f"[Q{q_num+1}] Status={status} Q='{q_text[:70]}' Answers={len(answers)}")
        
        if status == "qualified":
            url = qualification.get("url", "")
            log(f"  ✓ QUALIFIED! URL: {url[:120]}")
            if url:
                new_page = await raw.context.new_page()
                await new_page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(10)
                await new_page.screenshot(path=str(SS_DIR / "qualified_survey.png"))
                await new_page.close()
            break

        if status in ("disqualified", "screened_out", "completed"):
            log(f"  Survey ended: {status}")
            break

        if not answers:
            log("  No answers — trying skip button")
            try:
                skip = raw.locator("button").filter(has_text="can't answer")
                if await skip.first.is_visible(timeout=3000):
                    await skip.first.click(timeout=5000)
                    await asyncio.sleep(2)
                    continue
            except Exception:
                pass
            log("  No skip button — stopping")
            break

        # Pick answer
        answer_text = pick_answer(q_text, answers)
        log(f"  Answer: '{answer_text[:60]}'")

        # Wait for radios to render
        await asyncio.sleep(2)

        # Remove popup before clicking
        await page.evaluate(remove_popup_js())

        # Select radio
        clicked = await select_radio(page, answer_text)
        if not clicked:
            log("  Could not select radio — stopping")
            await page.screenshot(path=str(SS_DIR / "radio_fail.png"))
            break

        # Submit
        submitted = await try_submit(page, raw)
        
        if not submitted:
            log("  Submit did not trigger navigation — checking DOM...")
            await asyncio.sleep(2)
            await page.screenshot(path=str(SS_DIR / f"q{q_num+1}_after_submit.png"))

        # Wait for next question to load
        await asyncio.sleep(2)
        
        # Try to capture next qualification response
        # Poll DOM for new question text instead of waiting for POST
        new_question_found = False
        for attempt in range(10):
            await asyncio.sleep(1)
            new_q_text = await page.evaluate(r"""() => {
                const el = document.querySelector('.question-text, [class*="question-title"], [class*="QuestionTitle"]');
                return el?.innerText?.trim()?.substring(0, 80) || '';
            }""")
            if new_q_text and new_q_text != q_text[:80]:
                log(f"  New question detected: '{new_q_text[:60]}'")
                new_question_found = True
                break
            
            # Check for qualified URL
            current_url = page.url
            if "r.prmin.net" in current_url or "cint" in current_url:
                log(f"  → Navigated to survey: {current_url[:100]}")
                new_question_found = True
                break

        if not new_question_found:
            log("  No new question after 10s — stopping")
            break

    # ── Final balance check ──
    log("[FINAL] Checking balance...")
    await page.goto("https://app.primeopinion.com/", wait_until="domcontentloaded")
    await asyncio.sleep(5)
    end_balance = await page.evaluate(r"""() => {
        const el = document.querySelector('.user-balance');
        if (!el) return null;
        const m = el.innerText.match(/\$\s*([\d.]+)/);
        return m ? m[1] : null;
    }""")
    
    log("=" * 60)
    log(f"BEFORE: ${balance}")
    log(f"AFTER:  ${end_balance}")
    if balance and end_balance:
        delta = float(end_balance) - float(balance)
        log(f"DELTA:  ${delta:.2f}")
        if delta > 0:
            log("✅ SUCCESS: Survey credited!")
        else:
            log("❌ Balance unchanged")
    log(f"Traffic events: {len(traffic)}")
    posts = [t for t in traffic if t["m"] == "POST"]
    quals = [t for t in traffic if "qualification" in t["u"]]
    log(f"POSTs: {len(posts)}, /qualification: {len(quals)}")
    for t in posts[-5:]:
        log(f"  POST {t['u'][:120]} → {t['s']}")
    log("=" * 60)

    await backend.stop()
    log("[DONE]")


if __name__ == "__main__":
    asyncio.run(main())
