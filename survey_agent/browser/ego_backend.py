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

        Navigates to dashboard, clicks a survey, then answers questions
        using snapshotText() for page reading and click('@N') for interaction.
        """
        from ..llm_provider import get_llm, PROVIDERS  # noqa: PLC0415
        from ..config import load_config  # noqa: PLC0415
        from .survey_engine_ego import _find_interactive_elements, _find_survey_links  # noqa: PLC0415

        cfg = load_config()
        llm = get_llm(
            provider=cfg.llm_provider,
            model=cfg.llm_model or PROVIDERS.get(cfg.llm_provider, {}).get("default_model", ""),
            api_key=cfg.llm_api_key,
            base_url=cfg.llm_base_url or None,
            temperature=0.1,
        )

        # Step 1: Navigate to dashboard and find surveys
        dashboard_url = "https://surveys.gobranded.com/users/dashboard"
        nav_script = f"""
const task = await useOrCreateTaskSpace('survey answering');
await openOrReuseTab('{dashboard_url}', {{ wait: true, timeout: 20 }});
await waitForLoad();
await new Promise(r => setTimeout(r, 5000));
const pageSnap = await snapshotText();
const pageUrl = (await currentTab()).url;
cliLog(JSON.stringify({{ url: pageUrl, snapshot: pageSnap }}));
"""
        output, rc = await _run_ego(nav_script, timeout=45)
        if rc != 0:
            return AgentResult(success=False, error=f"Dashboard navigation failed: {output[:200]}")

        try:
            data = json.loads(output.strip())
            snapshot = data.get("snapshot", "")
        except json.JSONDecodeError:
            return AgentResult(success=False, error=f"Failed to parse dashboard: {output[:200]}")

        # Find survey links
        elements = _find_interactive_elements(snapshot)
        surveys = _find_survey_links(elements)

        if not surveys:
            return AgentResult(success=True, output="No surveys available on dashboard")

        # Click the first survey
        survey_ref = surveys[0]["ref"]
        logger.info(f"Clicking survey ref={survey_ref}: {surveys[0].get('url', '')[:80]}")

        click_script = f"""
const task = await useOrCreateTaskSpace('survey answering');
await click('@{survey_ref}', {{ label: 'click survey' }});
await new Promise(r => setTimeout(r, 5000));
const pageSnap = await snapshotText();
const pageUrl = (await currentTab()).url;
cliLog(JSON.stringify({{ url: pageUrl, snapshot: pageSnap }}));
"""
        output, rc = await _run_ego(click_script, timeout=30)
        if rc != 0:
            return AgentResult(success=False, error=f"Survey click failed: {output[:200]}")

        try:
            data = json.loads(output.strip())
            snapshot = data.get("snapshot", "")
            url = data.get("url", "")
        except json.JSONDecodeError:
            return AgentResult(success=False, error=f"Failed to parse survey page: {output[:200]}")

        # Step 2: Answer questions in a loop
        max_questions = 50
        for i in range(max_questions):
            # Check for completion/disqualification
            snap_lower = snapshot.lower()
            if any(kw in snap_lower for kw in ["thank you", "survey complete", "successfully completed"]):
                return AgentResult(success=True, output=f"Survey completed after {i} questions")
            if any(kw in snap_lower for kw in ["not a match", "disqualified", "screened out"]):
                return AgentResult(success=False, error="Disqualified from survey")

            # Parse snapshot for questions and options
            elements = _find_interactive_elements(snapshot)

            # Filter for answer options (radio/checkbox/select)
            options = []
            for el in elements:
                text = el.get("text", "").strip()
                ref = el.get("ref", "")
                raw = el.get("raw", "").lower()
                # Look for radio/checkbox/select elements
                if ("radio" in raw or "checkbox" in raw or "option" in raw or
                    "button" in raw or "select" in raw):
                    if text and len(text) < 100 and len(text) > 1:
                        # Skip navigation elements
                        if any(skip in text.lower() for skip in ["dashboard", "profile", "logout", "menu", "skip"]):
                            continue
                        options.append({"ref": ref, "text": text})

            if not options:
                # No options found — might be between pages or need scrolling
                scroll_script = """
const task = await useOrCreateTaskSpace('survey answering');
await scrollBy(500);
await new Promise(r => setTimeout(r, 1000));
const pageSnap = await snapshotText();
cliLog(JSON.stringify({snapshot: pageSnap}));
"""
                output, rc = await _run_ego(scroll_script, timeout=15)
                if rc == 0:
                    try:
                        data = json.loads(output.strip())
                        snapshot = data.get("snapshot", "")
                    except json.JSONDecodeError:
                        pass
                await asyncio.sleep(2)
                continue

            # Extract question text from non-interactive lines
            lines = snapshot.split("\n")
            question_text = ""
            for line in lines:
                if re.search(r'\[ref=\d+', line):
                    continue
                text = line.strip()
                if len(text) > 15 and ('?' in text or 'select' in text.lower() or 'how' in text.lower()):
                    question_text = text[:300]
                    break
                if len(text) > 20 and not question_text:
                    question_text = text[:300]

            # Use LLM to pick the best answer
            prompt = f"""Answer this survey question as the user described below.

USER PROFILE:
{profile_context}

QUESTION: {question_text}

OPTIONS (each has a ref number):
{json.dumps(options[:20], indent=2)}

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

            logger.info(f"Survey Q{i+1}: {question_text[:50]} → {answer[:50]}")

            # Apply the answer
            if answer.startswith("TEXT:"):
                text_val = answer[5:].strip()
                fill_script = f"""
const task = await useOrCreateTaskSpace('survey answering');
const inputs = document.querySelectorAll('input[type="text"], textarea');
for (const input of inputs) {{
    if (input.offsetParent !== null) {{
        await fillInput(input, '{text_val.replace("'", "\\'")}');
        break;
    }}
}}
cliLog('filled');
"""
                await _run_ego(fill_script, timeout=15)
            else:
                ref_num = re.search(r'\d+', answer)
                if ref_num:
                    click_script = f"""
const task = await useOrCreateTaskSpace('survey answering');
await click('@{ref_num.group()}', {{ label: 'select answer' }});
cliLog('clicked @{ref_num.group()}');
"""
                    await _run_ego(click_script, timeout=15)

            await asyncio.sleep(1)

            # Click Next/Submit
            submit_script = """
const task = await useOrCreateTaskSpace('survey answering');
const pageSnap = await snapshotText();
const lines = pageSnap.split(String.fromCharCode(10));
for (const line of lines) {
    const refMatch = line.match(/\\[ref=(\\d+)/);
    if (refMatch) {
        const text = line.toLowerCase();
        if (text.includes('next') || text.includes('submit') || text.includes('continue') || text.includes('>>')) {
            await click('@' + refMatch[1], { label: 'click next/submit' });
            cliLog('clicked next: @' + refMatch[1]);
            break;
        }
    }
}
await new Promise(r => setTimeout(r, 3000));
"""
            await _run_ego(submit_script, timeout=15)

            # Get updated snapshot for next iteration
            snap_script = """
const task = await useOrCreateTaskSpace('survey answering');
const pageSnap = await snapshotText();
const pageUrl = (await currentTab()).url;
cliLog(JSON.stringify({ url: pageUrl, snapshot: pageSnap }));
"""
            output, rc = await _run_ego(snap_script, timeout=30)
            if rc == 0:
                try:
                    data = json.loads(output.strip())
                    snapshot = data.get("snapshot", "")
                except json.JSONDecodeError:
                    pass

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
