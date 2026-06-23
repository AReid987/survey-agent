"""Google OAuth login handler — handles 'Sign in with Google' flows."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ..browser.human_behavior import HumanBehavior

if TYPE_CHECKING:
    from ..browser.engine import BrowserPage

logger = logging.getLogger(__name__)


class GoogleOAuthHandler:
    """Handle Google OAuth login flows for survey sites."""

    def __init__(self, google_email: str, behavior: HumanBehavior | None = None):
        self._email = google_email
        self._behavior = behavior or HumanBehavior()

    async def login_via_google(self, page: BrowserPage) -> bool:
        """Click the Google sign-in button and complete the OAuth flow.

        On first run (headed mode), the user will need to manually sign in.
        The persistent browser session saves the cookies for future runs.
        """
        logger.info("Looking for Google sign-in button...")

        # Try multiple selectors for the Google button
        google_btn = None
        selectors = [
            "button:has-text('Google')",
            "button:has-text('Sign in with Google')",
            "a:has-text('Google')",
            "a:has-text('Sign in with Google')",
            "[class*='google']",
            "[class*='gsi']",
            "[data-provider='google']",
            "div[role='button']:has-text('Google')",
        ]

        for sel in selectors:
            try:
                google_btn = await page.query_selector(sel)
                if google_btn:
                    logger.info(f"Found Google button with selector: {sel}")
                    break
            except Exception:
                continue

        if not google_btn:
            # Broader search
            logger.info("Trying broader button search...")
            all_buttons = await page.query_selector_all("button, a, div[role='button']")
            for btn in all_buttons:
                try:
                    text = await btn.evaluate("el => el.textContent?.toLowerCase() || ''")
                    if "google" in text or "gmail" in text or "sign in with" in text:
                        google_btn = btn
                        logger.info(f"Found Google button via text match: '{text.strip()[:50]}'")
                        break
                except Exception:
                    continue

        if not google_btn:
            logger.warning("No Google sign-in button found on page")
            content = await page.content()
            # Log all button/link text for debugging
            all_buttons = await page.query_selector_all("button, a, input[type='submit']")
            button_texts = []
            for btn in all_buttons:
                try:
                    t = await btn.evaluate("el => el.textContent?.trim() || el.value || ''")
                    if t:
                        button_texts.append(t[:60])
                except Exception:
                    pass
            logger.info(f"Available buttons/links: {button_texts}")
            return False

        # Click the Google button
        await self._behavior.random_delay(0.5)
        logger.info("Clicking Google sign-in button...")
        await google_btn.click()
        await self._behavior.random_delay(2.0)

        url = await page.url()
        logger.info(f"After Google click, URL: {url}")

        # Handle the Google page
        return await self._handle_google_page(page)

    async def _handle_google_page(self, page: BrowserPage) -> bool:
        """Handle the Google login/account chooser page."""
        url = await page.url()
        logger.info(f"Handling Google page: {url}")

        # Check if we're already redirected back (already signed in)
        if "accounts.google.com" not in url:
            logger.info("Not on Google page — already authenticated or redirected")
            return True

        # Wait for Google's login UI to load
        try:
            await page.wait_for_selector(
                "input[type='email'], #identifierId, [data-email], "
                ".BHzsHc, div[data-email], .L5Fo6c-P1ILyc",
                timeout=10,
            )
            logger.info("Google login UI loaded")
        except Exception:
            logger.warning("Timed out waiting for Google login UI")
            # Check if we're already past Google
            url = await page.url()
            if "accounts.google.com" not in url:
                return True
            return False

        await self._behavior.random_delay(1.0)

        # Check for account chooser (already signed into Google before)
        account_option = await page.query_selector(
            f"[data-email='{self._email}'], "
            f"div[data-email='{self._email}'], "
            f".BHzsHc[data-email='{self._email}']"
        )

        if account_option:
            logger.info(f"Found account chooser entry for {self._email}")
            await account_option.click()
            await self._behavior.random_delay(2.0)
            return await self._handle_consent(page)

        # Check for list of accounts (different selector patterns)
        account_items = await page.query_selector_all(
            ".BHzsHc, div[data-email], li[data-email], .zZVDYe"
        )
        for item in account_items:
            email = await item.evaluate("el => el.getAttribute('data-email') || el.textContent || ''")
            if self._email in str(email):
                logger.info(f"Found account in list: {email}")
                await item.click()
                await self._behavior.random_delay(2.0)
                return await self._handle_consent(page)

        # Need to type email
        logger.info(f"No existing account found, typing email: {self._email}")
        email_input = await page.query_selector(
            "input[type='email'], #identifierId, input[name='identifier']"
        )
        if email_input:
            selector = await email_input.evaluate(
                "el => { el.id = el.id || 'g-email-' + Math.random().toString(36).slice(2); return '#' + el.id; }"
            )
            await self._behavior.human_type(page, selector, self._email)
            await self._behavior.random_delay(0.5)

            # Click Next
            next_btn = await page.query_selector(
                "#identifierNext, button:has-text('Next'), button[type='submit']"
            )
            if next_btn:
                logger.info("Clicking Next after email entry")
                await next_btn.click()
                await self._behavior.random_delay(3.0)
            else:
                logger.warning("Could not find Next button after email entry")

            # Handle password page or consent
            return await self._handle_password_or_consent(page)

        logger.warning("Could not find email input on Google page")
        return False

    async def _handle_password_or_consent(self, page: BrowserPage) -> bool:
        """Handle post-email step — could be password, consent, or redirect."""
        url = await page.url()
        logger.info(f"Post-email step, URL: {url}")

        # Check if we're already past Google
        if "accounts.google.com" not in url:
            logger.info("Already past Google — authenticated")
            return True

        # Check for password input
        pw_input = await page.query_selector(
            "input[type='password'], input[name='password']"
        )
        if pw_input:
            # Google wants a password. In headed mode, the user needs to type it.
            # We don't store Google passwords for security.
            logger.info("Google is asking for password — waiting for user to complete manually...")
            logger.info("(In headed mode, please type your Google password in the browser window)")

            # Wait for the user to complete the password step
            # Poll until we leave accounts.google.com
            for _ in range(120):  # 2 minutes max
                await asyncio.sleep(1)
                url = await page.url()
                if "accounts.google.com" not in url:
                    logger.info("User completed Google sign-in")
                    return True
                # Check if password page is gone
                still_pw = await page.query_selector("input[type='password']")
                if not still_pw:
                    # Might be on consent now
                    break

            logger.info("Password step completed, checking next step...")

        return await self._handle_consent(page)

    async def _handle_consent(self, page: BrowserPage) -> bool:
        """Handle the Google consent/authorize screen."""
        await self._behavior.random_delay(1.0)
        url = await page.url()
        logger.info(f"Checking consent screen: {url}")

        if "accounts.google.com" not in url:
            return True

        # Look for Continue/Allow/Authorize button
        consent_btn = await page.query_selector(
            "button:has-text('Continue'), button:has-text('Allow'), "
            "button:has-text('Authorize'), button#submit_approve_access, "
            "input[type='submit'][value*='Allow'], "
            "button[type='submit']"
        )
        if consent_btn:
            text = await consent_btn.evaluate("el => el.textContent?.trim() || el.value || ''")
            logger.info(f"Found consent button: '{text}', clicking...")
            await consent_btn.click()
            await self._behavior.random_delay(3.0)

        # Wait for redirect back to survey site
        for _ in range(30):
            url = await page.url()
            if "accounts.google.com" not in url:
                logger.info(f"Redirected back to: {url}")
                return True
            await asyncio.sleep(1)

        url = await page.url()
        logger.warning(f"Still on Google after consent: {url}")
        return "accounts.google.com" not in url

    async def is_google_page(self, page: BrowserPage) -> bool:
        url = await page.url()
        return "accounts.google.com" in url
