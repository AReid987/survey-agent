#!/usr/bin/env python3
"""
Camoufox smoke test — validates the pivot from Chromium to Camoufox.

Tests:
  1. Browser launches without errors
  2. navigator.webdriver is False (not detected as automation)
  3. Key fingerprint properties look real
  4. Can navigate to survey site without immediate block
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from camoufox.async_api import AsyncCamoufox

STEALTH_CHECK_URL = "https://bot.sannysoft.com/"
PRIME_OPINION_URL = "https://www.primeopinion.com/"
BROWSERSCAN_URL = "https://www.browserscan.net/bot-detection"
OUTPUT_DIR = Path(__file__).parent.parent / "data" / "smoke"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


async def main():
    print("=" * 60)
    print("CAMOUFOX SMOKE TEST")
    print("=" * 60)

    results = {}

    print("\n[1/4] Launching Camoufox...")
    try:
        async with AsyncCamoufox(
            headless=False,
            humanize=True,
            geoip=True,
        ) as browser:
            page = await browser.new_page()
            print("  -> Browser launched OK")

            # --- Test 1: bot.sannysoft.com ---
            print("\n[2/4] Navigating to bot.sannysoft.com...")
            try:
                await page.goto(STEALTH_CHECK_URL, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)
                await page.screenshot(
                    path=str(OUTPUT_DIR / "bot_sannysoft.png"), full_page=True
                )
                print(f"  -> Page title: {await page.title()}")
                print(f"  -> Screenshot saved to data/smoke/bot_sannysoft.png")

                # Extract key fingerprint values
                fingerprint = await page.evaluate("""() => {
                    return {
                        webdriver: navigator.webdriver,
                        userAgent: navigator.userAgent,
                        platform: navigator.platform,
                        languages: navigator.languages,
                        plugins: navigator.plugins.length,
                        mimeTypes: navigator.mimeTypes.length,
                        hardwareConcurrency: navigator.hardwareConcurrency,
                        deviceMemory: navigator.deviceMemory,
                        maxTouchPoints: navigator.maxTouchPoints,
                    };
                }""")
                results["fingerprint"] = fingerprint
                print(f"  -> navigator.webdriver = {fingerprint['webdriver']}")
                print(f"  -> userAgent = {fingerprint['userAgent'][:80]}...")
                print(f"  -> plugins = {fingerprint['plugins']}, mimeTypes = {fingerprint['mimeTypes']}")
                print(f"  -> hardwareConcurrency = {fingerprint['hardwareConcurrency']}")

                if fingerprint["webdriver"] is False or fingerprint["webdriver"] is None:
                    print("  -> PASS: webdriver is not flagged")
                else:
                    print(f"  -> FAIL: webdriver = {fingerprint['webdriver']}")
            except Exception as e:
                print(f"  -> ERROR on bot.sannysoft.com: {e}")

            # --- Test 2: browserscan.net bot detection ---
            print("\n[3/4] Navigating to browserscan.net/bot-detection...")
            try:
                await page.goto(BROWSERSCAN_URL, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(5000)
                await page.screenshot(
                    path=str(OUTPUT_DIR / "browserscan.png"), full_page=True
                )
                print(f"  -> Page title: {await page.title()}")
                print(f"  -> Screenshot saved to data/smoke/browserscan.png")
            except Exception as e:
                print(f"  -> ERROR on browserscan.net: {e}")

            # --- Test 3: Prime Opinion ---
            print("\n[4/4] Navigating to Prime Opinion...")
            try:
                await page.goto(PRIME_OPINION_URL, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)
                await page.screenshot(
                    path=str(OUTPUT_DIR / "prime_opinion.png"), full_page=True
                )
                title = await page.title()
                url = page.url
                print(f"  -> Page title: {title}")
                print(f"  -> Final URL: {url}")

                # Check for common block indicators
                body_text = await page.evaluate("() => document.body?.innerText?.substring(0, 500) || ''")
                blocked = any(kw in body_text.lower() for kw in [
                    "access denied", "blocked", "captcha", "verify you are human",
                    "unusual traffic", "please wait", "checking your browser"
                ])
                if blocked:
                    print(f"  -> POSSIBLE BLOCK detected. Body preview: {body_text[:200]}")
                else:
                    print(f"  -> Page loaded clean. Body preview: {body_text[:200]}")
                print(f"  -> Screenshot saved to data/smoke/prime_opinion.png")
            except Exception as e:
                print(f"  -> ERROR on Prime Opinion: {e}")

            await page.close()

    except Exception as e:
        print(f"  -> FATAL: Failed to launch Camoufox: {e}")
        import traceback
        traceback.print_exc()
        return 1

    # Summary
    print("\n" + "=" * 60)
    print("SMOKE TEST COMPLETE")
    print("=" * 60)
    if results.get("fingerprint"):
        f = results["fingerprint"]
        print(f"  webdriver:   {f['webdriver']}")
        print(f"  plugins:     {f['plugins']}")
        print(f"  user-agent:  {f['userAgent'][:100]}")
    print(f"\n  Screenshots saved to: {OUTPUT_DIR}/")
    print(f"  Open bot_sannysoft.png to visually verify stealth")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
