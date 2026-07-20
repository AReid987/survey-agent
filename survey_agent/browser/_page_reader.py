"""Enhanced page reader with recursive iframe support for browser-harness.

This script is passed to browser-harness via stdin.
It reads page text and interactive elements from the main document
AND all accessible iframes (same-origin, or cross-origin when
--disable-web-security is active).

JS code is loaded from temp files to avoid string escaping issues.
"""

import json
import time

# ── Step 1: Suppress popups and cookie overlays ──────────────────────
js(
    "window.alert = function(){}; "
    "window.confirm = function(){return true}; "
    "window.prompt = function(){return null}"
)
js(
    "document.querySelector('.cky-btn-accept')?.click(); "
    "document.querySelectorAll('button').forEach(function(b){"
    "var t=(b.textContent||'').trim().toLowerCase(); "
    "if(t==='accept all'||t==='accept'||t==='save my preferences'){b.click()}})"
)
time.sleep(1)
wait_for_load()

# ── Step 2: Read main page text ──────────────────────────────────────
with open("/tmp/_bh_text_js.js") as f:
    _text_js = f.read()
text = js(_text_js)

# ── Step 3: Read main page interactive elements ──────────────────────
with open("__ELEMENTS_JS_PATH__") as f:
    _elements_js = f.read()
main_elements_raw = js(_elements_js)

# Parse main elements
try:
    main_elements = json.loads(main_elements_raw) if main_elements_raw else []
except (json.JSONDecodeError, TypeError):
    main_elements = []

# ── Step 4: Probe ALL iframes ────────────────────────────────────────
# Always run the probe, even if main page has content.
# Survey questions may be split between main page and iframe.
with open("/tmp/_bh_iframe_probe.js") as f:
    _iframe_probe_js = f.read()

iframe_probe_raw = js(_iframe_probe_js)
print("IFRAME_PROBE:", iframe_probe_raw[:500])  # Log summary

try:
    iframe_data = json.loads(iframe_probe_raw)
except (json.JSONDecodeError, TypeError) as e:
    iframe_data = {"frames": [], "totalFrames": 0, "accessibleCount": 0}
    print("IFRAME_PARSE_ERROR:", str(e))

# ── Step 5: Collect content from all accessible iframes ──────────────
all_texts = [text]
all_elements = list(main_elements)

# Recursively walk frames and collect content
def collect_frame_content(frames, parent_path=""):
    """Recursively collect text and elements from frames."""
    texts = []
    elements = []
    for frame in frames:
        if frame.get("access") == "same-origin":
            # Add frame text
            frame_text = frame.get("text", "")
            if frame_text and len(frame_text.strip()) > 20:
                src_summary = (frame.get("src", "") or "")[:60]
                texts.append(f"--- FRAME ({src_summary}) ---\n{frame_text}")

            # Add frame elements (tagged with frame metadata)
            frame_els = frame.get("elements", [])
            if frame_els:
                elements.extend(frame_els)

            # Recurse into nested frames
            nested = frame.get("nestedFrames", [])
            if nested:
                n_texts, n_els = collect_frame_content(
                    nested, parent_path + frame.get("idx", "") + "-"
                )
                texts.extend(n_texts)
                elements.extend(n_els)

        elif frame.get("access") == "cross-origin":
            # Log cross-origin frames for diagnostic purposes
            src = frame.get("src", "")[:80]
            rect = frame.get("rect", {})
            print(f"CROSS_ORIGIN_IFRAME: {src} at x={rect.get('x')} y={rect.get('y')}")

    return texts, elements

iframe_texts, iframe_elements = collect_frame_content(
    iframe_data.get("frames", [])
)
all_texts.extend(iframe_texts)
all_elements.extend(iframe_elements)

# ── Step 6: Determine which content to use ───────────────────────────
# Primary heuristic: if main page is sparse (< 600 chars) AND we found
# iframe content, use iframe content as primary.
# Secondary: always merge texts so LLM sees full context.
has_substantial_iframe_content = any(
    len(t) > 200 for t in iframe_texts
)
main_page_sparse = len(text.strip()) < 600

if main_page_sparse and has_substantial_iframe_content:
    # Use iframe content as primary, but also include main page text
    merged_text = "\n\n".join(all_texts)
    elements_raw = json.dumps(all_elements)
    use_iframe_content = True
elif iframe_texts:
    # Main page has content, but iframes also have content — merge
    merged_text = text + "\n" + "\n".join(iframe_texts)
    elements_raw = json.dumps(all_elements)
    use_iframe_content = True
else:
    # No iframe content — use main page as-is
    merged_text = text
    elements_raw = main_elements_raw
    use_iframe_content = False

# ── Step 7: Output ───────────────────────────────────────────────────
# Log diagnostic info
print("PAGE_TEXT_LENGTH:", len(merged_text))
print("IFRAME_COUNT:", iframe_data.get("totalFrames", 0))
print("ACCESSIBLE_IFRAMES:", iframe_data.get("accessibleCount", 0))
print("MAIN_PAGE_ELEMENTS:", len(main_elements))
print("IFRAME_ELEMENTS:", len(iframe_elements))
print("TOTAL_ELEMENTS:", len(json.loads(elements_raw)) if elements_raw else 0)
print("USING_IFRAME_CONTENT:", "yes" if use_iframe_content else "no")

print("PAGE_TEXT:", merged_text)
print("ELEMENTS:", elements_raw)
print("URL:", current_tab()["url"])
print("RESULT: page_read")
