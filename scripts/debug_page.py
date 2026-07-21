#!/usr/bin/env python3
"""Debug: check if survey page has iframes and read their content."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main():
    from survey_agent.browser.harness_backend import CDP_PORT, _run_harness

    cdp_url = f"http://127.0.0.1:{CDP_PORT}"

    # Check for iframes
    out, _ = await _run_harness(
        """import json
url = current_tab()["url"]
# Check for iframes
iframes = js('(() => { const frames = document.querySelectorAll("iframe"); return JSON.stringify(Array.from(frames).map((f,i) => ({idx:i, src:(f.src||"").substring(0,200), id:f.id||"", name:f.name||"", w:f.offsetWidth, h:f.offsetHeight}))); })()')
print("URL:", url)
print("IFRAMES:", iframes)
# Try to read main body text
text = js("document.body ? document.body.innerText.substring(0, 2000) : ''")
print("MAIN_TEXT:", text[:500])
# Try to read iframe content (same-origin only)
iframe_text = js('(() => { const frames = document.querySelectorAll("iframe"); const texts = []; for(const f of frames) { try { const doc = f.contentDocument || f.contentWindow.document; if(doc) texts.push(doc.body ? doc.body.innerText.substring(0,1000) : "empty"); } catch(e) { texts.push("cross-origin:" + e.message.substring(0,50)); } } return texts.join("|||"); })()')
print("IFRAME_TEXT:", iframe_text[:2000])
print("RESULT: done")
""",
        timeout=15,
        cdp_url=cdp_url,
    )
    print(out[:3000])


asyncio.run(main())
