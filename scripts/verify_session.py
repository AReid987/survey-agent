"""Verify that the seeded Prime Opinion session persists in headless mode.

Run AFTER seed_primeopinion_login.py completes successfully.

Usage:
    pdm run python scripts/verify_session.py
"""

import asyncio
import sys
from pathlib import Path

from survey_agent.browser import create_backend

PROFILE_DIR = Path(__file__).resolve().parent.parent / "data" / "camoufox_session" / "profile"


async def main():
    print("=" * 60)
    print("SESSION VERIFICATION (headless)")
    print("=" * 60)

    if not PROFILE_DIR.exists():
        print(f"ERROR: Profile dir not found: {PROFILE_DIR}")
        print("Run seed_primeopinion_login.py first.")
        sys.exit(1)

    print(f"Profile: {PROFILE_DIR}")
    file_count = len([f for f in PROFILE_DIR.rglob("*") if f.is_file()])
    print(f"Files: {file_count}")
    print()

    backend = create_backend(
        "camoufox",
        headless=True,
        humanize=False,
        geoip=False,
        persistent=True,
        user_data_dir=PROFILE_DIR,
    )

    await backend.start()
    page = await backend.get_page()

    print("Navigating to Prime Opinion surveys page...")
    await page.goto("https://app.primeopinion.com/surveys", wait_until="domcontentloaded")
    await asyncio.sleep(3)

    url = await page.url()
    title = await page.evaluate("() => document.title")
    body = await page.evaluate("() => document.body?.innerText?.substring(0, 500) || ''")

    print(f"URL:   {url}")
    print(f"Title: {title}")
    print(f"Body:  {body[:200]}")
    print()

    is_login = "login" in url.lower() or "sign" in url.lower()

    if is_login:
        print("RESULT: FAIL — still on login page")
        print("The session was not persisted or expired.")
        print("Re-run seed_primeopinion_login.py and try again.")
        await backend.stop()
        sys.exit(1)

    print("RESULT: PASS — logged in successfully in headless mode!")
    print("Session persistence verified. You can now run survey automation.")

    ss_path = PROFILE_DIR / "verify_headless.png"
    await page.screenshot(path=str(ss_path))
    print(f"Screenshot: {ss_path}")

    await backend.stop()
    print("\n" + "=" * 60)
    print("VERIFICATION COMPLETE — CAMOUFOX PIVOT READY")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
