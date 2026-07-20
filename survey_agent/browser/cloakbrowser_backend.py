"""cloakbrowser backend — Playwright-compatible stealth Chromium for survey automation.

Architecture: a dedicated worker thread owns the Playwright Browser and processes
a queue of commands. The main async backend enqueues commands and waits for results.

This avoids the greenlet/thread-switching issues that come from mixing
asyncio with Playwright's sync API (which uses greenlets bound to one thread).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from queue import Queue, Empty
from typing import Any, Callable

from .backend import AgentResult, SurveyAgentBackend

logger = logging.getLogger(__name__)

AGENT_CHROME_PROFILE = Path(os.environ.get("CHROME_PROFILE", str(Path(__file__).resolve().parent.parent.parent / "data" / "cloak_profile")))
CDP_PORT = int(os.environ.get("CDP_PORT", "19333"))  # Override via env for parallel instances

class CloakBrowserBackend(SurveyAgentBackend):
    """Survey agent backend powered by cloakbrowser (stealth Chromium + Playwright).

    Commands are enqueued to a dedicated worker thread that owns the browser.
    Results are returned via threading primitives, then wrapped in awaitables.
    """

    def __init__(
        self,
        llm_provider: str = "opencode",
        llm_model: str | None = None,
        llm_api_key: str | None = None,
        llm_base_url: str | None = None,
        headless: bool = False,
        google_email: str = "",
        paypal_email: str = "",
        login_email: str = "",
        login_password: str = "",
        timezone: str = "America/New_York",
        locale: str = "en-US",
        profile_dir: str | Path | None = None,
    ):
        self._google_email = google_email
        self._paypal_email = paypal_email
        self._login_email = login_email or google_email
        self._login_password = login_password
        self._llm_provider = llm_provider
        self._llm_model = llm_model
        self._llm_api_key = llm_api_key
        self._llm_base_url = llm_base_url
        self._headless = headless
        self._timezone = timezone
        self._locale = locale
        self._profile_dir = Path(profile_dir) if profile_dir else AGENT_CHROME_PROFILE
        # Worker thread state
        self._worker_thread: threading.Thread | None = None
        self._shutdown_event = threading.Event()
        self._ready = threading.Event()
        self._call_queue: Queue = Queue()
        self._result_event = threading.Event()
        self._result_box: dict = {}

    # ─── Worker thread management ─────────────────────────────────────────

    async def start(self) -> None:
        """Start the worker thread and wait for browser to be ready."""
        logger.info("Starting cloakbrowser worker thread...")
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._shutdown_event.clear()
        self._ready.clear()
        self._call_queue = Queue()
        self._result_event = threading.Event()
        self._result_box = {}

        self._worker_thread = threading.Thread(
            target=self._worker_main, daemon=True, name="cloakbrowser-worker"
        )
        self._worker_thread.start()

        # Wait for ready (use sync wait — this is called from async context)
        await asyncio.get_event_loop().run_in_executor(None, self._ready.wait, 60)
        if not self._ready.is_set():
            raise RuntimeError("cloakbrowser worker thread failed to start within 60s")
        logger.info("cloakbrowser worker ready")

    async def stop(self) -> None:
        """Signal shutdown and wait for worker thread to close browser."""
        self._shutdown_event.set()
        # Put a poison-pill on the queue to wake up the worker
        self._call_queue.put(None)
        if self._worker_thread:
            self._worker_thread.join(timeout=15)
        self._worker_thread = None
        self._ready.clear()

    def _worker_main(self) -> None:
        """Worker thread: own Playwright Browser, process commands sequentially.

        Uses launch_persistent_context to save cookies/sessions to disk.
        Each launch restores the previous session (Google login, Branded login, etc.)
        """
        browser = None
        page = None
        try:
            from cloakbrowser import launch
            # launch_persistent_context saves session to user-data-dir.
            # cloakbrowser's launch() wraps playwright.chromium.launch() — for persistence
            # we need to call launch_persistent_context with our own user-data-dir.
            # Workaround: import the binary path used by cloakbrowser and call directly.
            import os as _os
            from cloakbrowser import browser as _cb_browser
            from playwright.sync_api import sync_playwright

            self._profile_dir.mkdir(parents=True, exist_ok=True)
            # Get cloakbrowser's binary path
            binary_path = _cb_browser.ensure_binary()

            # Resolve timezone/locale/proxy args the same way cloakbrowser does
            timezone = self._timezone
            locale = self._locale
            stealth_args = [
                f"--fingerprint-timezone={timezone}",
                f"--lang={locale}",
            ]
            pw = sync_playwright().start()
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(self._profile_dir),
                executable_path=binary_path,
                headless=self._headless,
                args=stealth_args,
                accept_downloads=True,
            )
            self._ctx = ctx
            if ctx.pages:
                page = ctx.pages[0]
            else:
                page = ctx.new_page()
            self._ready.set()
            logger.info("cloakbrowser launched (persistent context, profile: %s)", self._profile_dir)

            while not self._shutdown_event.is_set():
                try:
                    cmd = self._call_queue.get(timeout=1.0)
                except Empty:
                    continue
                if cmd is None:
                    break
                self._result_box = {}
                try:
                    func, args, kwargs = cmd
                    self._result_box["value"] = func(page, *args, **kwargs)
                except Exception as e:
                    self._result_box["error"] = e
                finally:
                    self._result_event.set()

        except Exception as e:
            logger.error("Worker thread init error: %s", e)
            self._ready.set()
        finally:
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
            logger.info("cloakbrowser worker thread exiting")

    # ─── Command dispatcher (run on worker thread) ────────────────────────

    def _dispatch(self, func: Callable, *args, **kwargs):
        """Enqueue a function to run on the worker thread. Returns result (blocking)."""
        if not self._ready.is_set():
            raise RuntimeError("cloakbrowser worker not ready")
        self._result_event.clear()
        self._result_box = {}
        self._call_queue.put((func, args, kwargs))
        # Wait for result
        self._result_event.wait(timeout=120)
        if not self._result_event.is_set():
            raise TimeoutError("cloakbrowser worker did not respond within 120s")
        if "error" in self._result_box:
            raise self._result_box["error"]
        return self._result_box.get("value")

    # ─── Playwright operations as worker-thread commands ──────────────────

    async def _goto(self, url: str, timeout: int = 30) -> None:
        def _go(page, url, timeout):
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            page.wait_for_timeout(2000)
        await asyncio.get_event_loop().run_in_executor(
            None, self._dispatch, _go, url, timeout
        )

    async def _eval(self, expression: str) -> str:
        def _eval_cmd(page, expr):
            return str(page.evaluate(expr) or "")
        return await asyncio.get_event_loop().run_in_executor(
            None, self._dispatch, _eval_cmd, expression
        )

    async def _click_text(self, text: str, timeout: int = 10) -> bool:
        def _click(page, text, timeout):
            try:
                loc = page.get_by_text(text, exact=False).first
                loc.click(timeout=timeout * 1000)
                return True
            except Exception as e:
                logger.debug("Click by text failed for %r: %s", text, e)
                return False
        return await asyncio.get_event_loop().run_in_executor(
            None, self._dispatch, _click, text, timeout
        )

    async def _click_selector(self, selector: str, timeout: int = 10) -> bool:
        def _click(page, selector, timeout):
            try:
                el = page.query_selector(selector)
                if el:
                    el.click(timeout=timeout * 1000, force=True)
                    return True
                return False
            except Exception as e:
                logger.debug("Click by selector failed for %r: %s", selector, e)
                return False
        return await asyncio.get_event_loop().run_in_executor(
            None, self._dispatch, _click, selector, timeout
        )

    async def _find_google_iframes(self) -> int:
        """Count GSI iframes on the page."""
        def _find(page):
            return len(page.query_selector_all(
                'iframe[src*="accounts.google.com"], iframe[src*="gsi/button"]'
            ))
        return await asyncio.get_event_loop().run_in_executor(None, self._dispatch, _find)

    async def _count_pages(self) -> int:
        """Count pages in the browser context."""
        def _count(page):
            return len(page.context.pages)
        return await asyncio.get_event_loop().run_in_executor(None, self._dispatch, _count)

    async def _get_new_page_url(self, initial_count: int) -> str | None:
        """Return URL of a new page that opened since initial_count, if any."""
        def _get_new(page, initial_count):
            pages = page.context.pages
            if len(pages) > initial_count:
                # Find the newest page (last in list)
                for pg in reversed(pages):
                    if pg != page:
                        return pg.url
            return None
        return await asyncio.get_event_loop().run_in_executor(None, self._dispatch, _get_new, initial_count)

    async def _click_gsi_iframe_button(self) -> bool:
        """Click the Google Sign-In button inside an iframe.

        The GSI iframe is cross-origin (accounts.google.com) and re-renders itself
        rapidly, which detaches DOM nodes between Playwright's locator queries.

        Critical: GSI buttons require a TRUSTED user gesture to open the OAuth
        popup. JS-triggered clicks (btn.click()) are untrusted and silently fail.
        We must use Playwright's mouse.click() with real coordinates, which
        generates trusted input events via CDP.
        """
        def _click(page):
            try:
                iframes = page.query_selector_all(
                    'iframe[src*="accounts.google.com"], iframe[src*="gsi/button"]'
                )
                logger.info("Found %d GSI iframe(s) on page", len(iframes))
                for iframe in iframes:
                    frame = iframe.content_frame()
                    logger.info("Frame: %s", frame)
                    if not frame:
                        continue
                    try:
                        # Wait for the iframe button to render and stabilize
                        page.wait_for_timeout(3000)

                        # Use frame.evaluate to get the button's screen coordinates
                        # relative to the top-level page (Playwright handles the
                        # cross-origin frame offset automatically)
                        coords = frame.evaluate("""() => {
                            const btn = document.querySelector('div[role="button"]');
                            if (!btn) return null;
                            const rect = btn.getBoundingClientRect();
                            return {
                                x: rect.left + rect.width / 2,
                                y: rect.top + rect.height / 2,
                                w: rect.width,
                                h: rect.height
                            };
                        }""")
                        logger.info("Button coords in iframe: %s", coords)
                        if not coords:
                            continue

                        # Translate iframe coords to top-level page coords
                        iframe_box = iframe.bounding_box()
                        if not iframe_box:
                            logger.warning("No iframe bounding box")
                            continue
                        page_x = iframe_box["x"] + coords["x"]
                        page_y = iframe_box["y"] + coords["y"]
                        logger.info("Clicking at page coords (%s, %s)", page_x, page_y)

                        # Real mouse click via Playwright — generates trusted input
                        page.mouse.click(page_x, page_y)
                        return True
                    except Exception as e:
                        logger.warning("Click in iframe failed: %s", e)
                        continue
                return False
            except Exception as e:
                logger.warning("_click_gsi_iframe_button error: %s", e)
                return False
        return await asyncio.get_event_loop().run_in_executor(None, self._dispatch, _click)

    # ─── Google OAuth login ───────────────────────────────────────────────

    async def _login_google_oauth(self, site_url: str) -> AgentResult:
        email = self._google_email
        if not email:
            return AgentResult(success=False, error="No google_email configured for OAuth login")

        await self._goto(site_url)
        await asyncio.sleep(2)

        url = await self._eval("window.location.href")
        if "dashboard" in url.lower():
            return AgentResult(success=True, output="already_logged_in", url=url)

        # Capture initial page count to detect new popups
        initial_pages = await self._count_pages()

        # Click GSI button
        clicked = await self._click_gsi_iframe_button()
        if not clicked:
            if not await self._click_text("Sign in with Google"):
                if not await self._click_text("Continue with Google"):
                    await self._click_text("Log in with Google")

        # Wait for the OAuth popup to close (user completes login manually).
        # We detect this by polling the page count — when it drops back to initial,
        # the popup closed and we can check if login succeeded.
        logger.info("Waiting for OAuth popup to close (user must complete login manually)...")
        for wait_i in range(60):  # up to 60s
            await asyncio.sleep(1)
            current_count = await self._count_pages()
            if current_count <= initial_pages:
                logger.info("Popup closed after %ds, checking auth state...", wait_i + 1)
                break
        else:
            logger.warning("OAuth popup didn't close within 60s")

        # After popup closes, navigate to dashboard to check auth state
        await self._goto(site_url.replace("/login/", "/dashboard").replace("/users/sign_in", "/users/dashboard"))
        await asyncio.sleep(3)

        url = await self._eval("window.location.href")
        text = await self._eval("document.body ? document.body.innerText.toLowerCase() : ''")
        try:
            title = await self._eval("document.title")
        except Exception:
            title = ""
        logger.info("After OAuth: URL=%s title=%s text_preview=%s", url, title, text[:100])

        if "dashboard" in url.lower() or "my points" in text.lower():
            return AgentResult(success=True, output="oauth_popup_login_success", url=url)
        if "accounts.google.com" in url or "sign in - google" in text:
            return AgentResult(success=False, error=f"Still on Google page. URL: {url}", url=url)
        return AgentResult(success=False, error=f"Login didn't complete. URL: {url}", url=url)

        # Google account chooser — click our email
        def _click_account(page, email):
            try:
                js = f"""() => {{
                    var els = document.querySelectorAll('[data-identifier], [data-email], li, div[role=link]');
                    for (var i = 0; i < els.length; i++) {{
                        var t = (els[i].textContent || '').toLowerCase();
                        if (t.includes('{email.lower()}')) {{
                            els[i].click();
                            return true;
                        }}
                    }}
                    return false;
                }}"""
                return page.evaluate(js)
            except Exception:
                return False
        await asyncio.get_event_loop().run_in_executor(
            None, self._dispatch, _click_account, email
        )

        await asyncio.sleep(5)
        url = await self._eval("window.location.href")
        if "dashboard" in url.lower() or site_url.split("//")[1].split("/")[0] in url:
            return AgentResult(success=True, output="oauth_account_click", url=url)

        # Email entry form
        def _fill_email(page, email):
            try:
                email_input = page.query_selector('input[type="email"], #identifierId')
                if not email_input:
                    return False
                email_input.fill(email)
                page.wait_for_timeout(1000)
                next_btn = page.query_selector('#identifierNext, button:has-text("Next")')
                if next_btn:
                    next_btn.click()
                else:
                    form = email_input.evaluate("el => el.closest('form')")
                    if form:
                        page.evaluate("(form) => form.submit()", form)
                return True
            except Exception as e:
                logger.debug("Email entry failed: %s", e)
                return False
        await asyncio.get_event_loop().run_in_executor(
            None, self._dispatch, _fill_email, email
        )
        await asyncio.sleep(8)

        url = await self._eval("window.location.href")
        text = await self._eval("document.body ? document.body.innerText.toLowerCase() : ''")
        if "challenges" in url or ("password" in text and "accounts.google.com" in url):
            return AgentResult(
                success=False,
                error=f"Google password challenge — close Chrome and rerun. URL: {url}",
                url=url,
            )
        if "dashboard" in url.lower() or site_url.split("//")[1].split("/")[0] in url:
            return AgentResult(success=True, output="oauth_email_auto_redirect", url=url)
        return AgentResult(success=False, error=f"OAuth flow didn't complete. URL: {url}", url=url)

    # ─── SurveyAgentBackend abstract methods ───────────────────────────────

    async def login(self, site_url: str, site_name: str) -> AgentResult:
        await self._goto(site_url)
        await asyncio.sleep(3)

        url = await self._eval("window.location.href")
        text = await self._eval("document.body ? document.body.innerText.substring(0, 1000) : ''")
        # Look for explicit auth indicators:
        # - "Log Out" or "Sign Out" link (only shown when logged in)
        # - "My Points" / "My Account" text on the page
        # - URL contains /dashboard or /members/
        has_dashboard = (
            "dashboard" in url.lower()
            or "/members/" in url.lower()
            or "log out" in text.lower()
            or "sign out" in text.lower()
            or "my points" in text.lower()
            or "my account" in text.lower()
        )
        is_login_page = "login" in url.lower() or "sign_in" in url.lower() or "sign-in" in url.lower()

        if has_dashboard and not is_login_page:
            logger.info("Already logged in to %s (URL=%s)", site_name, url)
            return AgentResult(success=True, output="already_logged_in", url=url)

        text_lower = text.lower()
        gsi_count = await self._find_google_iframes()
        use_google_oauth = bool(self._google_email) and (
            "sign in with google" in text_lower
            or "continue with google" in text_lower
            or "log in with google" in text_lower
            or gsi_count > 0
        )

        if use_google_oauth:
            logger.info("Using Google OAuth login for %s (GSI iframes: %d)", site_name, gsi_count)
            return await self._login_google_oauth(site_url)

        if not self._login_email or not self._login_password:
            return AgentResult(success=False, error="No login credentials. Run: survey-agent credentials")

        def _email_login(page, email, password):
            try:
                email_input = page.query_selector('#UserEmail, input[name="email"], input[type="email"]')
                if not email_input:
                    return False, "no email field"
                email_input.fill(email)
                page.wait_for_timeout(500)
                pw_input = page.query_selector('#UserPassword, input[name="password"], input[type="password"]')
                if not pw_input:
                    return False, "no password field"
                pw_input.fill(password)
                page.wait_for_timeout(500)
                submit = page.query_selector('input[type="submit"], button[type="submit"]')
                if submit:
                    submit.click()
                else:
                    form = page.query_selector("form")
                    if form:
                        page.evaluate("(form) => form.submit()", form)
                return True, "submitted"
            except Exception as e:
                return False, str(e)

        ok, msg = await asyncio.get_event_loop().run_in_executor(
            None, self._dispatch, _email_login, self._login_email, self._login_password
        )
        if not ok:
            return AgentResult(success=False, error=f"Email login failed: {msg}")
        await asyncio.sleep(5)
        url = await self._eval("window.location.href")
        if "dashboard" in url.lower():
            return AgentResult(success=True, output="email_login_success", url=url)
        return AgentResult(success=False, error=f"Login unclear after submit. URL: {url}", url=url)

    async def discover_surveys(self, dashboard_url: str) -> AgentResult:
        await self._goto(dashboard_url)
        await asyncio.sleep(3)

        def _discover(page):
            return page.evaluate("""() => {
                // Find ONLY survey tile links — they have /surveys/pre/ or /surveys/take/ in href
                const allLinks = document.querySelectorAll('a[href*="surveys/pre"], a[href*="surveys/take"]');
                const surveys = [];
                const seen = new Set();
                allLinks.forEach(el => {
                    const href = el.href || '';
                    if (seen.has(href)) return;
                    seen.add(href);
                    // Only keep survey tiles, not /take-surveys-online marketing page
                    if (/\\/surveys\\/(pre|take)\\/\\d+\\//.test(href)) {
                        const text = (el.textContent || '').trim();
                        surveys.push({
                            text: text.substring(0, 100),
                            href: href.substring(0, 200)
                        });
                    }
                });
                return {url: window.location.href, title: document.title, surveys: surveys.slice(0, 30)};
            }""")
        info = await asyncio.get_event_loop().run_in_executor(None, self._dispatch, _discover)
        return AgentResult(success=True, output=json.dumps(info), url=info.get("url", ""))

    async def complete_survey(self, survey_info: str, profile_context: str) -> AgentResult:
        """Complete a survey end-to-end.

        survey_info is the JSON list output from discover_surveys, like:
        [{"text": "Take Survey", "href": "https://..."}, ...]
        We pick the first unprocessed survey and complete it.

        Flow (mirrors harness_backend.complete_survey):
        1. Extract first survey URL from survey_info
        2. Navigate to survey URL
        3. Handle pre-survey pages (consent, "begin", cookie buttons) — up to 3 iterations
        4. Loop: read page text → ask LLM for action JSON → apply action → click Next
        5. Detect completion (domain change to branded surveys = success)
        6. Return AgentResult
        """
        # Step 1: Extract first survey URL from survey_info
        survey_url = self._extract_first_survey_url(survey_info)
        if not survey_url:
            return AgentResult(success=False, error="No survey URL found in survey_info")

        logger.info("Completing survey: %s", survey_url)

        # Step 2: Navigate to survey URL
        await self._goto(survey_url)
        await asyncio.sleep(3)

        # Step 3: Pre-survey pages (consent, begin, cookie) — up to 3 iterations
        for _ in range(3):
            pre_clicked = False
            for label in [
                "I Agree", "i agree",
                "Start Survey", "start survey",
                "Begin Survey", "begin survey",
                "Take Survey", "take survey",
                "Continue", "continue",
                "Next", "next",
                "Accept All", "Accept all", "accept all",
                "I Accept", "i accept",
                "OK", "ok",
            ]:
                if await self._click_text(label):
                    pre_clicked = True
                    logger.info("Pre-survey: clicked %r", label)
                    break
            if not pre_clicked:
                break
            await asyncio.sleep(3)

        # Step 4: Main survey loop — ask LLM, apply action, click Next
        completed = False
        last_url = ""
        for page_iter in range(15):  # Max 15 pages per survey
            url = await self._eval("window.location.href")
            text = await self._eval("document.body ? document.body.innerText.substring(0, 3000) : ''")

            # Detect completion: domain changed back to branded surveys
            if "surveys.gobranded.com" in url and "members" not in url and "dashboard" not in url:
                logger.info("Survey completed (domain returned to dashboard): %s", url)
                completed = True
                break
            if "completed" in text.lower() or "thank you" in text.lower() or "redirecting" in text.lower():
                logger.info("Survey completed (completion message detected)")
                completed = True
                break
            if url == last_url:
                logger.info("URL unchanged, breaking loop")
                break
            last_url = url

            # Get page text (top frame)
            page_text = await self._eval("document.body ? document.body.innerText.substring(0, 2000) : ''")

            # Ask LLM for next action
            action_json = await self._ask_llm_for_action(page_text, profile_context)
            if not action_json:
                logger.warning("LLM returned no action, trying to click Next")
                clicked = await self._click_text("Next") or await self._click_text("Submit") or await self._click_text("Continue")
                if not clicked:
                    logger.warning("No Next button found, ending survey")
                    break
                await asyncio.sleep(3)
                continue

            # Apply action: click or fill
            ok = await self._apply_action(action_json)
            if not ok:
                logger.warning("Failed to apply action, clicking Next as fallback")
                await self._click_text("Next")
                await self._click_text("Submit")
                await asyncio.sleep(2)

            # Click Next/Submit to advance
            await asyncio.sleep(1)
            next_clicked = (
                await self._click_text("Next")
                or await self._click_text("next")
                or await self._click_text("Submit")
                or await self._click_text("submit")
                or await self._click_text("Continue")
                or await self._click_text("continue")
            )
            await asyncio.sleep(3)

        # Step 5: Final check
        final_url = await self._eval("window.location.href")
        final_text = await self._eval("document.body ? document.body.innerText.substring(0, 500) : ''")
        if "dashboard" in final_url or "surveys.gobranded.com/users" in final_url:
            completed = True
        return AgentResult(
            success=completed,
            output=f"Survey URL: {survey_url}, final URL: {final_url}, body: {final_text[:200]!r}",
            url=final_url,
        )

    def _extract_first_survey_url(self, survey_info: str) -> str | None:
        """Extract the first REAL survey URL from the discover output JSON.

        Real Branded Surveys URLs look like:
          https://surveys.gobranded.com/surveys/pre/123456/abc?key=xyz
          https://surveys.gobranded.com/surveys/take/123456/abc?key=xyz

        Filters out marketing pages like /take-surveys-online, /blog/,
        /users/preferences, etc.
        """
        try:
            data = json.loads(survey_info)
            if isinstance(data, dict):
                surveys = data.get("surveys", [])
            elif isinstance(data, list):
                surveys = data
            else:
                surveys = []
            # First pass: look for /surveys/pre/ or /surveys/take/ paths
            for s in surveys:
                if not isinstance(s, dict):
                    continue
                href = s.get("href", "")
                if "/surveys/pre/" in href or "/surveys/take/" in href:
                    return href
            # Second pass: any href that looks like a survey (has survey ID pattern)
            for s in surveys:
                if not isinstance(s, dict):
                    continue
                href = s.get("href", "")
                # Real surveys have a 9-digit ID followed by a slug
                import re as _re
                if _re.search(r"/surveys/(pre|take)/\d+/", href):
                    return href
        except Exception as e:
            logger.debug("JSON parse failed: %s", e)
        return None

    async def _ask_llm_for_action(self, page_text: str, profile_context: str) -> str | None:
        """Ask the LLM what action to take on the current page.

        Uses httpx directly to call the opencode.ai API (free tier).
        Returns JSON like: [{"selector": "#radio1", "action": "click", "value": ""}]
        """
        import os
        import httpx

        # Build fallback list — try opencode first (it's free), then config keys
        providers = []
        opencode_key = os.environ.get("OPENCODE_API_KEY", "")
        if opencode_key:
            providers.append({
                "base_url": "https://opencode.ai/zen/v1",
                "api_key": opencode_key,
                "model": "deepseek-v4-flash-free",
            })
        if self._llm_api_key and self._llm_base_url:
            providers.append({
                "base_url": self._llm_base_url,
                "api_key": self._llm_api_key,
                "model": self._llm_model or "gpt-4o",
            })

        if not providers:
            logger.error("No LLM API keys available")
            return None

        prompt = (
            "You are completing an online survey. The user's profile:\n"
            f"{profile_context}\n\n"
            "Current page text:\n"
            "```\n"
            f"{page_text[:1500]}\n"
            "```\n\n"
            "What action should the agent take? Return ONLY a JSON array, no other text.\n"
            'Format: [{"selector": "...", "action": "click"|"fill", "value": "..."}]\n\n'
            "Rules:\n"
            "- Pick the most profile-aligned option for single-choice questions\n"
            "- For text fields, write a thoughtful 1-2 sentence answer\n"
            '- For "Next"/"Submit"/"Continue" buttons, use text:Next, text:Submit, etc.\n'
            "- Limit to 1-3 actions per response\n"
            "- If the page looks complete, return []"
        )

        for prov in providers:
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        f"{prov['base_url']}/chat/completions",
                        headers={"Authorization": f"Bearer {prov['api_key']}"},
                        json={
                            "model": prov["model"],
                            "messages": [{"role": "user", "content": prompt}],
                            "temperature": 0.1,
                            "max_tokens": 500,
                        },
                    )
                    if resp.status_code != 200:
                        logger.warning("LLM %s returned %d: %s", prov["model"], resp.status_code, resp.text[:200])
                        continue
                    data = resp.json()
                    response = data["choices"][0]["message"]["content"].strip()
                    logger.info("LLM response (%s): %s", prov["model"], response[:200])
                    # Strip code fences
                    if response.startswith("```"):
                        match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", response, re.DOTALL)
                        if match:
                            response = match.group(1)
                    # Find JSON array
                    match = re.search(r"\[.*?\]", response, re.DOTALL)
                    if match:
                        return match.group(0)
                    return response
            except Exception as e:
                logger.warning("LLM call to %s failed: %s", prov["model"], e)
                continue
        return None

    async def _apply_action(self, action_json: str) -> bool:
        """Apply a list of actions to the current page. Returns True if any succeeded."""
        try:
            actions = json.loads(action_json)
        except Exception as e:
            logger.warning("Bad action JSON: %s", e)
            return False

        applied_any = False
        for action in actions:
            selector = action.get("selector", "")
            act = action.get("action", "click")
            value = action.get("value", "")
            try:
                if act == "click":
                    # Try as selector first, then as text
                    clicked = await self._click_selector(selector) if selector.startswith(("#", ".", "[", "button", "input")) else await self._click_text(selector)
                    if clicked:
                        applied_any = True
                        logger.info("Clicked: %s", selector)
                elif act == "fill":
                    def _fill(page, selector, value):
                        try:
                            el = page.query_selector(selector)
                            if el:
                                el.fill(value)
                                return True
                            return False
                        except Exception:
                            return False
                    filled = await asyncio.get_event_loop().run_in_executor(None, self._dispatch, _fill, selector, value)
                    if filled:
                        applied_any = True
                        logger.info("Filled: %s", selector)
            except Exception as e:
                logger.warning("Action %r failed: %s", action, e)
        return applied_any

    async def check_balance(self, dashboard_url: str) -> AgentResult:
        await self._goto(dashboard_url)
        await asyncio.sleep(3)

        def _read_balance(page):
            return page.evaluate("""() => {
                const text = document.body ? document.body.innerText : '';
                let m = text.match(/my\\s*points[:\\s]*([\\d,]+)/i);
                if (m) return {balance: parseInt(m[1].replace(/,/g, '')), currency: 'pts'};
                m = text.match(/\\$\\s*([\\d,]+\\.?\\d*)/);
                if (m) return {balance: parseFloat(m[1].replace(/,/g, '')), currency: 'USD'};
                return {balance: null, currency: null};
            }""")
        bal = await asyncio.get_event_loop().run_in_executor(None, self._dispatch, _read_balance)
        return AgentResult(success=True, output=json.dumps(bal))

    async def withdraw(self, redeem_url: str, method: str = "paypal") -> AgentResult:
        await self._goto(redeem_url)
        return AgentResult(success=True, output=f"Navigated to redeem page: {redeem_url}")