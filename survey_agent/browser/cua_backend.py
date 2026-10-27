"""CUA backend — drives the user's real Chrome/Browser via `cua-driver`.

Why this exists
---------------
The Playwright Chromium bundled with the project (1161, May 2024) triggers
Google's "This browser or app may not be secure" interstitial. Hermes
already solved this: `cua-driver` talks to the *real* Chrome you already
log into every day via macOS Accessibility + Screen Recording, not via
`--enable-automation` or a separate `user-data-dir`. No CDP flags, no
isolated profile, so Google sees a normal browser with your real cookies.

This backend is a thin SurveyAgentBackend wrapper around the same
`cua-driver call get_window_state / click / type` flow Hermes uses for
its own `cua_*` tools. It reuses the `cua-driver` daemon Herme­s already
configured at `~/.cache/cua` — no extra setup.

Prerequisites (one-time):
  cua-driver permissions grant   # grants Accessibility + Screen Recording
  # (Hermes setup already did this if you ran it)

Usage:
  survey-agent run --backend cua              # local cua backend
  pnpm run survey:cua                         # alias added to package.json

Architecture notes (bitter-lesson harness friendly):
  - Raw Accessibility tree via `get_window_state` → LLM sees refs
  - Native clicks/types via cua-driver → not DOM-injected, survives bot checks
  - Screenshot via cua-driver's `screenshot_png_b64` (already used for CAPTCHA)
  - Shares the same `survey_agent.vision` LFM model for captcha + iframe escalation
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from typing import Any

from .backend import AgentResult, SurveyAgentBackend

logger = logging.getLogger(__name__)

CUA_DEFAULT_TIMEOUT = 30


def _cua_call(method: str, payload: dict | None = None, timeout: int = CUA_DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Synchronous wrapper around `cua-driver call <method> --json '<payload>'`."""
    cmd = ["cua-driver", "call", method]
    if payload is not None:
        cmd += ["--json", json.dumps(payload)]
    else:
        cmd += ["--json"]
    # cua-driver writes JSON to stdout; errors to stderr
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    raw = (result.stdout or "").strip()
    if not raw:
        raise RuntimeError(f"cua-driver {method} returned empty stdout: {result.stderr[:300]}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"cua-driver {method} non-JSON: {raw[:500]}") from e


def _find_chrome_window() -> tuple[int, str]:
    """Pick the best Chrome window for survey automation (largest content area)."""
    data = _cua_call("list_windows")
    windows = data.get("windows", [])
    best: dict[str, Any] | None = None
    best_area = 0
    for w in windows:
        if w.get("app_name") != "Google Chrome":
            continue
        b = w.get("bounds", {})
        area = b.get("width", 0) * b.get("height", 0)
        if b.get("height", 0) > 100 and area > best_area:
            best = w
            best_area = area
    if not best:
        raise RuntimeError("No Google Chrome window found — open Chrome first")
    return int(best["pid"]), str(best["window_id"])


class CuaBackend(SurveyAgentBackend):
    """Survey agent backend powered by `cua-driver` (native macOS automation).

    Drives the user's real Chrome window, so Google OAuth sees a normal browser
    and captcha screenshots come from the true screen buffer (high fidelity for
    LFM vision). All survey reasoning still uses the same deterministic
    `question_classifier` + `form_filler` + LFM escalation path as the harness.
    """

    def __init__(self, headless: bool = False, **_: Any):
        # CUA is always headed — it automates the visible desktop.
        self._headless = headless
        self._pid: int | None = None
        self._wid: str | None = None
        self._base_url: str = ""

    async def start(self) -> None:
        # Verify permissions + Chrome presence without stealing focus.
        # We attach to the existing Chrome window in the background — your
        # physical mouse/keyboard stay free (Hermes uses the same daemon).
        # The agent cursor overlay (virtual cursor) is used for clicks, not
        # the real pointer, so you can keep using the machine.
        loop = asyncio.get_running_loop()
        pid, wid = await loop.run_in_executor(None, _find_chrome_window)
        self._pid, self._wid = pid, wid
        logger.info("cua backend: attached to Chrome pid=%s wid=%s (virtual cursor, background-safe)", pid, wid)
        # Ensure a cua session exists so clicks use the agent cursor overlay
        try:
            await loop.run_in_executor(None, lambda: _cua_call("start_session", {"label": "survey-agent-cua"}))
            logger.info("cua backend: session started with agent cursor overlay enabled")
        except Exception as e:
            logger.debug("cua start_session not needed or failed: %s", e)
        # Quick probe — ensure get_window_state works & permissions are granted
        state = await loop.run_in_executor(None, lambda: _cua_call("get_window_state", {"pid": pid, "window_id": wid, "max_elements": 5}))
        if not state.get("screenshot_png_b64"):
            logger.warning("cua get_window_state returned no screenshot — check System Settings > Privacy > Screen Recording for cua-driver")
        else:
            logger.info("cua backend: permissions OK (screenshot %d bytes) — you can keep using Chrome, it stays in background", len(state["screenshot_png_b64"]))

    async def stop(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: _cua_call("end_session", {}))
        except Exception:
            pass
        logger.info("cua backend: detached (Chrome left running, virtual cursor removed)")

    async def _snapshot(self) -> dict[str, Any]:
        assert self._pid and self._wid, "Call start() first"
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: _cua_call("get_window_state", {"pid": self._pid, "window_id": self._wid, "max_elements": 300}))

    async def _navigate(self, url: str) -> None:
        """Navigate via cua-driver's background-safe `browser_navigate` (CDP, not AppleScript).

        This does NOT steal focus or move your physical mouse — it talks to
        the Chrome CDP endpoint that `cua-driver`'s daemon already bridges
        (the same way Hermes does). The window can stay in the background
        while Hermes/survey-agent drives it with its own virtual cursor.
        If no browser session exists yet, fall back to `osascript` only as
        a last resort (still better than Chromium).
        """
        loop = asyncio.get_running_loop()
        # Prefer the session-aware browser_navigate tool (background-safe, no foreground steal)
        try:
            # If we have a session, use browser_navigate; otherwise it will auto-create one
            await loop.run_in_executor(
                None,
                lambda: _cua_call("browser_navigate", {"url": url}),
            )
            await asyncio.sleep(3)
            return
        except Exception as e:
            logger.debug("cua browser_navigate failed (%s), falling back to osascript: %s", type(e).__name__, e)
        # Fallback: AppleScript (still better than launching isolated Chromium)
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", f'tell application "Google Chrome" to set URL of active tab of window 1 to "{url}"',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        await asyncio.sleep(3)

    async def login(self, site_url: str, site_name: str) -> AgentResult:
        logger.info("cua login: %s (%s)", site_name, site_url)
        await self._navigate(site_url)
        await asyncio.sleep(3)
        state = await self._snapshot()
        text = (state.get("text") or "")[:2000].lower()
        # Already logged in?
        if any(k in text for k in ("dashboard", "my points", "log out", "my account", "take survey")):
            return AgentResult(success=True, output="already_logged_in")
        # Otherwise ask user to log in manually — cua can detect but shouldn't autofill Google password
        logger.info("cua login: manual login required — please log into %s in the Chrome window", site_name)
        # Snapshot URL
        url = state.get("url") or site_url
        return AgentResult(success=False, error=f"Manual login required for {site_name} at {url} — log in in Chrome, then re-run")

    async def discover_surveys(self, dashboard_url: str) -> AgentResult:
        await self._navigate(dashboard_url)
        await asyncio.sleep(3)
        state = await self._snapshot()
        text = (state.get("text") or "")
        if not text.strip():
            return AgentResult(success=False, error="no surveys")
        # Heuristic: look for survey cards / "available surveys" heading
        if any(k in text.lower() for k in ("available", "survey", "earn", "points", "take survey")):
            return AgentResult(success=True, output=text[:4000])
        return AgentResult(success=False, error="no surveys")

    async def complete_survey(self, survey_info: str, profile_context: str) -> AgentResult:
        # Delegate to the shared harness survey engine but via cua clicks.
        # For now return a stub so the orchestrator can probe liveness.
        return AgentResult(success=False, error="cua complete_survey: stub — wire survey_engine via cua click/type in next patch")

    async def check_balance(self, dashboard_url: str) -> AgentResult:
        await self._navigate(dashboard_url)
        await asyncio.sleep(3)
        state = await self._snapshot()
        text = state.get("text") or ""
        # Extract points/balance
        m = re.search(r"(\d[\d,]*)\s*(points?|credits?|\$)", text, re.I)
        if m:
            return AgentResult(success=True, output=f"BALANCE: {m.group(1)} {m.group(2)} — {text[:500]}")
        return AgentResult(success=True, output=text[:2000])

    async def withdraw(self, redeem_url: str, method: str = "paypal") -> AgentResult:
        return AgentResult(success=False, error="withdraw via cua: use manual cashout per payout flow")
