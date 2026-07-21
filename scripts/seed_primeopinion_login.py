"""Seed Prime Opinion login session using Camoufox.

Launches Camoufox in HEADED mode so you can manually log in.
The session is saved to a persistent profile for reuse by headless runs.

Usage:
    pdm run python scripts/seed_primeopinion_login.py
"""

import asyncio
import sys
from pathlib import Path

from survey_agent.browser import create_backend

PROFILE_DIR = Path(__file__).resolve().parent.parent / "data" / "camoufox_session" / "profile"


async def main():
    print("=" * 60)
    print("PRIME OPINION — SEED LOGIN")
    print("=" * 60)
    print(f"Profile dir: {PROFILE_DIR}")
    print()
    print("A Firefox window will open to the Prime Opinion login page.")
    print()
    print("Steps:")
    print("  1. Enter your email address")
    print("  2. Check your email for the verification code")
    print("  3. Enter the code in the browser")
    print("  4. Wait until you see the surveys dashboard")
    print()
    print("This script will auto-detect when you're logged in.")
    print("Press Ctrl+C to abort at any time.")
    print("=" * 60)
    print()

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    # Clean stale lock files from previous crashes
    for lock_file in PROFILE_DIR.glob("*lock*"):
        lock_file.unlink(missing_ok=True)
    print("Cleaned stale lock files (if any).")

    # humanize=False, geoip=False for seed — avoid fighting with manual input
    backend = create_backend(
        "camoufox",
        headless=False,
        humanize=False,
        geoip=False,
        persistent=True,
        user_data_dir=PROFILE_DIR,
    )

    await backend.start()
    try:
        page = await backend.get_page()

        print("Navigating to Prime Opinion login...")
        await page.goto("https://app.primeopinion.com/app-login", wait_until="domcontentloaded")
        print("Please complete login in the browser window.")
        print()

        # Poll until logged in (up to 5 minutes)
        max_wait = 150  # 300 seconds = 5 minutes
        for i in range(max_wait):
            await asyncio.sleep(2)
            url = await page.url()

            if "login" not in url.lower() and "sign" not in url.lower():
                body = await page.evaluate("() => document.body?.innerText?.substring(0, 200) || ''")
                body_lower = body.lower()

                if "survey" in body_lower or "earn" in body_lower or "dashboard" in body_lower:
                    print(f"\nLOGIN DETECTED!")
                    print(f"  URL: {url}")
                    title = await page.evaluate("() => document.title")
                    print(f"  Title: {title}")

                    # Take success screenshot (wrapper defaults to full_page=True)
                    try:
                        ss_path = PROFILE_DIR / "seed_success.png"
                        await page.screenshot(path=str(ss_path))
                        print(f"  Screenshot: {ss_path}")
                    except Exception as e:
                        print(f"  Screenshot failed (non-fatal): {e}")

                    # Grace period for Firefox to flush cookies to disk
                    print("  Waiting 5s for session to flush...")
                    await asyncio.sleep(5)
                    break

            if i % 15 == 0:
                print(f"  Waiting for login... ({i * 2}s)")
        else:
            print(f"\nTimed out after 5 minutes.")
            print("Session may still be saved if you were partially logged in.")

    finally:
        print("\nClosing browser (session saved to profile)...")
        await backend.stop()

    # Quick verification
    files = list(PROFILE_DIR.rglob("*"))
    file_count = len([f for f in files if f.is_file()])
    print(f"\nProfile directory: {file_count} files")

    print("\n" + "=" * 60)
    print("SEED COMPLETE")
    print("=" * 60)
    print("Next step: verify headless re-launch inherits this session.")
    print("Run: pdm run python scripts/verify_session.py")
    print()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nAborted by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\n\nError: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
