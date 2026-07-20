"""Ego Lite browser backend for survey automation.

Ego Lite (https://lite.ego.app) is a free, open-source Chromium-based browser
designed for AI agents. It can inherit your real Chrome data (cookies, logins,
extensions) on first launch, solving the Google OAuth problem for Prime Opinion.

Prerequisites:
1. Install Ego Lite app: https://lite.ego.app (free DMG download)
2. Run it once, migrate Chrome data when prompted
3. Install ego-browser CLI: copy from GitHub releases or use npx

Usage:
    from .ego_backend import EgoLiteBackend
    backend = EgoLiteBackend()
    await backend.start()
    result = await backend.complete_survey(survey_info, profile_context)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .backend import AgentResult, SurveyAgentBackend

logger = logging.getLogger(__name__)

EGO_BROWSER_BIN = os.environ.get("EGO_BROWSER_PATH", os.path.expanduser("~/.local/bin/ego-browser"))
EGO_NODE = "/opt/homebrew/bin/node"  # Node 26+ required
EGO_BROWSER_TIMEOUT = 120  # seconds per command


def _check_node_version() -> bool:
    """Check if Node.js is new enough for ego-browser (needs 22+)."""
    try:
        result = subprocess.run(
            [EGO_NODE, "--version"], capture_output=True, text=True, timeout=5, check=False
        )
        if result.returncode == 0:
            ver = result.stdout.strip().lstrip("v")
            major = int(ver.split(".")[0])
            return major >= 22
    except (FileNotFoundError, ValueError, subprocess.TimeoutExpired):
        pass
    return False


def _check_installed() -> tuple[bool, str]:
    """Check if ego-browser CLI is available and compatible."""
    try:
        result = subprocess.run(
            ["which", EGO_BROWSER_BIN], capture_output=True, text=True, timeout=5, check=False
        )
        if result.returncode != 0:
            return False, "ego-browser not found on PATH"
        if not _check_node_version():
            return False, "Node.js >=22 required (current: {})".format(
                subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=5).stdout.strip()
            )
        return True, "ready"
    except Exception as e:
        return False, str(e)


async def _run_ego(
    script: str,
    timeout: float = EGO_BROWSER_TIMEOUT,
) -> tuple[str, int]:
    """Run a Node.js heredoc script through ego-browser.

    The script is a Node.js program that uses ego-browser's built-in helpers:
    - snapshotText(), click('@N'), fillInput('@N', text)
    - openOrReuseTab(url), gotoAndWait(url)
    - js(expression), cliLog(value)
    """
    # Write script to temp file
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", delete=False, prefix="ego_"
    ) as f:
        f.write(script)
        script_path = f.name

    try:
        # Read script and pass via -e flag (ego-browser doesn't accept file args)
        with open(script_path) as f:
            script_content = f.read()

        proc = await asyncio.create_subprocess_exec(
            EGO_BROWSER_BIN,
            "nodejs",
            "-e",
            script_content,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        Path(script_path).unlink(missing_ok=True)

        # cliLog output goes to stderr
        output = stderr.decode("utf-8", errors="replace")
        err_output = stdout.decode("utf-8", errors="replace")
        if err_output.strip():
            logger.debug("ego-browser stdout: %s", err_output[:200])

        return output, proc.returncode or 0
    except asyncio.TimeoutError:
        Path(script_path).unlink(missing_ok=True)
        return "TIMEOUT", 1
    except FileNotFoundError:
        Path(script_path).unlink(missing_ok=True)
        return "ego-browser not found. Install Ego Lite from https://lite.ego.app", 1


def _build_snapshot_script(url: str) -> str:
    """Build a script that navigates and takes a snapshot."""
    return f"""
const task = await useOrCreateTaskSpace('survey automation');
await openOrReuseTab('{url}', {{ wait: true, timeout: 20 }});
await waitForLoad();
await new Promise(r => setTimeout(r, 3000));

const pageSnap = await snapshotText();
const pageUrl = (await currentTab()).url;
const pageTitle = (await pageInfo()).title;

cliLog(JSON.stringify({{
    url: pageUrl,
    title: pageTitle,
    snapshot: pageSnap,
}}));
await completeTaskSpace(task.name);
"""


def _build_action_script(actions: list[dict]) -> str:
    """Build a script that executes survey actions (click, fill, select)."""
    action_lines = []
    for i, action in enumerate(actions):
        act = action.get("action", "click")
        sel = action.get("selector", "")
        val = action.get("value", "")
        label = action.get("label", f"action_{i}")

        if act == "click":
            if sel.startswith("@"):
                action_lines.append(f"await click('{sel}', {{ label: '{label}' }});")
            else:
                action_lines.append(f"await click('{sel}', {{ label: '{label}' }});")
        elif act == "fill":
            escaped_val = val.replace("'", "\\'").replace("\\", "\\\\")
            action_lines.append(f"await fillInput('{sel}', '{escaped_val}');")
        elif act == "select":
            action_lines.append(f"await fillInput('{sel}', '{val}');")
        elif act == "type":
            escaped_val = val.replace("'", "\\'").replace("\\", "\\\\")
            action_lines.append(f"await typeText('{escaped_val}');")
        elif act == "press":
            action_lines.append(f"await pressKey('{val}');")

    actions_code = "\n  ".join(action_lines) if action_lines else "// no actions"

    return f"""
const task = await useOrCreateTaskSpace('survey answering');
await waitForLoad();
await new Promise(r => setTimeout(r, 2000));

{actions_code}

await new Promise(r => setTimeout(r, 1500));
const pageSnap = await snapshotText();
const pageUrl = (await currentTab()).url;

cliLog(JSON.stringify({{
    url: pageUrl,
    snapshot: pageSnap,
}}));
await completeTaskSpace(task.name);
"""


class EgoLiteBackend(SurveyAgentBackend):
    """Browser backend powered by Ego Lite.

    Requires:
    - Ego Lite app installed (https://lite.ego.app)
    - Chrome data migrated on first launch
    - ego-browser CLI on PATH
    - Node.js >=22
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
    ):
        self._llm_provider = llm_provider
        self._llm_model = llm_model
        self._llm_api_key = llm_api_key
        self._llm_base_url = llm_base_url
        self._headless = headless
        self._google_email = google_email
        self._paypal_email = paypal_email
        self._login_email = login_email
        self._login_password = login_password

    async def start(self) -> None:
        """Verify ego-browser is available."""
        available, msg = _check_installed()
        if not available:
            raise RuntimeError(
                f"Ego Lite not ready: {msg}\n"
                "Install from https://lite.ego.app and ensure ego-browser is on PATH."
            )
        logger.info("Ego Lite backend ready (Node.js OK, ego-browser found)")

    async def stop(self) -> None:
        """Clean up — leave Ego Lite running (user may use it)."""
        logger.info("Ego Lite backend stopped (browser left running)")

    async def login(self, site_url: str, site_name: str) -> AgentResult:
        """Log into a survey site using Ego Lite.

        First checks if already logged in (inherited Chrome session).
        If on login page, fills credentials using ego-browser helpers.
        """
        login_url = site_url.rstrip("/") + "/users/login/"
        script = _build_snapshot_script(login_url)
        output, rc = await _run_ego(script, timeout=60)

        if rc != 0:
            return AgentResult(success=False, error=f"ego-browser error (rc={rc}): {output[:200]}")

        try:
            data = json.loads(output.strip())
            url = data.get("url", "")
            snapshot = data.get("snapshot", "")

            # Already logged in
            if "dashboard" in url.lower():
                return AgentResult(success=True, output="already_logged_in")

            # Login page — fill credentials
            if "login" in url.lower() or "sign" in url.lower():
                email = self._login_email or self._google_email
                password = self._login_password

                if not email or not password:
                    # Try Google OAuth button (inherited Chrome session)
                    if "Sign in as Antonio" in snapshot or "read.musik@gmail.com" in snapshot:
                        # Click the Google sign-in button
                        oauth_script = f"""
const task = await useOrCreateTaskSpace('survey automation');
const pageSnap2 = await snapshotText();
const lines = pageSnap2.split(String.fromCharCode(10));
for (const line of lines) {{
    if (line.includes('Sign in as') || line.includes('read.musik@gmail.com')) {{
        const refMatch = line.match(/\\[ref=(\\d+)/);
        if (refMatch) {{
            await click('@' + refMatch[1], {{ label: 'click google sign-in' }});
            cliLog('clicked google sign-in');
            break;
        }}
    }}
}}
await new Promise(r => setTimeout(r, 5000));
const finalUrl = (await currentTab()).url;
cliLog(JSON.stringify({{ url: finalUrl }}));
"""
                        output2, rc2 = await _run_ego(oauth_script, timeout=30)
                        if rc2 == 0:
                            try:
                                result = json.loads(output2.strip().split("\n")[-1])
                                if "dashboard" in result.get("url", "").lower():
                                    return AgentResult(success=True, output="logged_in_via_google")
                            except (json.JSONDecodeError, IndexError):
                                pass

                    return AgentResult(
                        success=False,
                        error="Login page shown. No credentials provided and Google OAuth failed.",
                    )

                # Fill email/password
                login_script = f"""
const task = await useOrCreateTaskSpace('survey automation');
await fillInput('input[name="email"]', '{email}');
await fillInput('input[name="password"]', '{password}');
await new Promise(r => setTimeout(r, 500));
await click('button[type="submit"], input[type="submit"]', {{ label: 'click sign in' }});
await new Promise(r => setTimeout(r, 5000));
const finalUrl = (await currentTab()).url;
cliLog(JSON.stringify({{ url: finalUrl }}));
"""
                output3, rc3 = await _run_ego(login_script, timeout=30)
                if rc3 == 0:
                    try:
                        result = json.loads(output3.strip().split("\n")[-1])
                        final_url = result.get("url", "")
                        if "dashboard" in final_url.lower() or "survey" in final_url.lower():
                            return AgentResult(success=True, output=f"logged_in: {final_url}")
                        return AgentResult(success=False, error=f"Login failed, still at: {final_url}")
                    except (json.JSONDecodeError, IndexError):
                        pass

                return AgentResult(success=False, error="Login form submission failed")

            # Some other page — might be logged in
            return AgentResult(success=True, output=f"navigated to {url}")
        except json.JSONDecodeError:
            return AgentResult(success=True, output=output)

    async def discover_surveys(self, dashboard_url: str) -> AgentResult:
        """Find available surveys on the dashboard."""
        script = _build_snapshot_script(dashboard_url)
        output, rc = await _run_ego(script, timeout=60)

        if rc != 0:
            return AgentResult(success=False, error=f"ego-browser error: {output[:200]}")

        # Ego Lite snapshots include refs for interactive elements
        try:
            data = json.loads(output.strip())
            snapshot = data.get("snapshot", "")
            has_surveys = bool(re.search(r"(take survey|start|begin|\d+\s*pts)", snapshot, re.I))
            return AgentResult(
                success=True,
                output=output if has_surveys else "no surveys available",
            )
        except json.JSONDecodeError:
            return AgentResult(success=True, output=output)

    async def complete_survey(self, survey_info: str, profile_context: str) -> AgentResult:
        """Complete a survey using Ego Lite's snapshot-based interaction.

        Uses gotoAndWait() for navigation and js() for clicking to avoid
        CDP Input.dispatchMouseEvent timeouts on survey pages.
        """
        from ..llm_provider import get_llm, PROVIDERS  # noqa: PLC0415
        from ..config import load_config  # noqa: PLC0415

        cfg = load_config()
        llm = get_llm(
            provider=cfg.llm_provider,
            model=cfg.llm_model or PROVIDERS.get(cfg.llm_provider, {}).get("default_model", ""),
            api_key=cfg.llm_api_key,
            base_url=cfg.llm_base_url or None,
            temperature=0.1,
        )

        # Step 1: Get survey URL from dashboard via JS (no CDP click)
        dashboard_url = "https://surveys.gobranded.com/users/dashboard"
        get_url_script = f"""
const task = await useOrCreateTaskSpace('survey answering');
await openOrReuseTab('{dashboard_url}', {{ wait: true, timeout: 30 }});
await waitForLoad();
await new Promise(r => setTimeout(r, 5000));
const surveyUrl = await js(String.raw`(function(){{
    var link = document.querySelector('a[href*="/surveys/pre/"]');
    return link ? link.href : '';
}})()`);
cliLog(surveyUrl);
"""
        output, rc = await _run_ego(get_url_script, timeout=45)
        if rc != 0:
            return AgentResult(success=False, error=f"Dashboard failed: {output[:200]}")

        survey_url = output.strip().split("\n")[-1].strip()
        if not survey_url or "http" not in survey_url:
            return AgentResult(success=True, output="No surveys available on dashboard")

        logger.info(f"Survey URL: {survey_url[:80]}")

        # Step 2: Navigate to survey pre-page and click TAKE SURVEY then BEGIN SURVEY
        start_survey_script = f"""
const task = await useOrCreateTaskSpace('survey answering');
await gotoAndWait('{survey_url}', {{ timeout: 30, settle: 5 }});
await new Promise(r => setTimeout(r, 3000));
// Click TAKE SURVEY via JS
await js(String.raw`(function(){{
    var els = document.querySelectorAll('a, button, input[type="submit"]');
    for (var i = 0; i < els.length; i++) {{
        var text = (els[i].textContent||els[i].value||'').trim().toUpperCase();
        if (text === 'TAKE SURVEY') {{ els[i].click(); return 'clicked'; }}
    }}
    return 'not found';
}})()`);
await new Promise(r => setTimeout(r, 8000));
// Click BEGIN SURVEY if present (second pre-survey page)
await js(String.raw`(function(){{
    var els = document.querySelectorAll('a, button, input[type="submit"]');
    for (var i = 0; i < els.length; i++) {{
        var text = (els[i].textContent||els[i].value||'').trim().toUpperCase();
        if (text === 'BEGIN SURVEY' || text === 'START SURVEY') {{ els[i].click(); return 'clicked'; }}
    }}
    return 'not found';
}})()`);
await new Promise(r => setTimeout(r, 10000));
// Get survey page content
const body = await js(String.raw`(function(){{
    try {{ return document.body ? document.body.innerText.substring(0, 3000) : 'no body'; }}
    catch(e) {{ return 'err: ' + e.message; }}
}})()`);
cliLog('BODY: ' + body);
"""
        output, rc = await _run_ego(start_survey_script, timeout=60)
        if rc != 0:
            return AgentResult(success=False, error=f"Survey start failed: {output[:200]}")

        # Parse the output
        body_text = ""
        for line in output.strip().split("\n"):
            if line.startswith("BODY: "):
                body_text = line[6:]

        # Step 3: Handle consent/agreement pages before question loop
        consent_script = """
const task = await useOrCreateTaskSpace('survey answering');
// Check for consent checkbox and click Accept
const consentResult = await js(String.raw`(function(){
    // Check the confirmation checkbox if present
    var cb = document.getElementById('confirmationCheckbox');
    if (cb && !cb.checked) { cb.click(); return 'checked_and_accepting'; }
    // Also try generic agreement checkboxes
    var cbs = document.querySelectorAll('input[type="checkbox"]');
    for (var i = 0; i < cbs.length; i++) {
        if (!cbs[i].checked && cbs[i].offsetParent !== null) {
            cbs[i].click();
            break;
        }
    }
    // Click Accept button
    var btns = document.querySelectorAll('button, input[type="submit"]');
    for (var i = 0; i < btns.length; i++) {
        var text = (btns[i].textContent||btns[i].value||'').trim().toUpperCase();
        if (text === 'ACCEPT' || text === 'AGREE' || text === 'I AGREE' || text === 'CONTINUE') {
            btns[i].click();
            return 'accepted';
        }
    }
    return 'no_consent_page';
})()`);
cliLog('CONSENT: ' + consentResult);
await new Promise(r => setTimeout(r, 8000));
// Get page after consent
const body = await js(String.raw`(function(){
    try { return document.body ? document.body.innerText.substring(0, 3000) : 'no body'; }
    catch(e) { return 'err: ' + e.message; }
})()`);
cliLog('BODY: ' + body);
"""
        output, rc = await _run_ego(consent_script, timeout=30)
        if rc == 0:
            for line in output.strip().split("\n"):
                if line.startswith("BODY: "):
                    body_text = line[6:]
            if "CONSENT:" in output:
                logger.info(f"Consent: {[l for l in output.split(chr(10)) if 'CONSENT:' in l][0].strip()}")

        if "not found" in output.lower():
            return AgentResult(success=False, error="TAKE SURVEY button not found")

        # Step 4: Answer questions using js() for all interactions
        max_questions = 50
        last_question = ""
        same_count = 0

        for i in range(max_questions):
            # Get current page content and interactive elements
            get_state_script = """
const task = await useOrCreateTaskSpace('survey answering');
const body = await js(String.raw`(function(){
    try { return document.body ? document.body.innerText.substring(0, 3000) : 'no body'; }
    catch(e) { return 'err: ' + e.message; }
})()`);
const elements = await js(String.raw`(function(){
    var els = document.querySelectorAll('input[type="radio"], input[type="checkbox"], select, textarea, input[type="text"]');
    var result = [];
    for (var i = 0; i < els.length && i < 30; i++) {
        var el = els[i];
        var label = '';
        var parent = el.closest('label');
        if (parent) label = parent.textContent.trim().substring(0, 80);
        if (!label) {
            var lbl = document.querySelector('label[for="' + el.id + '"]');
            if (lbl) label = lbl.textContent.trim().substring(0, 80);
        }
        result.push({tag: el.tagName, type: el.type||'', name: el.name||'', id: el.id||'', label: label, visible: el.offsetParent !== null, value: el.value||''});
    }
    return JSON.stringify(result);
})()`);
cliLog('BODY: ' + body);
cliLog('ELEMENTS: ' + elements);
"""
            output, rc = await _run_ego(get_state_script, timeout=30)
            if rc != 0:
                await asyncio.sleep(2)
                continue

            # Parse output
            body_text = ""
            elements_json = "[]"
            for line in output.strip().split("\n"):
                if line.startswith("BODY: "):
                    body_text = line[6:]
                elif line.startswith("ELEMENTS: "):
                    elements_json = line[10:]

            # Check for completion/disqualification
            body_lower = body_text.lower()
            if any(kw in body_lower for kw in ["thank you", "survey complete", "successfully completed"]):
                return AgentResult(success=True, output=f"Survey completed after {i} questions")
            if any(kw in body_lower for kw in ["not a match", "disqualified", "screened out"]):
                return AgentResult(success=False, error="Disqualified from survey")

            # Parse elements
            try:
                elements = json.loads(elements_json)
            except json.JSONDecodeError:
                elements = []

            if not elements:
                await asyncio.sleep(2)
                continue

            # Find question text from body
            question_text = ""
            for line in body_text.split("\n"):
                line = line.strip()
                if len(line) > 15 and ('?' in line or 'select' in line.lower() or 'how' in line.lower()):
                    question_text = line[:300]
                    break
                if len(line) > 20 and not question_text:
                    question_text = line[:300]

            # Build options list with labels
            options = []
            for el in elements:
                if not el.get("visible"):
                    continue
                label = el.get("label", "").strip()
                if not label or len(label) < 2:
                    continue
                if any(skip in label.lower() for skip in ["dashboard", "profile", "logout", "menu"]):
                    continue
                options.append({
                    "name": el.get("name", ""),
                    "id": el.get("id", ""),
                    "type": el.get("type", ""),
                    "label": label,
                    "value": el.get("value", ""),
                })

            if not options:
                # Also look for links/buttons that might be answer options
                link_get_script = """
const task = await useOrCreateTaskSpace('survey answering');
const els = await js(String.raw`(function(){
    var els = document.querySelectorAll('a, button, [onclick], [role="button"]');
    var result = [];
    for (var i = 0; i < els.length && i < 30; i++) {
        var el = els[i];
        var text = (el.textContent||'').trim().substring(0, 80);
        if (text && text.length > 1 && el.offsetParent !== null) {
            result.push({tag: el.tagName, text: text, href: el.href||''});
        }
    }
    return JSON.stringify(result);
})()`);
cliLog('LINKS: ' + els);
"""
                link_output, link_rc = await _run_ego(link_get_script, timeout=15)
                if link_rc == 0:
                    for line in link_output.strip().split("\n"):
                        if line.startswith("LINKS: "):
                            try:
                                link_els = json.loads(line[7:])
                                for el in link_els:
                                    text = el.get("text", "").strip()
                                    if text and len(text) > 1 and len(text) < 60:
                                        if any(skip in text.lower() for skip in ["dashboard", "profile", "logout", "menu", "cookie", "privacy", "terms"]):
                                            continue
                                        options.append({"name": "", "id": "", "type": "link", "label": text, "value": el.get("href", "")})
                            except json.JSONDecodeError:
                                pass

            if not options:
                await asyncio.sleep(2)
                continue

            # Stuck detection — same question 3 times in a row
            if question_text == last_question:
                same_count += 1
                if same_count >= 3:
                    # Try scrolling to reveal more content
                    scroll_script = """
const task = await useOrCreateTaskSpace('survey answering');
await js(String.raw`(function(){ window.scrollBy(0, 500); return 'scrolled'; })()`);
await new Promise(r => setTimeout(r, 1000));
"""
                    await _run_ego(scroll_script, timeout=10)
                    same_count = 0
            else:
                same_count = 0
            last_question = question_text

            # Use LLM to pick the best answer
            prompt = f"""Answer this survey question as the user described below.

USER PROFILE:
{profile_context}

QUESTION: {question_text}

OPTIONS:
{json.dumps(options[:20], indent=2)}

Reply with ONLY the label text of the best option.
For text inputs, reply with "TEXT: your answer here"."""

            try:
                from browser_use.llm.messages import UserMessage  # noqa: PLC0415
                result = await llm.ainvoke([UserMessage(content=prompt)])
                answer = (getattr(result, "completion", None) or str(result)).strip()
            except Exception as e:
                logger.warning(f"LLM call failed: {e}")
                answer = ""

            if not answer or answer == "None":
                await asyncio.sleep(1)
                continue

            logger.info(f"Survey Q{i+1}: {question_text[:50]} → {answer[:50]}")

            # Apply answer via js()
            if answer.startswith("TEXT:"):
                text_val = answer[5:].strip().replace("'", "\\'").replace('"', '\\"')
                apply_script = f"""
const task = await useOrCreateTaskSpace('survey answering');
await js(String.raw`(function(){{
    var inputs = document.querySelectorAll('input[type="text"], textarea');
    for (var i = 0; i < inputs.length; i++) {{
        if (inputs[i].offsetParent !== null) {{
            inputs[i].value = '{text_val}';
            inputs[i].dispatchEvent(new Event('input', {{bubbles: true}}));
            inputs[i].dispatchEvent(new Event('change', {{bubbles: true}}));
            return 'filled';
        }}
    }}
    return 'no input found';
}})()`);
cliLog('applied');
"""
                await _run_ego(apply_script, timeout=15)
            else:
                # Click the matching option — handles radio, checkbox, AND links
                answer_clean = answer.strip().replace("'", "\\'").replace('"', '\\"')
                click_script = f"""
const task = await useOrCreateTaskSpace('survey answering');
await js(String.raw`(function(){{
    // First try radio/checkbox
    var els = document.querySelectorAll('input[type="radio"], input[type="checkbox"]');
    for (var i = 0; i < els.length; i++) {{
        var parent = els[i].closest('label');
        var label = parent ? parent.textContent.trim() : '';
        if (!label) {{
            var lbl = document.querySelector('label[for="' + els[i].id + '"]');
            if (lbl) label = lbl.textContent.trim();
        }}
        if (label.toLowerCase().indexOf('{answer_clean.lower()[:40]}') !== -1) {{
            els[i].click();
            return 'clicked radio: ' + label;
        }}
    }}
    // Try links and buttons
    var links = document.querySelectorAll('a, button, [onclick], [role="button"]');
    for (var i = 0; i < links.length; i++) {{
        var text = (links[i].textContent||'').trim();
        if (text.toLowerCase().indexOf('{answer_clean.lower()[:40]}') !== -1 && links[i].offsetParent !== null) {{
            links[i].click();
            return 'clicked link: ' + text;
        }}
    }}
    return 'no match for: {answer_clean[:40]}';
}})()`);
cliLog('answered');
"""
                await _run_ego(click_script, timeout=15)

            await asyncio.sleep(1)

            # Click Next/Submit via JS
            next_script = """
const task = await useOrCreateTaskSpace('survey answering');
await js(String.raw`(function(){
    var btns = document.querySelectorAll('button, input[type="submit"], a.btn');
    for (var i = 0; i < btns.length; i++) {
        var text = (btns[i].textContent || btns[i].value || '').trim().toUpperCase();
        if (text === 'NEXT' || text === 'SUBMIT' || text === 'CONTINUE' || text === '>>' || text === 'NEXT >') {
            btns[i].click();
            return 'clicked: ' + text;
        }
    }
    return 'no next button';
})()`);
cliLog('nexted');
await new Promise(r => setTimeout(r, 3000));
"""
            await _run_ego(next_script, timeout=15)

        return AgentResult(success=True, output=f"Answered {max_questions} questions (loop limit)")

    async def check_balance(self, dashboard_url: str) -> AgentResult:
        """Check account balance."""
        script = _build_snapshot_script(dashboard_url)
        output, rc = await _run_ego(script, timeout=60)
        return AgentResult(success=rc == 0, output=output)

    async def withdraw(self, redeem_url: str, method: str = "paypal") -> AgentResult:
        """Withdraw earnings."""
        script = _build_snapshot_script(redeem_url)
        script += "\ncliLog('RESULT: withdraw_navigate_complete');\n"
        output, rc = await _run_ego(script, timeout=60)
        return AgentResult(success=rc == 0, output=output, error="" if rc == 0 else output[:200])
