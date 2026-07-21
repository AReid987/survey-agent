"""Navigate through the survey provider interstitial gate.

The provider page (r.prmin.net) shows "Please click to continue. ➔"
This script:
1. Opens survey URL from PO page via window.open (preserves referer)
2. Catches the new page
3. Dumps HTML to find the clickable element
4. Uses native Playwright click (trusted event) on the continue element
5. Inspects what loads after the click
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
    print(f"[gate] {msg}")


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

    # Step 1: Load home and capture qualification
    log("Loading home page...")
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

    await page.goto("https://app.primeopinion.com/", wait_until="domcontentloaded")
    await asyncio.sleep(5)

    # Dismiss device modal
    log("Dismissing device modal...")
    try:
        desktop = raw.get_by_text("Desktop", exact=True)
        if await desktop.is_visible(timeout=3000):
            await desktop.click(timeout=3000)
            await asyncio.sleep(0.5)
    except Exception:
        pass
    try:
        save = raw.locator("button").filter(has_text="Save Selection")
        if await save.first.is_visible(timeout=2000):
            await save.first.click(timeout=3000)
            await asyncio.sleep(2)
    except Exception:
        pass

    # Click survey tile to trigger qualification
    log("Clicking survey tile...")
    qualification = None
    await raw.locator(".list-item.survey-item").first.scroll_into_view_if_needed(timeout=5000)
    await raw.locator(".list-item.survey-item").first.click(timeout=10000)

    for _ in range(20):
        if qualification:
            break
        await asyncio.sleep(0.5)

    if not qualification or "url" not in qualification:
        log("No qualification URL captured!")
        await backend.stop()
        return

    survey_url = qualification["url"]
    log(f"Qualification URL: {survey_url[:80]}...")

    # Step 2: Open survey via window.open (preserves referer)
    log("Opening survey via window.open (preserves referer)...")
    new_page = None
    pages_before = set(raw.context.pages)

    try:
        await page.evaluate(f'window.open("{survey_url}", "_blank")')
    except Exception as e:
        log(f"window.open failed: {e}")
        # Fallback: open in new page directly
        new_page = await raw.context.new_page()

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

    # Wait for the interstitial page to load
    log("Waiting for interstitial page to load...")
    try:
        await new_page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass
    await asyncio.sleep(3)

    # Step 3: Dump the page HTML to understand structure
    log("Dumping interstitial page structure...")
    page_info = await new_page.evaluate("""() => {
        const body = document.body;
        const html = body ? body.innerHTML.substring(0, 3000) : 'NO BODY';
        const text = body ? body.innerText.substring(0, 1000) : 'NO TEXT';
        const links = [...document.querySelectorAll('a')].map(a => ({
            text: a.textContent.trim(),
            href: a.href,
            onclick: a.getAttribute('onclick') || '',
            target: a.target || ''
        }));
        const buttons = [...document.querySelectorAll('button')].map(b => ({
            text: b.textContent.trim(),
            onclick: b.getAttribute('onclick') || ''
        }));
        const divs = [...document.querySelectorAll('div')].filter(d => {
            const t = d.textContent.trim();
            return t.includes('click') || t.includes('continue') || t.includes('Continue');
        }).map(d => ({
            tag: d.tagName,
            cls: d.className,
            text: d.textContent.trim().substring(0, 100),
            onclick: d.getAttribute('onclick') || '',
            role: d.getAttribute('role') || '',
            tabindex: d.getAttribute('tabindex') || '',
            style: d.getAttribute('style') || ''
        }));
        const allClickable = [...document.querySelectorAll('[onclick], [role="button"], a, button, [tabindex]')].map(el => ({
            tag: el.tagName,
            text: el.textContent.trim().substring(0, 50),
            href: el.href || '',
            onclick: (el.getAttribute('onclick') || '').substring(0, 100)
        }));
        return {text, links, buttons, divs, clickable: allClickable, html};
    }""")

    log(f"Page text: {page_info['text'][:200]}")
    log(f"Links: {json.dumps(page_info['links'], indent=2)}")
    log(f"Buttons: {json.dumps(page_info['buttons'], indent=2)}")
    log(f"Matching divs: {json.dumps(page_info['divs'], indent=2)}")
    log(f"All clickable: {json.dumps(page_info['clickable'], indent=2)}")
    log(f"HTML preview: {page_info['html'][:1500]}")

    # Screenshot before click
    await new_page.screenshot(path=str(SS_DIR / "interstitial_before.png"))
    log("Screenshot saved: interstitial_before.png")

    # Step 4: Try multiple click strategies
    log("\n=== CLICKING CONTINUE ELEMENT ===")
    click_strategies = [
        ("Playwright locator: text='click to continue'",
         lambda t=5000: new_page.get_by_text("click to continue").click(timeout=t)),
        ("Playwright locator: text='continue'",
         lambda t=5000: new_page.get_by_text("continue").click(timeout=t)),
        ("Playwright locator: arrow '➔'",
         lambda t=5000: new_page.get_by_text("➔").click(timeout=t)),
        ("CSS: a:has-text('continue')",
         lambda t=5000: new_page.locator("a").filter(has_text="continue").first.click(force=True, timeout=t)),
        ("CSS: [onclick]",
         lambda t=5000: new_page.locator("[onclick]").first.click(force=True, timeout=t)),
        ("CSS: a[href]",
         lambda t=5000: new_page.locator("a[href]").first.click(force=True, timeout=t)),
        ("Coordinate click on visible element",
         lambda t=5000: new_page.locator("body").first.click(position={"x": 400, "y": 300}, timeout=t)),
    ]

    clicked = False
    for name, strategy in click_strategies:
        try:
            log(f"Trying: {name}...")
            await strategy()
            log(f"  ✓ Click succeeded!")
            clicked = True
            break
        except Exception as e:
            log(f"  ✗ Failed: {str(e)[:80]}")
            continue

    if not clicked:
        log("All click strategies failed! Trying evaluate fallback...")
        await new_page.evaluate("""() => {
            // Click anything that looks clickable
            const els = document.querySelectorAll('*');
            for (const el of els) {
                const style = window.getComputedStyle(el);
                if (style.cursor === 'pointer') {
                    el.click();
                    return;
                }
            }
        }""")

    # Step 5: Wait for what happens after click
    log("\nWaiting for post-click state...")
    await asyncio.sleep(8)

    final_url = new_page.url
    title = await new_page.title()
    body = await new_page.evaluate("() => document.body?.innerText?.substring(0, 2000) || 'NO BODY'")
    iframe_count = await new_page.evaluate("() => document.querySelectorAll('iframe').length")

    log(f"Final URL: {final_url}")
    log(f"Title: {title}")
    log(f"Iframes: {iframe_count}")
    log(f"Body: {body[:500]}")

    # Check iframes
    if iframe_count > 0:
        log("\n=== INSPECTING IFRAMES ===")
        for frame in new_page.frames:
            if frame == new_page.main_frame:
                continue
            try:
                f_url = frame.url
                f_body = await frame.evaluate("() => document.body?.innerText?.substring(0, 2000) || ''")
                f_forms = await frame.evaluate("""() => ({
                    radios: document.querySelectorAll('input[type="radio"]').length,
                    checkboxes: document.querySelectorAll('input[type="checkbox"]').length,
                    texts: document.querySelectorAll('input[type="text"], textarea, select').length,
                    buttons: [...document.querySelectorAll('button')].map(b => b.textContent.trim()).filter(t => t).slice(0, 5)
                })""")
                log(f"Frame URL: {f_url[:100]}")
                log(f"Frame body: {f_body[:300]}")
                log(f"Frame forms: {json.dumps(f_forms)}")
            except Exception as e:
                log(f"Frame error: {e}")

    # Check for new pages (popup from popup)
    all_pages = raw.context.pages
    log(f"\nTotal pages in context: {len(all_pages)}")
    for i, p in enumerate(all_pages):
        log(f"  Page {i}: {p.url[:80]}")

    # Screenshot after click
    await new_page.screenshot(path=str(SS_DIR / "interstitial_after.png"))
    log("Screenshot saved: interstitial_after.png")

    # Check for disqualification
    body_lower = body.lower()
    for kw in ["disqualified", "screened out", "quota full", "not eligible", "unfortunately", "blocked"]:
        if kw in body_lower:
            log(f"⚠️  Detected: '{kw}'")

    await new_page.close()
    await backend.stop()
    log("[DONE]")


if __name__ == "__main__":
    asyncio.run(main())
