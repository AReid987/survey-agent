"""Attempt to enter a Prime Opinion survey.

Loads the home page, dismisses device modal, clicks a survey tile,
captures the qualification API response, and navigates to the survey
provider in a new page.

Usage:
    pdm run python scripts/attempt_survey.py
"""

import asyncio
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from survey_agent.browser import create_backend

PROFILE_DIR = Path("data/camoufox_session/profile")
SS_DIR = Path("data/smoke")


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

    # Grant popup permissions
    try:
        await raw.context.grant_permissions(["popups", "notifications"])
        print("[0] Popup permissions granted")
    except Exception as e:
        print(f"[0] Permission grant failed (non-fatal): {e}")

    # Register response listener
    qualification = None

    async def on_resp(resp):
        nonlocal qualification
        if "qualification" in resp.url and not qualification:
            try:
                body = await resp.text()
                qualification = json.loads(body)
                print(f"  >>> QUALIFICATION: {json.dumps(qualification, indent=2)[:500]}")
            except Exception as e:
                print(f"  >>> Qualification read error: {e}")

    raw.on("response", lambda r: asyncio.ensure_future(on_resp(r)))

    # 1. Load home
    print("[1] Loading home page...")
    await page.goto("https://app.primeopinion.com/", wait_until="domcontentloaded")
    await asyncio.sleep(5)

    # 2. Dismiss device modal
    print("[2] Dismissing device selector modal...")
    try:
        desktop = raw.get_by_text("Desktop", exact=True)
        if await desktop.is_visible(timeout=3000):
            await desktop.click(timeout=3000)
            print("    Desktop clicked")
            await asyncio.sleep(0.5)
    except Exception as e:
        print(f"    Desktop click: {e}")

    try:
        save = raw.get_by_role("button", name="Save Selection")
        if await save.is_visible(timeout=2000):
            await save.click(timeout=3000)
            print("    Save Selection clicked")
            await asyncio.sleep(2)
    except Exception:
        # Fallback: try generic Save
        try:
            save2 = raw.locator("button").filter(has_text="Save")
            if await save2.first.is_visible(timeout=2000):
                await save2.first.click(timeout=3000)
                print("    Save (fallback) clicked")
                await asyncio.sleep(2)
        except Exception as e:
            print(f"    Save click: {e}")

    # 3. Count and list survey tiles
    tiles = await page.evaluate(
        """() => {
        const items = document.querySelectorAll('.list-item.survey-item');
        return [...items].slice(0, 10).map((t, i) => ({
            index: i,
            text: t.innerText.trim().substring(0, 150)
        }));
    }"""
    )
    print(f"[3] Found {len(tiles)} survey tiles (showing first 10):")
    for t in tiles[:5]:
        print(f"    [{t['index']}] {t['text'][:80]}")

    # 4. Click first survey tile
    print("[4] Clicking first survey tile...")
    qualification = None  # Reset
    try:
        await raw.locator(".list-item.survey-item").first.scroll_into_view_if_needed(timeout=5000)
        await raw.locator(".list-item.survey-item").first.click(timeout=10000)
        print("    Click succeeded")
    except Exception as e:
        print(f"    Click failed: {e}")
        await backend.stop()
        return

    # Wait for qualification response
    for i in range(20):
        if qualification:
            break
        await asyncio.sleep(0.5)

    if not qualification or "url" not in qualification:
        print("[4] ERROR: No qualification URL captured!")
        await backend.stop()
        return

    survey_url = qualification["url"]
    print(f"[5] Got qualification URL: {survey_url}")
    print(f"    Status: {qualification.get('status')}")
    print(f"    Redirect type: {qualification.get('redirect_type')}")

    # 6. Open survey URL in a NEW page (not same page — preserve PO session)
    print("[6] Opening survey in new page...")
    new_page = await raw.context.new_page()
    try:
        await new_page.goto(survey_url, wait_until="networkidle", timeout=60000)
    except Exception as e:
        print(f"    Navigation warning: {e}")
        # Continue anyway — we may have landed somewhere useful
    await asyncio.sleep(10)

    # 7. Capture landing page
    try:
        final_url = new_page.url
        title = await new_page.title()
        body = await new_page.evaluate("() => document.body?.innerText?.substring(0, 3000) || 'NO BODY'")
        iframe_count = await new_page.evaluate("() => document.querySelectorAll('iframe').length")
    except Exception as e:
        print(f"[7] Error reading page: {e}")
        await backend.stop()
        return

    print(f"[7] Landed at: {final_url}")
    print(f"    Title: {title}")
    print(f"    Iframes: {iframe_count}")
    print(f"    Body text ({len(body)} chars):")
    print(body[:2000])

    # Check for iframes
    if iframe_count > 0:
        frames = await new_page.evaluate(
            """() => {
            return [...document.querySelectorAll('iframe')].map(f => ({
                src: f.src || '',
                id: f.id || '',
                name: f.name || '',
                width: f.offsetWidth,
                height: f.offsetHeight,
                visible: f.offsetWidth > 0
            }));
        }"""
        )
        print(f"    Iframe details:")
        for f in frames:
            print(f"      src={f['src'][:100]} id={f['id']} visible={f['visible']} {f['width']}x{f['height']}")

    # Look for survey form elements
    form_elements = await new_page.evaluate(
        """() => {
        const radios = document.querySelectorAll('input[type="radio"]');
        const checkboxes = document.querySelectorAll('input[type="checkbox"]');
        const textInputs = document.querySelectorAll('input[type="text"], textarea');
        const selects = document.querySelectorAll('select');
        const buttons = [...document.querySelectorAll('button')].map(b => b.textContent.trim()).filter(t => t);
        return {
            radios: radios.length,
            checkboxes: checkboxes.length,
            textInputs: textInputs.length,
            selects: selects.length,
            buttons: buttons.slice(0, 10)
        };
    }"""
    )
    print(f"    Form elements: {json.dumps(form_elements)}")

    # Check for disqualification / error
    body_lower = body.lower()
    for keyword in ["disqualified", "screened out", "quota full", "not eligible", "error", "blocked", "unfortunately"]:
        if keyword in body_lower:
            print(f"    ⚠️  Detected keyword: '{keyword}'")

    # Screenshot
    await new_page.screenshot(path=str(SS_DIR / "survey_provider.png"))
    print("    Screenshot: data/smoke/survey_provider.png")

    # Don't close — let us inspect
    await new_page.close()
    await backend.stop()
    print("[DONE]")


if __name__ == "__main__":
    asyncio.run(main())
