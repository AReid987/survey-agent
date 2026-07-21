"""Answer the quality gate question and verify crediting.

Proven selectors from HTML dump:
- Textarea: #free_text_answer (placeholder "Your answer here")
- Submit:   #submit_button (<button>Submit</button>)

Flow:
1. Check balance BEFORE
2. Load PO home, dismiss device modal
3. Click survey tile, capture /qualification response
4. Open survey URL in new page (preserves referer via window.open)
5. Wait for rdsecured.com/landing2 quality gate
6. Fill textarea with native Playwright .fill()
7. Click Submit with native Playwright locator
8. Wait for response, capture result
9. Check balance AFTER
"""

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from survey_agent.browser import create_backend

PROFILE_DIR = Path("data/camoufox_session/profile")
SS_DIR = Path("data/smoke")


def log(msg):
    print(f"[gate] {msg}")


async def get_balance(page) -> float | None:
    """Extract balance from PO home page."""
    data = await page.evaluate("""() => {
        const el = document.querySelector('.user-balance');
        if (!el) return null;
        const m = el.innerText.match(/\\$\\s*([\\d.]+)/);
        return m ? m[1] : null;
    }""")
    return float(data) if data else None


async def main():
    backend = create_backend(
        "camoufox",
        headless=True,
        persistent=True,
        user_data_dir=PROFILE_DIR,
        humanize=False,
        geoip=False,
    )
    await backend.start()
    page = await backend.get_page()
    raw = page._page

    # ── Step 1: Check starting balance ──
    log("Step 1: Loading PO home for balance check...")
    await page.goto("https://app.primeopinion.com/", wait_until="domcontentloaded")
    await asyncio.sleep(5)
    start_balance = await get_balance(page)
    log(f"Starting balance: ${start_balance}")

    # ── Step 2: Dismiss device modal ──
    log("Step 2: Dismissing device modal...")
    try:
        desktop = raw.get_by_text("Desktop", exact=True)
        if await desktop.is_visible(timeout=3000):
            await desktop.click(timeout=3000)
            await asyncio.sleep(0.5)
            log("  Desktop clicked")
    except Exception:
        log("  No Desktop button")
    try:
        save = raw.locator("button").filter(has_text="Save Selection")
        if await save.first.is_visible(timeout=2000):
            await save.first.click(timeout=3000)
            await asyncio.sleep(2)
            log("  Save Selection clicked")
    except Exception:
        log("  No Save button")

    # ── Step 3: Capture qualification ──
    log("Step 3: Clicking survey tile...")
    qualification = None

    async def on_resp(resp):
        nonlocal qualification
        if "qualification" in resp.url and not qualification:
            try:
                body = await resp.text()
                qualification = json.loads(body)
            except Exception:
                pass

    raw.on("response", lambda r: asyncio.ensure_future(on_resp(r)))

    try:
        await raw.locator(".list-item.survey-item").first.scroll_into_view_if_needed(timeout=5000)
        await raw.locator(".list-item.survey-item").first.click(timeout=10000)
        log("  Click succeeded")
    except Exception as e:
        log(f"  Click failed: {e}")
        await backend.stop()
        return

    for _ in range(20):
        if qualification:
            break
        await asyncio.sleep(0.5)

    if not qualification or "url" not in qualification:
        log("ERROR: No qualification URL captured!")
        await backend.stop()
        return

    survey_url = qualification["url"]
    log(f"  Qualification URL: {survey_url[:80]}...")
    log(f"  Status: {qualification.get('status')}")

    # ── Step 4: Open survey in new page ──
    log("Step 4: Opening survey via window.open...")
    new_page = None
    pages_before = set(raw.context.pages)

    try:
        await page.evaluate(f'window.open("{survey_url}", "_blank")')
    except Exception as e:
        log(f"  window.open failed: {e}")

    # Wait for new page to appear
    for _ in range(30):
        new_pages = set(raw.context.pages) - pages_before
        if new_pages:
            new_page = new_pages.pop()
            break
        await asyncio.sleep(0.5)

    if not new_page:
        log("No new page detected, using context.new_page fallback")
        new_page = await raw.context.new_page()
        await new_page.goto(survey_url, wait_until="domcontentloaded", timeout=60000)

    log("  Waiting for page to load...")
    try:
        await new_page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass
    await asyncio.sleep(5)

    # ── Step 5: Check what we landed on ──
    current_url = new_page.url
    title = await new_page.title()
    body_text = await new_page.evaluate("() => document.body?.innerText?.substring(0, 2000) || ''")
    log(f"  Landed URL: {current_url[:100]}")
    log(f"  Title: {title}")
    log(f"  Body preview: {body_text[:200]}")

    # Look for quality gate question
    if "quality reasons" in body_text.lower() or "following question" in body_text.lower():
        log("  ✓ Quality gate detected!")
    elif "please click to continue" in body_text.lower():
        log("  ⚠ Interstitial gate detected — clicking continue...")
        # Click the continue element with native locator
        try:
            link = new_page.locator("a[href]").first
            await link.click(timeout=5000)
            await asyncio.sleep(8)
            current_url = new_page.url
            body_text = await new_page.evaluate("() => document.body?.innerText?.substring(0, 2000) || ''")
            log(f"  After click URL: {current_url[:100]}")
            log(f"  After click body: {body_text[:200]}")
        except Exception as e:
            log(f"  Continue click failed: {e}")
    else:
        log(f"  ⚠ Unknown page state")

    # ── Step 6: Fill the textarea ──
    log("Step 5: Filling answer textarea...")
    # Try multiple selectors
    textarea_selectors = [
        "#free_text_answer",
        "textarea",
        "textarea[name='free_text_answer']",
        "input[name='free_text_answer']",
    ]
    filled = False
    for sel in textarea_selectors:
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
            continue

    if not filled:
        log("  ✗ Could not fill textarea — trying evaluate fallback")
        filled = await new_page.evaluate("""() => {
            const ta = document.querySelector('textarea, input[type="text"]');
            if (!ta) return false;
            ta.value = "I would live in Kyoto, Japan because of its rich cultural heritage, beautiful temples, amazing food, and the perfect blend of traditional and modern lifestyle.";
            ta.dispatchEvent(new Event('input', {bubbles: true}));
            ta.dispatchEvent(new Event('change', {bubbles: true}));
            return true;
        }""")
        log(f"  Evaluate fallback: {'ok' if filled else 'failed'}")

    if not filled:
        log("ERROR: Could not find or fill any textarea!")
        await new_page.screenshot(path=str(SS_DIR / "debug_no_textarea.png"))
        await new_page.close()
        await backend.stop()
        return

    await asyncio.sleep(1)

    # ── Step 7: Click Submit ──
    log("Step 6: Clicking Submit...")
    submit_selectors = [
        "#submit_button",
        "button:has-text('Submit')",
        "button[type='submit']",
    ]
    submitted = False
    for sel in submit_selectors:
        try:
            btn = new_page.locator(sel)
            if await btn.count() > 0:
                await btn.first.click(timeout=5000)
                log(f"  ✓ Clicked via {sel}")
                submitted = True
                break
        except Exception as e:
            log(f"  {sel} failed: {str(e)[:60]}")
            continue

    if not submitted:
        log("  ✗ Locator strategies failed — trying evaluate fallback")
        submitted = await new_page.evaluate("""() => {
            const btns = document.querySelectorAll('button');
            for (const btn of btns) {
                const t = btn.textContent.trim().toLowerCase();
                if (t.includes('submit')) {
                    btn.click();
                    return true;
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

    # ── Step 8: Wait for result ──
    log("Step 7: Waiting for submission result...")
    await asyncio.sleep(10)

    result_url = new_page.url
    result_body = await new_page.evaluate("() => document.body?.innerText?.substring(0, 2000) || ''")
    log(f"  Result URL: {result_url[:100]}")
    log(f"  Result body: {result_body[:500]}")

    # Check for success
    body_lower = result_body.lower()
    success_keywords = ["thank you", "earned", "congratulations", "completed", "response recorded"]
    success = any(kw in body_lower for kw in success_keywords)
    log(f"  Success indicators: {[kw for kw in success_keywords if kw in body_lower]}")

    await new_page.screenshot(path=str(SS_DIR / "after_submit.png"))
    log("  Screenshot: data/smoke/after_submit.png")

    # ── Step 9: Check ending balance ──
    log("Step 8: Checking ending balance...")
    await new_page.close()

    # Navigate back to PO
    await page.goto("https://app.primeopinion.com/", wait_until="domcontentloaded")
    await asyncio.sleep(5)
    end_balance = await get_balance(page)
    log(f"Ending balance: ${end_balance}")

    # ── Report ──
    log("=" * 60)
    log(f"BEFORE:  ${start_balance}")
    log(f"AFTER:   ${end_balance}")
    if start_balance is not None and end_balance is not None:
        delta = end_balance - start_balance
        log(f"DELTA:   ${delta:.2f}")
        if delta > 0:
            log("✅ SUCCESS: Survey completed and credited!")
        else:
            log("❌ Balance did not increase")
    else:
        log("❌ Could not verify balance change")
    log(f"SUCCESS TEXT: {success}")
    log("=" * 60)

    await backend.stop()
    log("[DONE]")


if __name__ == "__main__":
    asyncio.run(main())
