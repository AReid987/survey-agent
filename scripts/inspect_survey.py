#!/usr/bin/env python3
"""Inspect what's on a survey page after navigating to it."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main():
    from survey_agent.browser.harness_backend import CDP_PORT, _run_harness

    cdp_url = f"http://127.0.0.1:{CDP_PORT}"

    # Navigate to dashboard (loads session cookies)
    out, _ = await _run_harness(
        'import time\ngoto_url("https://surveys.gobranded.com/users/dashboard")\nwait_for_load()\ntime.sleep(3)\nurl = current_tab()["url"]\nprint("URL:", url)\nprint("RESULT: done")',
        timeout=30,
        cdp_url=cdp_url,
    )
    print("Dashboard:", out.strip()[:200])

    # Navigate to a survey URL
    out, _ = await _run_harness(
        """import time
goto_url("https://surveys.gobranded.com/surveys/pre/120045973?key=23e7b2f7&from=dashboard.tile")
wait_for_load()
time.sleep(8)
url = current_tab()["url"]
text = js("document.body ? document.body.innerText.substring(0, 3000) : ''")
elements = js('(() => { const els = []; document.querySelectorAll("input, select, textarea, button, a, [role=button], [role=radio]").forEach(el => { if (el.offsetParent === null && el.type !== "radio" && el.type !== "checkbox") return; const tag = el.tagName.toLowerCase(); let text = (el.textContent || el.value || "").trim().substring(0, 80); const type = el.type || el.getAttribute("role") || ""; const name = el.name || ""; const id = el.id || ""; els.push({tag, type, name, id, text}); }); return JSON.stringify(els.slice(0, 30)); })()')
print("URL:", url)
print("TEXT:", text[:2000])
print("ELEMENTS:", elements[:2000])
print("RESULT: done")""",
        timeout=45,
        cdp_url=cdp_url,
    )
    print("Survey:", out.strip()[:3000])


asyncio.run(main())
