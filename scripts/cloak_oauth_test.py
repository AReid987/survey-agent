"""cloakbrowser backend Google OAuth test.

Run this AFTER closing your regular Chrome (so cloakbrowser inherits the Google session).
Tests the cloakbrowser backend's _login_google_oauth against Branded Surveys.
"""
import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
sys.path.insert(0, "/Users/antonioreid/survey-agent")


async def main():
    from survey_agent.browser.cloakbrowser_backend import CloakBrowserBackend
    from survey_agent.config import load_config
    from pathlib import Path
    import os

    # Load config
    config = load_config()

    # Use CHROME_PROFILE env var if set, else default cloak profile
    profile_dir = os.environ.get(
        "CHROME_PROFILE",
        "/Users/antonioreid/survey-agent/data/cloak_profile",
    )

    # Create backend using harness-specific LLM settings (opencode, deepseek)
    harness_llm = config.harness_llm_settings()
    backend = CloakBrowserBackend(
        llm_provider=harness_llm["llm_provider"],
        llm_model=harness_llm["llm_model"],
        llm_api_key=harness_llm["llm_api_key"],
        llm_base_url=harness_llm["llm_base_url"],
        headless=False,
        google_email=config.account.google_email,
        paypal_email=config.account.paypal_email,
        profile_dir=profile_dir,
    )

    print("Starting cloakbrowser backend...", flush=True)
    await backend.start()
    print("Backend started.", flush=True)

    # Test Google OAuth login on Branded Surveys
    print("\n[1] Testing Google OAuth login on Branded Surveys...", flush=True)
    result = await backend.login(
        site_url="https://surveys.gobranded.com/users/login/",
        site_name="Branded Surveys",
    )
    print(f"  Login result: success={result.success} error={result.error!r}", flush=True)
    print(f"  URL: {result.url}", flush=True)
    print(f"  Output: {result.output[:200]!r}", flush=True)

    if result.success:
        print("\n[2] Discovering surveys...", flush=True)
        disc = await backend.discover_surveys(
            dashboard_url="https://surveys.gobranded.com/users/dashboard",
        )
        print(f"  Discovery: {disc.output[:500]!r}", flush=True)

    print("\n[3] Stopping backend...", flush=True)
    await backend.stop()
    print("Done.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())