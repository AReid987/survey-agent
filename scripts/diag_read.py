#!/usr/bin/env python3
"""Diagnose page reading: what does browser-harness actually see?"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main():
    from survey_agent.browser.harness_backend import CDP_PORT, _run_harness

    cdp = f"http://127.0.0.1:{CDP_PORT}"

    # Navigate to dashboard
    await _run_harness(
        'goto_url("https://surveys.gobranded.com/users/dashboard")\nimport time\nwait_for_load()\ntime.sleep(3)',
        timeout=30,
        cdp_url=cdp,
    )

    # Dismiss cookies
    await _run_harness(
        'js("document.querySelector(\\".cky-btn-accept\\")?.click()")\nimport time\ntime.sleep(1)',
        timeout=10,
        cdp_url=cdp,
    )

    # Read page text via temp file
    text_js = 'document.body ? document.body.innerText.substring(0, 3000) : ""'
    with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False) as f:
        f.write(text_js)
        text_js_path = f.name

    out, rc = await _run_harness(
        f'with open("{text_js_path}") as f:\n    _js = f.read()\ntext = js(_js)\nprint("TEXT:", text[:2000])\nprint("RESULT: done")',
        timeout=15,
        cdp_url=cdp,
    )
    print(f"rc={rc}")
    print(out[:2000])
    Path(text_js_path).unlink(missing_ok=True)


asyncio.run(main())
