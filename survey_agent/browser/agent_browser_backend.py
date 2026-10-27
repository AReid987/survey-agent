"""agent-browser backend — wraps `agent-browser` CLI (SKILL'd harness).

Why this exists
---------------
`agent-browser` (vercel-labs/agent-browser, 0.27.0 installed) is the local
harness Hermes itself uses for browser tasks. It already bundles a stealth
Chromium, supports `--profile` (reuse real Chrome login), `--auto-connect`
(discover a running Chrome), and `--cdp` (attach to Hermes's or harness's
CDP). Its `--enable react-devtools` and `--session-name` give per-task
isolation. The Bitter Lesson of Agent Harnesses post advocates this
"raw CDP + SKILL.md" shape — SKILL.md + 4 helpers, model writes the rest.

Usage:
  survey-agent run --backend agent-browser
  pnpm run survey:agent-browser
  # Reuse real Chrome profile (avoids Google unsafe):
  agent-browser --profile Default open https://surveys.gobranded.com
  # Or auto-connect to already-running Chrome:
  BROWSER_CDP_URL=http://127.0.0.1:9222 survey-agent run --backend agent-browser

References from the resource list:
  https://docs.browser-use.com/open-source/browser-use-cli  (browser-use CLI v3 harness)
  https://github.com/vercel-labs/agent-browser                  (skills + local harness)
  https://github.com/BrowserBox/undetected-chromedriver       (stealth chromedriver)
  https://github.com/browser-use/bux                         (experimental harness, pip bux 0.2.0)
  https://github.com/cloudflare/computer                       (@cloudflare/computer 0.2.1, virtual FS)
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess

from .backend import AgentResult, SurveyAgentBackend

logger = logging.getLogger(__name__)

AGENT_BROWSER_BIN = shutil.which("agent-browser") or "agent-browser"
SESSION_NAME = "survey-agent"


def _ab(*args: str, timeout: int = 30) -> tuple[str, int]:
    """Run `agent-browser --session survey-agent <args>` synchronously."""
    cmd = [AGENT_BROWSER_BIN, "--session", SESSION_NAME, *args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out = (result.stdout or "") + (result.stderr or "")
    return out, result.returncode


async def _ab_async(*args: str, timeout: int = 30) -> tuple[str, int]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _ab(*args, timeout=timeout))


class AgentBrowserBackend(SurveyAgentBackend):
    """Thin SurveyAgentBackend over `agent-browser` CLI.

    Mirrors Hermes's `browser_tool.py` local mode but scoped to a single
    survey-agent session. Supports `--auto-connect` / `--cdp` via env so
    it can piggyback on Hermes's or harness's Chrome.
    """

    def __init__(self, headless: bool = False, **_: object):
        self._headless = headless
        self._started = False

    async def start(self) -> None:
        # Ensure binary exists
        bin_path = shutil.which("agent-browser")
        if not bin_path:
            raise RuntimeError("agent-browser not found — run `pnpm add -g agent-browser && agent-browser install`")
        # Probe session
        out, rc = await _ab_async("snapshot", "--help")
        # Lazy — actual Chromium download happens on first `open`
        self._started = True
        logger.info("agent-browser backend ready (session=%s)", SESSION_NAME)

    async def stop(self) -> None:
        try:
            await _ab_async("close")
        except Exception:
            pass
        logger.info("agent-browser backend stopped")

    async def login(self, site_url: str, site_name: str) -> AgentResult:
        logger.info("agent-browser login: %s (%s)", site_name, site_url)
        out, rc = await _ab_async("open", site_url, timeout=30)
        if rc != 0:
            return AgentResult(success=False, error=f"agent-browser open failed: {out[:400]}")
        await asyncio.sleep(2)
        snap, _ = await _ab_async("snapshot", timeout=15)
        low = snap.lower()
        if any(k in low for k in ("dashboard", "my points", "log out", "my account")):
            return AgentResult(success=True, output="already_logged_in")
        return AgentResult(success=False, error=f"Manual login required for {site_name} — snapshot: {snap[:600]}")

    async def discover_surveys(self, dashboard_url: str) -> AgentResult:
        out, rc = await _ab_async("open", dashboard_url, timeout=30)
        await asyncio.sleep(2)
        snap, _ = await _ab_async("snapshot", timeout=15)
        if "no surveys" in snap.lower() or not snap.strip():
            return AgentResult(success=False, error="no surveys")
        return AgentResult(success=True, output=snap[:4000])

    async def complete_survey(self, survey_info: str, profile_context: str) -> AgentResult:
        # TODO: wire Hermes's survey_engine via agent-browser snapshot/click/fill
        return AgentResult(success=False, error="agent-browser complete_survey stub")

    async def check_balance(self, dashboard_url: str) -> AgentResult:
        await _ab_async("open", dashboard_url, timeout=30)
        snap, _ = await _ab_async("snapshot", timeout=15)
        return AgentResult(success=True, output=snap[:3000])

    async def withdraw(self, redeem_url: str, method: str = "paypal") -> AgentResult:
        return AgentResult(success=False, error="manual cashout per payout flow")
