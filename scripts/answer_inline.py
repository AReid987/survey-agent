"""Inline survey answering loop for Prime Opinion.

Flow: click tile → /qualification returns accepted + questions.current →
answer radio → click gradient submit → POST /qualification → next question
→ repeat until qualified+URL or disqualified.

KEY LESSONS (verified by terminal):
  - Radio value='on' (NOT the answer UUID) — match by label text only
  - Submit button is "CLAIM REWARD" (.p-btn--gradient) — NOT "Next"/"Submit"
  - React requires Playwright native .click() — JS eval .click() doesn't
    trigger React's synthetic onChange, so the submit button stays disabled
  - .last avoids stale tile buttons behind the overlay
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
    print(f"[survey] {msg}", flush=True)


async def get_balance(page) -> float | None:
    data = await page.evaluate(r"""() => {
        const el = document.querySelector('.user-balance');
        if (!el) return null;
        const m = el.innerText.match(/\$\s*([\d.]+)/);
        return m ? m[1] : null;
    }""")
    return float(data) if data else None


def pick_answer(question: dict, _profile: dict) -> str | None:
    """Pick answer ID based on question text and user profile.

    Profile: Antonio Reid, 39M, self-employed CTO, Austin TX, 3 kids (4/5/5),
    $50-75K household, Bachelor's degree, Black, rent, former smoker, never drinks.
    """
    q_text = (question.get("question") or "").lower()
    answers = question.get("answers", [])
    if not answers:
        return None

    def match(*keywords):
        for a in answers:
            text = (a.get("text") or "").lower()
            for kw in keywords:
                if kw in text:
                    return a["id"]
        return None

    # Ordered by specificity — first match wins
    rules = [
        # (condition, keywords)
        ("smartphone" in q_text, ("android",)),
        ("occupation" in q_text or "sector" in q_text or "industry" in q_text,
         ("technology", "information", "computer", "software", "high tech", "internet")),
        ("company" in q_text and ("employ" in q_text or "people" in q_text or "size" in q_text),
         ("1 to 4", "1-4", "fewer than 10", "1 to 9", "under 10", "sole", "self")),
        ("age" in q_text and "household" not in q_text, ("35", "30-39", "35-44")),
        ("gender" in q_text or "sex" in q_text, ("male",)),
        ("income" in q_text or "household" in q_text,
         ("50,000", "50000", "50-75", "50,000 - 74,999", "50000 - 74999")),
        ("state" in q_text or "reside" in q_text, ("texas",)),
        ("how many" in q_text and ("children" in q_text or "kids" in q_text), ("3",)),
        ("children" in q_text or "kids" in q_text or "child" in q_text, ("yes",)),
        ("education" in q_text or "degree" in q_text, ("bachelor", "college")),
        ("race" in q_text or "ethnicity" in q_text, ("black", "african")),
        ("marital" in q_text or "married" in q_text or "relationship status" in q_text, ("married",)),
        ("do you own or rent" in q_text or "housing" in q_text
         or ("own" in q_text and "rent" in q_text), ("rent",)),
        ("employment status" in q_text or "are you employed" in q_text or "currently work" in q_text,
         ("self-employed", "self employed", "freelance", "independent")),
        ("smok" in q_text, ("no",)),
        ("alcohol" in q_text or "drink" in q_text, ("no",)),
        ("recommend" in q_text or "nps" in q_text or "how likely" in q_text, None),  # special
        ("purchas" in q_text or "decision" in q_text, ("final approval", "decision maker", "sole")),
    ]

    for cond, keywords in rules:
        if not cond:
            continue
        if keywords is None:
            # NPS: match exact numbers to avoid "10" matching "100+"
            for target in ["10", "9", "8"]:
                for a in answers:
                    if (a.get("text") or "").strip() == target:
                        return a["id"]
        else:
            r = match(*keywords)
            if r:
                return r

    # Generic fallbacks
    for a in answers:
        text = (a.get("text") or "").lower()
        if "does not apply" in text or "none of the above" in text:
            return a["id"]
    for a in answers:
        if "other" in (a.get("text") or "").lower():
            return a["id"]

    return answers[0]["id"] if answers else None


async def main():
    backend = create_backend("camoufox", headless=True, persistent=True,
                             user_data_dir=PROFILE_DIR, humanize=False, geoip=False)
    await backend.start()
    page = await backend.get_page()
    raw = page._page

    # ── POST body logging ──
    post_bodies = []

    def capture_post(req):
        if "qualification" in req.url and req.method == "POST":
            body = req.post_data
            post_bodies.append(body)
            log(f"  >>> POST body: {(body or '(empty)')[:500]}")

    raw.on("request", capture_post)

    # Step 1: Load home + balance
    log("[1] Loading PO home...")
    await page.goto("https://app.primeopinion.com/", wait_until="domcontentloaded")
    await asyncio.sleep(5)
    start_balance = await get_balance(page)
    log(f"    Balance: ${start_balance}")

    # Step 2: Dismiss device modal
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

    # Step 3: Click first tile + capture qualification
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
    except Exception as e:
        log(f"ERROR: {str(e)[:200]}")
        await backend.stop()
        return

    # Step 4: Answer loop
    max_questions = 15
    for q_num in range(max_questions):
        status = qualification.get("status")
        log(f"[Q{q_num + 1}] Status: {status}")

        if status == "qualified":
            url = qualification.get("url")
            if url:
                log(f"    ✓ QUALIFIED! Survey URL: {url[:100]}")
                new_page = await raw.context.new_page()
                await new_page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(10)
                survey_body = await new_page.evaluate(
                    "() => document.body?.innerText?.substring(0, 1000) || ''"
                )
                log(f"    Survey page: {survey_body[:300]}")
                await new_page.screenshot(path=str(SS_DIR / "external_survey.png"))
                await new_page.close()
                break
            else:
                log("    Qualified but no URL — completed inline?")
                break

        if status in ("disqualified", "screened_out", "completed", "success"):
            log(f"    Survey ended: {status}")
            break

        questions = qualification.get("questions", {})
        current_q = questions.get("current")
        if not current_q:
            log("    No current question — end of survey?")
            log(f"    Response: {json.dumps(qualification)[:300]}")
            break

        q_text = current_q.get("question", "?")
        answers = current_q.get("answers", [])
        is_skippable = current_q.get("is_skippable", False)

        log(f"    Q: {q_text[:80]}")
        log(f"    Type: {current_q.get('type')}, Answers: {len(answers)}, Skippable: {is_skippable}")

        if not answers and is_skippable:
            skip_btn = raw.locator("button").filter(has_text="can't answer")
            if await skip_btn.first.is_visible(timeout=3000):
                await skip_btn.first.click(timeout=5000)
                log("    Skipped")
                await asyncio.sleep(2)
                continue

        if not answers:
            log("    No answers and not skippable")
            break

        # Pick answer
        answer_id = pick_answer(current_q, {})
        chosen = next((a for a in answers if a["id"] == answer_id), answers[0])
        log(f"    Chosen: {chosen.get('text', '?')[:60]}")

        # Step 5: Select radio via Playwright native click
        # (JS .click() doesn't trigger React onChange — verified by terminal)
        await asyncio.sleep(2)

        radio_info = await page.evaluate(r"""() => {
            const radios = [...document.querySelectorAll('input[type="radio"]')];
            return radios.map(r => {
                let label = r.labels?.[0]?.textContent?.trim() || '';
                if (!label) {
                    const p = r.closest('[class*="answer"], [class*="option"], label, li, div');
                    if (p) label = p.innerText?.trim()?.substring(0, 80) || '';
                }
                return { id: r.id, label, visible: r.offsetWidth > 0 || r.offsetHeight > 0 };
            }).filter(r => r.visible);
        }""")

        clicked = False
        chosen_text = (chosen.get("text") or "").strip().lower()

        if radio_info:
            log(f"    Found {len(radio_info)} visible radios")

            # Strategy 1: Match by label text, click via JS getElementById
            # (raw.locator("#id") fails for digit-first IDs — verified by terminal)
            for r in radio_info:
                label = (r.get("label") or "").strip().lower()
                if not label:
                    continue
                if chosen_text == label or chosen_text in label or label in chosen_text:
                    try:
                        rid = r["id"]
                        await page.evaluate(f"""() => {{
                            const r = document.getElementById('{rid}');
                            if (!r) return false;
                            const label = r.closest('label') || r.parentElement;
                            if (label) {{ label.click(); return true; }}
                            r.click();
                            return true;
                        }}""")
                        clicked = True
                        log(f"    ✓ Clicked radio: '{r['label'][:50]}'")
                        break
                    except Exception as e:
                        log(f"    Click failed: {str(e)[:60]}")

            # Strategy 2: Fallback — click first visible radio
            if not clicked and radio_info:
                first = radio_info[0]
                try:
                    fid = first["id"]
                    await page.evaluate(f"""() => {{
                        const r = document.getElementById('{fid}');
                        if (!r) return false;
                        const label = r.closest('label') || r.parentElement;
                        if (label) {{ label.click(); return true; }}
                        r.click();
                        return true;
                    }}""")
                    clicked = True
                    log(f"    ✓ Clicked first radio: '{first['label'][:50]}'")
                except Exception as e:
                    log(f"    Fallback click failed: {e}")

            # Verify selection + dispatch change event for React reactivity
            await asyncio.sleep(0.5)
            await page.evaluate("""() => {
                const checked = document.querySelectorAll('input[type="radio"]:checked');
                checked.forEach(r => {
                    r.dispatchEvent(new Event('change', {bubbles: true}));
                    r.dispatchEvent(new Event('input', {bubbles: true}));
                });
            }""")
            is_checked = await page.evaluate(
                r"""() => document.querySelectorAll('input[type="radio"]:checked').length"""
            )
            log(f"    Checked radios: {is_checked}")
        else:
            log("    No visible radios!")

        if not clicked:
            log("    Could not click any radio — breaking")
            await page.screenshot(path=str(SS_DIR / "radio_fail.png"))
            break

        # Step 6: Submit answer
        await asyncio.sleep(1)
        submit_clicked = False

        # Remove popup overlay that intercepts ALL Playwright clicks
        await page.evaluate("document.querySelector('.popup.integration-script-popup')?.remove()")
        log("    Removed popup overlay")

        # Primary: p-btn--fill appears ONLY after radio selection (real submit)
        try:
            fill_btn = raw.locator("button.p-btn--fill:visible")
            if await fill_btn.count() > 0:
                try:
                    async with raw.expect_response(
                        lambda resp: "qualification" in resp.url, timeout=15000
                    ) as resp_info:
                        await fill_btn.last.click(timeout=5000, force=True)
                        log("    Clicked fill submit button")
                    response = await resp_info.value
                    qualification = await response.json()
                    submit_clicked = True
                    log(f"    Next response status: {qualification.get('status')}")
                except Exception:
                    log("    No qualification response after fill click")
        except Exception as e:
            log(f"    Fill button error: {e}")

        # Fallback: text-based search
        if not submit_clicked:
            for btn_text in ["Claim Reward", "CLAIM REWARD", "Next", "Submit", "Continue"]:
                try:
                    btns = raw.locator(f"button:visible:has-text('{btn_text}')")
                    if await btns.count() > 0:
                        async with raw.expect_response(
                            lambda resp: "qualification" in resp.url, timeout=15000
                        ) as resp_info:
                            await btns.last.click(timeout=5000)
                            log(f"    Clicked '{btn_text}' button")
                        response = await resp_info.value
                        qualification = await response.json()
                        submit_clicked = True
                        break
                except Exception:
                    await asyncio.sleep(1)
                    continue

        if not submit_clicked:
            log("    Could not submit — breaking")
            await page.screenshot(path=str(SS_DIR / "submit_fail.png"))
            break

    # Step 7: Final balance check
    log("[FINAL] Checking balance...")
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
    log(f"POST bodies captured: {len(post_bodies)}")
    log("=" * 60)

    await backend.stop()
    log("[DONE]")


if __name__ == "__main__":
    asyncio.run(main())
