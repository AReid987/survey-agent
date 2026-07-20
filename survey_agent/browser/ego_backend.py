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
        """Complete a survey using ego-lite snapshot-based interaction.

        Uses snapshotText() for page reading and click('@ref') for interaction.
        Logs extensively so we can diagnose snapshot format issues.
        Fails fast (3 no-option iterations) so orchestrator can try next survey.
        """
        import re
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

        dashboard_url = "https://surveys.gobranded.com/users/dashboard"

        # Step 1: Find survey URL on dashboard
        find_survey_script = f"""
const task = await useOrCreateTaskSpace('survey answering');
await openOrReuseTab('{dashboard_url}', {{ wait: true, timeout: 30 }});
await waitForLoad();
await new Promise(r => setTimeout(r, 10000));
// Try /surveys/pre/ first (real survey links)
var surveyUrl = await js(String.raw`(function(){{
    var l = document.querySelector('a[href*="/surveys/pre/"]');
    return l ? l.href : '';
}})()`);
if (!surveyUrl) {{
    // Fallback: router/go
    surveyUrl = await js(String.raw`(function(){{
        var l = document.querySelector('a[href*="/router/go"]');
        return l ? l.href : '';
    }})()`);
    cliLog('FALLBACK: router/go -> ' + surveyUrl);
}}
// Check IN PROGRESS
var body = await js('(function(){{ return document.body ? document.body.innerText : ""; }})()');
var inProgress = body.indexOf('IN PROGRESS') !== -1;
cliLog('IN_PROGRESS: ' + inProgress);
cliLog('SURVEY_URL: ' + surveyUrl);
"""
        output, rc = await _run_ego(find_survey_script, timeout=45)
        if rc != 0:
            logger.warning(f"Dashboard failed (rc={rc}): {output[:200]}")
            return AgentResult(success=False, error=f"Dashboard failed: {output[:200]}")

        survey_url = ""
        is_in_progress = False
        for line in output.strip().split("\n"):
            if line.startswith("SURVEY_URL: "):
                survey_url = line[12:].strip()
            if line.startswith("IN_PROGRESS: true"):
                is_in_progress = True
            if line.startswith("FALLBACK:"):
                logger.info(line)

        if not survey_url or survey_url == "none":
            if is_in_progress:
                return AgentResult(success=True, output="IN PROGRESS blocking — no /surveys/pre/ links available")
            return AgentResult(success=True, output="No surveys available")

        logger.info(f"Survey URL: {survey_url[:80]}")

        # Step 2: Navigate to survey pre-page
        nav_output, nav_rc = await _run_ego(f"""
const task = await useOrCreateTaskSpace('survey answering');
await gotoAndWait('{survey_url}', {{ timeout: 30, settle: 5 }});
await new Promise(r => setTimeout(r, 3000));
var url = (await currentTab()).url;
cliLog('NAV_URL: ' + url);
""", timeout=45)
        for line in (nav_output or "").strip().split("\n"):
            if line.startswith("NAV_URL:"):
                logger.info(f"Nav: {line}")

        # Step 3: Click TAKE SURVEY using snapshot ref (handles navigation better)
        take_output, take_rc = await _run_ego("""
const task = await useOrCreateTaskSpace('survey answering');
// Get snapshot to find TAKE SURVEY ref
var snap = await snapshotText();
var takeRef = '';
var lines = snap.split('\\n');
for (var i = 0; i < lines.length; i++) {
    if (lines[i].indexOf('TAKE SURVEY') !== -1) {
        // Look backwards for ref
        for (var j = i; j >= Math.max(0, i - 3); j--) {
            var m = lines[j].match(/ref=(\d+)/);
            if (m) { takeRef = m[1]; break; }
        }
        if (takeRef) break;
    }
}
cliLog('TAKE_REF: ' + takeRef);
if (takeRef) {
    try {
        await click('@' + takeRef, { label: 'TAKE SURVEY' });
        cliLog('TAKE_RESULT: clicked @' + takeRef);
    } catch(e) {
        cliLog('TAKE_RESULT: click failed: ' + e.message);
        // Fallback: js click
        await js(String.raw`(function(){
            var els = document.querySelectorAll('a, button, input[type="submit"]');
            for (var i = 0; i < els.length; i++) {
                var text = (els[i].textContent||els[i].value||'').trim().toUpperCase();
                if (text === 'TAKE SURVEY') { els[i].click(); return 'clicked'; }
            }
            return 'not found';
        })()`);
        cliLog('TAKE_RESULT: fallback js click');
    }
} else {
    cliLog('TAKE_RESULT: no ref found');
}
await new Promise(r => setTimeout(r, 10000));
var url = (await currentTab()).url;
cliLog('AFTER_TAKE_URL: ' + url);
""", timeout=30)

        after_take_url = ""
        for line in (take_output or "").strip().split("\n"):
            if line.startswith("AFTER_TAKE_URL:"):
                after_take_url = line[15:].strip()
            if line.startswith("TAKE_RESULT:"):
                logger.info(f"TAKE: {line}")

        if "/users/dashboard" in after_take_url:
            logger.info("TAKE SURVEY redirected to dashboard — survey full or blocked")
            return AgentResult(success=True, output="Survey full (redirected to dashboard)")

        # Step 4: Click BEGIN SURVEY / START >>
        begin_output, begin_rc = await _run_ego("""
const task = await useOrCreateTaskSpace('survey answering');
var beginResult = await js(String.raw`(function(){
    var els = document.querySelectorAll('a, button, input[type="submit"]');
    for (var i = 0; i < els.length; i++) {
        var text = (els[i].textContent||els[i].value||'').trim().toUpperCase();
        if (text === 'BEGIN SURVEY' || text === 'START SURVEY' || text === 'START >>') {
            els[i].click(); return 'clicked ' + text;
        }
    }
    return 'not found';
})()`);
cliLog('BEGIN_RESULT: ' + beginResult);
await new Promise(r => setTimeout(r, 15000));
var url = (await currentTab()).url;
cliLog('AFTER_BEGIN_URL: ' + url);
""", timeout=30)

        for line in (begin_output or "").strip().split("\n"):
            if line.startswith("BEGIN_RESULT:") or line.startswith("AFTER_BEGIN_URL:"):
                logger.info(line)

        # Step 5: Handle consent and Start buttons
        consent_output, _ = await _run_ego("""
const task = await useOrCreateTaskSpace('survey answering');
// Click consent/accept/start buttons
var consentResult = await js(String.raw`(function(){
    var cb = document.getElementById('confirmationCheckbox');
    if (cb && !cb.checked) { cb.click(); }
    var cbs = document.querySelectorAll('input[type="checkbox"]');
    for (var i = 0; i < cbs.length; i++) {
        if (!cbs[i].checked && cbs[i].offsetParent !== null) { cbs[i].click(); break; }
    }
    var btns = document.querySelectorAll('a, button, input[type="submit"]');
    for (var i = 0; i < btns.length; i++) {
        var text = (btns[i].textContent||btns[i].value||'').trim().toUpperCase();
        if (text === 'ACCEPT' || text === 'AGREE' || text === 'I AGREE' || text === 'I CONSENT' || text === 'CONTINUE' || text === 'START') {
            btns[i].click(); return 'clicked ' + text;
        }
    }
    return 'no_consent';
})()`);
cliLog('CONSENT: ' + consentResult);
await new Promise(r => setTimeout(r, 8000));
""", timeout=20)

        for line in (consent_output or "").strip().split("\n"):
            if line.startswith("CONSENT:"):
                logger.info(line)

        # Step 6: Answer questions using snapshotText() + click('@ref')
        max_questions = 50
        no_option_count = 0  # Fail fast if no options found 3 times in a row
        last_snap = ""

        for i in range(max_questions):
            # Get page snapshot
            snap_output, snap_rc = await _run_ego("""
const task = await useOrCreateTaskSpace('survey answering');
const snap = await snapshotText();
cliLog('SNAP: ' + snap);
""", timeout=15)

            if snap_rc != 0:
                no_option_count += 1
                if no_option_count >= 3:
                    logger.warning(f"No snapshot after {i} iterations, failing fast")
                    return AgentResult(success=False, error="Failed to get page snapshot after 3 attempts")
                await asyncio.sleep(2)
                continue

            snapshot = ""
            for line in (snap_output or "").strip().split("\n"):
                if line.startswith("SNAP: "):
                    snapshot = line[6:]

            if not snapshot:
                no_option_count += 1
                if no_option_count >= 3:
                    logger.warning(f"Empty snapshot after {i} iterations, failing fast")
                    return AgentResult(success=False, error="Empty page snapshot after 3 attempts")
                await asyncio.sleep(2)
                continue

            # Log first snapshot for debugging
            if i == 0:
                logger.info(f"First snapshot (first 500 chars): {snapshot[:500]}")

            # Check for completion/disqualification/dashboard
            sl = snapshot.lower()
            if any(kw in sl for kw in ["thank you", "survey complete", "successfully completed"]):
                return AgentResult(success=True, output=f"Survey completed after {i} questions")
            if any(kw in sl for kw in ["not a match", "disqualified", "screened out"]):
                return AgentResult(success=False, error="Disqualified from survey")
            if any(kw in sl for kw in ["my top surveys", "daily poll", "branded elite", "500 points until payout"]):
                return AgentResult(success=True, output=f"Back on dashboard after {i} questions")

            # Parse snapshot for question text and option refs
            q_text = ""
            opts = []
            snap_lines = snapshot.split("\n")
            for j, raw in enumerate(snap_lines):
                sline = raw.strip()
                if any(skip in sline.lower() for skip in [
                    "button-back", "button-next", "error message",
                    "select language", "translate", "cookie",
                    "technical problem", "click here",
                ]):
                    continue
                # Find question text
                if not sline.startswith(("container", "button", "text", "form", "navigation", "anchor", "root", "group", "heading")):
                    if len(sline) > 10 and not q_text:
                        if any(kw in sline.lower() for kw in ["?", "select", "how", "what", "which", "are you", "have you", "please"]):
                            q_text = sline[:300]
                # Find options: text "X" followed by container with ref
                if sline.startswith('text "'):
                    txt = sline[6:].rstrip('"')
                    if j + 1 < len(snap_lines):
                        nxt = snap_lines[j + 1].strip()
                        rm = re.search(r'ref=(\d+)', nxt)
                        if rm and txt:
                            opts.append({"text": txt, "ref": rm.group(1)})

            # Find next button ref
            next_ref = ""
            nm = re.search(r'button \[ref=(\d+), loc=css:input\[name="button-next"\]\]', snapshot)
            if nm:
                next_ref = nm.group(1)

            # Find START/submit button ref
            start_ref = ""
            sm = re.search(r'input \[ref=(\d+), loc=css:input\[name="XX_(?:NXT|START)"\]\]', snapshot)
            if sm:
                start_ref = sm.group(1)

            # No options found
            if not opts:
                no_option_count += 1
                if no_option_count >= 3:
                    # Log the snapshot for debugging
                    logger.warning(f"No options found after {i} iterations. Snapshot (first 1000 chars):")
                    for snap_line in snapshot[:1000].split("\n"):
                        logger.warning(f"  SNAP: {snap_line}")
                    return AgentResult(success=False, error=f"No survey options found after {i} iterations (snapshot format mismatch)")

                if start_ref:
                    await _run_ego(f"""
const task = await useOrCreateTaskSpace('survey answering');
await click('@{start_ref}', {{ label: 'click START' }});
""", timeout=10)
                    await asyncio.sleep(5)
                    continue

                if snapshot == last_snap:
                    await asyncio.sleep(2)
                else:
                    no_option_count = 0  # Reset if snapshot changed
                last_snap = snapshot
                await asyncio.sleep(2)
                continue

            # Found options — reset no-option counter
            no_option_count = 0

            # Use LLM to pick the best answer
            prompt = f"""Answer this survey question as the user described below.

USER PROFILE:
{profile_context}

QUESTION: {q_text}

OPTIONS (each has a ref number):
{json.dumps(opts[:20], indent=2)}

Reply with ONLY the ref number of the best option (e.g. "5").
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

            # Find the matching option ref
            sel_ref = ""
            if answer.startswith("TEXT:"):
                tb = re.search(r'spinbutton \[ref=(\d+)', snapshot)
                if tb:
                    tval = answer[5:].strip().replace("'", "\\'")
                    tref = tb.group(1)
                    await _run_ego(f"""
const task = await useOrCreateTaskSpace('survey answering');
await fillInput('@{tref}', '{tval}');
""", timeout=10)
                    sel_ref = tref
            else:
                ref_match = re.search(r'\b(\d+)\b', answer)
                if ref_match:
                    candidate = ref_match.group(1)
                    valid_refs = [o["ref"] for o in opts]
                    if candidate in valid_refs:
                        sel_ref = candidate
                    else:
                        answer_lower = answer.lower().strip()
                        for opt in opts:
                            if answer_lower in opt["text"].lower() or opt["text"].lower() in answer_lower:
                                sel_ref = opt["ref"]
                                break

            if not sel_ref:
                logger.warning(f"Could not match answer '{answer[:50]}' to options")
                await asyncio.sleep(1)
                continue

            logger.info(f"Q{i+1}: {q_text[:50]} -> ref={sel_ref} ({answer[:30]})")

            # Click the selected option
            await _run_ego(f"""
const task = await useOrCreateTaskSpace('survey answering');
await click('@{sel_ref}', {{ label: 'select answer' }});
""", timeout=10)
            await asyncio.sleep(0.5)

            # Click next button
            if next_ref:
                await _run_ego(f"""
const task = await useOrCreateTaskSpace('survey answering');
await click('@{next_ref}', {{ label: 'click next' }});
""", timeout=10)
                await asyncio.sleep(5)

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
