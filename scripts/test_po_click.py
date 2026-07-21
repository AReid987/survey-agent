"""Click Prime Opinion survey and follow the redirect."""

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from survey_agent.browser.harness_backend import _run_harness

CDP_PORT = 19334
cdp = f"http://127.0.0.1:{CDP_PORT}"

CLICK_CARD_JS = """(() => {
  var cards = document.querySelectorAll('.list-item.survey-item');
  if (cards.length < 3) return 'no_cards';
  // Pick the $1.15 card (index 0 or 1)
  var card = cards[0];
  var text = (card.textContent || '').substring(0, 40);
  var rect = card.getBoundingClientRect();
  var cx = rect.left + rect.width / 2;
  var cy = rect.top + rect.height / 2;

  var opts = {bubbles: true, cancelable: true, clientX: cx, clientY: cy, pointerId: 1, pointerType: 'mouse'};
  card.dispatchEvent(new PointerEvent('pointerover', opts));
  card.dispatchEvent(new PointerEvent('pointerenter', opts));
  card.dispatchEvent(new PointerEvent('pointerdown', opts));
  card.dispatchEvent(new PointerEvent('pointerup', opts));
  card.dispatchEvent(new PointerEvent('click', opts));

  return 'clicked:' + text + ' at ' + cx.toFixed(0) + ',' + cy.toFixed(0);
})()"""


async def run_js(js_code):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, prefix="po_") as f:
        f.write(js_code)
        js_path = f.name
    out, rc = await _run_harness(
        f'with open("{js_path}") as f:\n    _js = f.read()\nresult = js(_js)\nprint("JS:", result)\nprint("END")',
        timeout=15,
        cdp_url=cdp,
    )
    Path(js_path).unlink(missing_ok=True)
    for line in out.split("\n"):
        if "JS:" in line:
            return line.split("JS:", 1)[1].strip()
    return ""


async def main():
    # Navigate to Prime Opinion surveys
    await _run_harness(
        'goto_url("https://app.primeopinion.com/surveys")\nimport time\nwait_for_load()\ntime.sleep(3)',
        timeout=30,
        cdp_url=cdp,
    )

    out, _ = await _run_harness('print("URL:", current_tab()["url"])', timeout=10, cdp_url=cdp)
    print(f"Start: {out.strip()[:200]}")

    # Click first card
    result = await run_js(CLICK_CARD_JS)
    print(f"Click: {result}")

    # Follow redirects
    prev_url = ""
    for i in range(10):
        time.sleep(2)
        out, _ = await _run_harness('print("URL:", current_tab()["url"])', timeout=10, cdp_url=cdp)
        url = out.strip().replace("URL:", "").strip()
        print(f"  [{i}] {url[:120]}")
        if "surveys" in url and "primeopinion" not in url:
            print("  -> Redirected to survey provider!")
            break
        if i > 0 and url == prev_url:
            break

    # Check page content
    js_code = 'document.body ? document.body.innerText.substring(0, 1000) : ""'
    with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False) as f:
        f.write(js_code)
        p = f.name
    out2, _ = await _run_harness(
        f'with open("{p}") as f:\n    _js = f.read()\ntext = js(_js)\nprint("TEXT:", text[:800])\nprint("RESULT: done")',
        timeout=15,
        cdp_url=cdp,
    )
    for line in out2.split("\n"):
        if "TEXT:" in line:
            print(f"\nPage content:\n{line.strip()[5:600]}")


asyncio.run(main())
