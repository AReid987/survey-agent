# Iframe-Based Survey Content: Implementation Plan

## Executive Summary

Survey providers (SSI/Dynata, Cint, PureSpectrum, Kantar, Lucid) embed their survey content inside cross-origin iframes. The agent currently reads `document.body.innerText` in the main page context, which **cannot see iframe content**. This plan details the best approaches to detect, enter, read, and interact with iframe-based survey content.

**Two recommended approaches:**

| Approach | Complexity | Success Rate | Integration Effort |
|---|---|---|---|
| **A: Chrome flags + enhanced JS** (primary) | Low | ~85% | 2-3 files |
| **B: CDP Target.setAutoAttach** (fallback) | High | ~95% | 4-5 files |

---

## Approach A (Primary): Chrome flags + recursive iframe JS

### 1. Chrome Launch Flags

Add to `harness_backend.py` args list in `start()`:

```python
# Line ~611-625 in harness_backend.py, add these flags:
args = [
    chrome_bin,
    f"--remote-debugging-port={CDP_PORT}",
    f"--user-data-dir={AGENT_CHROME_PROFILE}",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-extensions",
    "--disable-popup-blocking",
    "--disable-session-crashed-bubble",
    "--remote-allow-origins=*",
    "--start-maximized",
    "--disable-blink-features=AutomationControlled",
    # NEW: Disable web security for cross-origin iframe access
    "--disable-web-security",
    "--disable-site-isolation-trials",
    # Also disable site-per-process for OOPIF bypass
    "--disable-features=IsolateOrigins,site-per-process",
]
```

**Why these flags:**
- `--disable-web-security`: Allows `iframe.contentDocument` to work cross-origin
- `--disable-site-isolation-trials`: Required since Chrome 67+ for `--disable-web-security` to take effect
- `--disable-features=IsolateOrigins,site-per-process`: Prevents Chrome from moving cross-origin iframes into separate processes (OOPIF), which breaks `contentDocument` access at the JS level

**Risk:** Minimal for a headless/dedicated automation browser. Does NOT affect the user's main Chrome. The agent already uses a separate profile directory (`AGENT_CHROME_PROFILE`).

### 2. Enhanced Iframe Detection & Content Extraction JS

Replace the current basic iframe detection in the page reader with a comprehensive recursive JS module. Create `/tmp/_bh_iframe_probe.js`:

```javascript
/**
 * Recursive iframe detector — finds ALL iframes at any depth.
 * Returns structured data about each iframe and its content.
 *
 * When --disable-web-security is active, this can access cross-origin iframes
 * via contentDocument. When not, it reports the frame but marks it as cross-origin.
 */
(() => {
  const results = [];

  function probeFrame(frame, depth, parentIdx) {
    const items = [];

    // Find all iframes at the current level
    const iframes = frame.querySelectorAll
      ? frame.querySelectorAll('iframe, frame, object[type="text/html"], embed[type="text/html"]')
      : [];

    iframes.forEach((el, idx) => {
      const rect = el.getBoundingClientRect();
      const frameInfo = {
        idx: items.length,
        parentIdx: parentIdx,
        depth: depth,
        visible: rect.width > 0 && rect.height > 0,
        width: rect.width,
        height: rect.height,
        src: (el.src || el.data || '').substring(0, 500),
        id: el.id || '',
        name: el.name || '',
        className: (el.className || '').substring(0, 100),
        title: el.title || '',
        sandbox: el.sandbox ? el.sandbox.value : '',
        loading: el.loading || '',
      };

      // Try to access contentDocument
      let doc = null;
      try {
        doc = el.contentDocument || el.contentWindow?.document;
        frameInfo.access = doc ? 'same-origin' : 'no-doc';
      } catch (e) {
        frameInfo.access = 'cross-origin';
        frameInfo.accessError = e.message;
      }

      if (doc && doc.body) {
        // Read text content
        frameInfo.textLength = (doc.body.innerText || '').length;
        frameInfo.text = (doc.body.innerText || '').substring(0, 5000);

        // Find interactive elements (mirrors the main page query)
        const els = [];
        const formEls = doc.querySelectorAll(
          'input, select, textarea, button, [role=button], [role=radio], [role=option], [role=checkbox]'
        );
        formEls.forEach(el => {
          if (el.offsetParent === null && el.type !== 'radio' && el.type !== 'checkbox') return;
          const tag = el.tagName.toLowerCase();
          let text = (el.textContent || el.value || '').trim().substring(0, 100);
          if ((el.type === 'radio' || el.type === 'checkbox') && !text) {
            text = el.getAttribute('aria-label') || '';
            if (!text && el.id) {
              const lbl = doc.querySelector('label[for="' + el.id + '"]');
              if (lbl) text = lbl.textContent.trim();
            }
            if (!text) {
              const p = el.closest('label') || el.parentElement;
              if (p) text = (p.textContent || '').trim().replace(/\s+/g, ' ').substring(0, 100);
            }
          }
          const type = el.type || el.getAttribute('role') || '';
          const name = el.name || '';
          const id = el.id || '';
          const cls = (el.className || '').substring(0, 60);
          const checked = el.checked || false;
          const placeholder = el.placeholder || '';
          const opts = tag === 'select'
            ? Array.from(el.options).map(o => ({ v: o.value, t: o.text.trim() })).slice(0, 20)
            : [];
          els.push({ tag, type, name, id, text, cls, checked, placeholder, opts });
        });
        frameInfo.elements = els;
        frameInfo.elementCount = els.length;

        // Recursive: check for nested iframes
        const nested = probeFrame(doc, depth + 1, parentIdx !== null ? parentIdx + '-' + idx : '' + idx);
        frameInfo.nestedFrames = nested;
      }

      items.push(frameInfo);
    });

    return items;
  }

  // Start from the top document
  const allFrames = probeFrame(document, 0, null);

  // Also check shadow DOM roots
  function probeShadowRoots(root, depth, parentPath) {
    const shadowItems = [];
    const all = root.querySelectorAll('*');
    all.forEach(el => {
      if (el.shadowRoot) {
        const shadowDoc = el.shadowRoot;
        const shadowIframes = shadowDoc.querySelectorAll('iframe');
        shadowIframes.forEach((iframe, idx) => {
          const rect = iframe.getBoundingClientRect();
          const info = {
            idx: idx,
            parentPath: parentPath,
            depth: depth,
            inShadowDOM: true,
            visible: rect.width > 0 && rect.height > 0,
            width: rect.width,
            height: rect.height,
            src: (iframe.src || '').substring(0, 500),
            id: iframe.id || '',
            name: iframe.name || '',
          };
          try {
            const doc = iframe.contentDocument || iframe.contentWindow?.document;
            info.access = doc ? 'same-origin' : 'no-doc';
            if (doc && doc.body) {
              info.text = (doc.body.innerText || '').substring(0, 3000);
              info.textLength = (doc.body.innerText || '').length;
            }
          } catch (e) {
            info.access = 'cross-origin';
          }
          shadowItems.push(info);
        });
      }
    });
    return shadowItems;
  }

  const shadowFrames = probeShadowRoots(document, 0, '');

  return JSON.stringify({
    totalFrames: allFrames.length,
    totalShadowFrames: shadowFrames.length,
    frames: allFrames,
    shadowFrames: shadowFrames,
  });
})();
```

### 3. Enhanced Page Reader with Full Iframe Support

Replace `_page_reader.py` with a version that:

1. Reads main page text AND all accessible iframe texts
2. Merges elements from ALL frames (main + iframes)
3. Propagates frame ownership info so actions can target specific frames

Key code:

```python
"""Enhanced page reader with full iframe support."""
import json
import time

# Dismiss cookie overlays
js("window.alert = function(){}; window.confirm = function(){return true}; window.prompt = function(){return null}")
js(
    "document.querySelector('.cky-btn-accept')?.click(); "
    "document.querySelectorAll('button').forEach(function(b){"
    "var t=(b.textContent||'').trim().toLowerCase(); "
    "if(t==='accept all'||t==='accept'||t==='save my preferences'){b.click()}})"
)
time.sleep(1)
wait_for_load()

# Read main page text
with open("/tmp/_bh_text_js.js") as f:
    _text_js = f.read()
text = js(_text_js)
url = current_tab()["url"]

# Read main page elements
with open("__ELEMENTS_JS_PATH__") as f:
    _elements_js = f.read()
main_elements = js(_elements_js)

# Probe ALL iframes (enhanced version always runs)
with open("/tmp/_bh_iframe_probe.js") as f:
    _iframe_probe_js = f.read()
iframe_info_raw = js(_iframe_probe_js)
print("IFRAME_PROBE:", iframe_info_raw)

try:
    iframe_data = json.loads(iframe_info_raw)
except Exception:
    iframe_data = {"frames": [], "totalFrames": 0}

# Collect all texts and elements from accessible iframes
all_texts = [text]
all_elements_raw = main_elements

# Also read elements from main page for direct form detection
try:
    main_els = json.loads(main_elements)
except Exception:
    main_els = []

# Process each accessible iframe
iframe_elements_list = []
for frame in iframe_data.get("frames", []):
    if frame.get("access") == "same-origin" and frame.get("text"):
        all_texts.append(frame["text"])
        if "elements" in frame and frame["elements"]:
            # Tag each element with its frame index for targeting
            for el in frame["elements"]:
                el["_frameIdx"] = frame.get("idx", 0)
                el["_frameDepth"] = frame.get("depth", 0)
                el["_frameSrc"] = frame.get("src", "")
            iframe_elements_list.extend(frame["elements"])
    # Process nested iframes
    for nested in frame.get("nestedFrames", []):
        if nested.get("access") == "same-origin" and nested.get("text"):
            all_texts.append(nested["text"])

# Merge all elements: main page elements + iframe elements
merged_elements = main_els + iframe_elements_list

# Use merged content (prefer iframe content when main page is sparse)
merged_text = "\n---FRAME---\n".join(all_texts)
if len(text.strip()) < 600 and iframe_elements_list:
    elements_raw = json.dumps(merged_elements)
else:
    elements_raw = main_elements

print("PAGE_TEXT:", merged_text if len(merged_text) > len(text) else text)
print("ELEMENTS:", elements_raw)
print("IFRAME_COUNT:", iframe_data.get("totalFrames", 0))
print("URL:", url)
print("RESULT: page_read")
```

### 4. Iframe-Aware Answer Application

The `_apply_answers` method in `harness_backend.py` currently uses `document.querySelector()` to find elements. This needs to be extended to:

1. Check if an element belongs to an iframe (via `_frameIdx` metadata)
2. If yes, find the iframe element, enter its `contentDocument`, and query within it

```python
# Enhanced JS for _apply_answers that supports iframe elements
_APPLY_ANSWERS_IFRAME_JS = """
(() => {
  const answers = $_ANSWERS_JSON;
  let applied = 0;

  /**
   * Find an element across main document and all iframes.
   * Recursively searches iframe contentDocuments.
   */
  function findInFrames(rootDoc, selector) {
    // Search in the current document
    let el = rootDoc.querySelector(selector);
    if (el) return { element: el, doc: rootDoc };

    // Search in all iframes at this level
    const iframes = rootDoc.querySelectorAll('iframe, frame');
    for (const iframe of iframes) {
      try {
        const doc = iframe.contentDocument || iframe.contentWindow?.document;
        if (doc) {
          const result = findInFrames(doc, selector);
          if (result) return result;
        }
      } catch(e) {
        // Cross-origin iframe — skip
        continue;
      }
    }
    return null;
  }

  for (const ans of answers) {
    const sel = ans.selector || '';
    const val = String(ans.value || '');
    const action = ans.action || 'fill';
    if (!sel) continue;

    try {
      // Search across all frames
      const found = findInFrames(document, sel);
      if (!found) {
        // Fallback: try text-content matching
        if (action === 'click' && val) {
          const allClickable = document.querySelectorAll(
            '[role=radio], [role=option], [role=checkbox], [role=button], ' +
            'button, a, label, li, input[type=radio], input[type=checkbox]'
          );
          for (const candidate of allClickable) {
            const text = (candidate.textContent || '').trim();
            if (text === val || text.toLowerCase() === val.toLowerCase()) {
              candidate.click(); applied++;
              break;
            }
          }
        }
        continue;
      }

      const el = found.element;
      if (action === 'click') {
        el.click(); applied++;
      } else if (action === 'fill') {
        el.focus();
        el.value = val;
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        applied++;
      } else if (action === 'select') {
        el.value = val;
        el.dispatchEvent(new Event('change', {bubbles: true}));
        applied++;
      }
    } catch(e) {
      // Silently skip failed elements
    }
  }
  return 'APPLIED:' + applied + '/' + answers.length;
})()
"""
```

### 5. Iframe-Aware "Next" Button Clicking

The `_click_next` method also needs to search inside iframes:

```python
async def _click_next(self) -> bool:
    script = """
import time
result = js("""
(() => {
    const selectors = [
        'button[type=submit]', 'input[type=submit]',
        'button.next', 'button.continue', 'a.next',
        '[data-testid=next]', '[data-testid=submit]'
    ];

    // Search in main document
    function findButton(doc) {
        for (const sel of selectors) {
            const el = doc.querySelector(sel);
            if (el && el.offsetParent !== null) return el;
        }
        const keywords = ['next', 'continue', 'submit', 'proceed', 'done', 'finish', '>>'];
        const btns = doc.querySelectorAll('button, input[type=submit], a, [role=button]');
        for (const btn of btns) {
            const text = (btn.textContent || btn.value || '').toLowerCase().trim();
            if (keywords.some(k => text.includes(k)) && btn.offsetParent !== null) return btn;
        }
        // Search in iframes
        const iframes = doc.querySelectorAll('iframe, frame');
        for (const iframe of iframes) {
            try {
                const idoc = iframe.contentDocument || iframe.contentWindow?.document;
                if (idoc) {
                    const found = findButton(idoc);
                    if (found) return found;
                }
            } catch(e) { continue; }
        }
        return null;
    }

    const btn = findButton(document);
    if (btn) { btn.click(); return 'found:' + (btn.textContent || btn.value || '').trim().substring(0, 30); }
    return 'none';
})()
""")
if result and result != 'none':
    print("CLICKED:", result)
    time.sleep(1)
    print("RESULT: next_clicked")
else:
    print("RESULT: no_next_button")
"""
    output, rc = await _run_harness(script, timeout=15, cdp_url=self._cdp_url)
    ...
```

---

## Approach B (Fallback/Advanced): CDP Target.setAutoAttach + Isolated Worlds

When Approach A fails (e.g., survey provider has additional protections), use raw CDP sessions to access cross-origin iframes even without `--disable-web-security`.

### Architecture

```
┌─────────────────────────────────────────────────┐
│            browser-harness (Python)              │
│  js() → main page context (Runtime.evaluate)     │
│                                                   │
│  ┌─────────────────────────────────────────────┐ │
│  │  iframe_manager.py (NEW)                     │ │
│  │  ─────────────────                           │ │
│  │  - Connects to Chrome via raw WebSocket CDP  │ │
│  │  - Gets frame tree via Page.getFrameTree()   │ │
│  │  - Enables Target.setAutoAttach              │ │
│  │  - Creates isolated worlds per iframe        │ │
│  │  - Executes JS in iframe contexts            │ │
│  │  - Maps frameId ↔ executionContextId         │ │
│  └─────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────┘
```

### Key CDP Commands

```python
class CdpIframeManager:
    """Manages cross-origin iframe access via raw CDP WebSocket."""

    def __init__(self, cdp_url: str):
        self.cdp_url = cdp_url
        self.ws: websocket.WebSocket | None = None
        self._msg_id = 0
        self._frames: dict[str, dict] = {}  # frameId -> frame info
        self._contexts: dict[str, int] = {}  # frameId -> executionContextId
        self._sessions: dict[str, str] = {}  # targetId -> sessionId (for OOPIF)
        self._target_to_frame: dict[str, str] = {}  # targetId -> frameId

    async def connect(self):
        """Connect to Chrome CDP WebSocket and setup auto-attach."""
        # Get the WebSocket URL from the HTTP endpoint
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(self.cdp_url + "/json/version")
            ws_url = resp.json().get("webSocketDebuggerUrl")

        self.ws = await websocket.connect(ws_url)

        # Enable necessary domains
        await self._send("Target.setAutoAttach", {
            "autoAttach": True,
            "waitForDebuggerOnStart": False,
            "flatten": True,
        })
        await self._send("Page.enable")
        await self._send("Runtime.enable")

        # Listen for frame/target events
        self._start_event_listener()

        # Get initial frame tree
        result = await self._send("Page.getFrameTree")
        self._process_frame_tree(result.get("frameTree", {}))

    async def _send(self, method: str, params: dict = None) -> dict:
        """Send CDP command and wait for response."""
        self._msg_id += 1
        msg = {"id": self._msg_id, "method": method, "params": params or {}}
        await self.ws.send(json.dumps(msg))
        # Read responses until we get our match
        while True:
            resp = json.loads(await self.ws.recv())
            if resp.get("id") == self._msg_id:
                return resp.get("result", {})

    async def get_all_iframe_contents(self) -> list[dict]:
        """Get text and elements from ALL iframes (including cross-origin)."""
        results = []
        for frame_id, frame_info in self._frames.items():
            if frame_id == self._root_frame_id:
                continue  # Skip main page

            # Create isolated world for cross-origin access
            world_result = await self._send("Page.createIsolatedWorld", {
                "frameId": frame_id,
                "worldName": f"survey_agent::iframe::{frame_id}",
                "grantUniveralAccess": True,  # NOTE: typo is intentional in CDP API
            })
            context_id = world_result.get("executionContextId")

            # Get frame info
            results.append({
                "frameId": frame_id,
                "url": frame_info.get("url", ""),
                "parentId": frame_info.get("parentId", ""),
                "text": await self._evaluate_in_context(context_id,
                    "document.body ? document.body.innerText.substring(0, 5000) : ''"),
                "elements": await self._evaluate_in_context(context_id,
                    _FRAME_ELEMENTS_JS),
            })
        return results

    async def execute_in_iframe(
        self,
        frame_id: str,
        script: str,
    ) -> str:
        """Execute arbitrary JS in a specific iframe context."""
        if frame_id not in self._contexts:
            # Create isolated world
            world_result = await self._send("Page.createIsolatedWorld", {
                "frameId": frame_id,
                "worldName": f"survey_agent::eval::{frame_id}",
                "grantUniveralAccess": True,
            })
            self._contexts[frame_id] = world_result.get("executionContextId")

        return await self._evaluate_in_context(self._contexts[frame_id], script)

    async def _evaluate_in_context(self, context_id: int, expression: str) -> str:
        """Evaluate JS in a specific execution context."""
        result = await self._send("Runtime.evaluate", {
            "contextId": context_id,
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        if "exceptionDetails" in result:
            return f"ERROR: {result['exceptionDetails']}"
        return result.get("result", {}).get("value", "")

    def _process_frame_tree(self, tree: dict, parent_id: str = None):
        """Recursively process frame tree."""
        frame = tree.get("frame", {})
        fid = frame.get("id")
        self._frames[fid] = {
            "id": fid,
            "url": frame.get("url", ""),
            "parentId": parent_id,
        }
        if not parent_id:
            self._root_frame_id = fid

        for child in tree.get("childFrames", []):
            self._process_frame_tree(child, fid)
```

### Integration with browser-harness

```python
# In harness_backend.py, add an iframe_manager property

class BrowserHarnessBackend(SurveyAgentBackend):
    def __init__(self, ...):
        ...
        self._iframe_manager: CdpIframeManager | None = None

    async def start(self):
        ...existing Chrome launch...
        # Initialize CDP iframe manager
        self._iframe_manager = CdpIframeManager(self._cdp_url)
        await self._iframe_manager.connect()

    async def _read_page_with_iframes(self) -> dict:
        """Read page content from main page + all iframes."""
        # Main page via browser-harness js()
        output, _ = await _run_harness("""
text = js("document.body ? document.body.innerText.substring(0, 5000) : ''")
elements = js("__ELEMENTS_JS__")
print("TEXT:", text)
print("ELEMENTS:", elements)
""")
        # Iframe contents via CDP iframe manager
        iframe_contents = await self._iframe_manager.get_all_iframe_contents()

        # Merge: main page text + all iframe texts
        combined_text = self._extract_text(output)
        combined_elements = self._extract_elements(output)

        for ifc in iframe_contents:
            if ifc.get("text") and len(ifc["text"]) > 50:
                combined_text += f"\n--- IFRAME ({ifc.get('url','')[:60]}) ---\n" + ifc["text"]
            if ifc.get("elements"):
                frame_els = json.loads(ifc["elements"])
                for el in frame_els:
                    el["_frameId"] = ifc["frameId"]
                combined_elements.extend(frame_els)

        return {"text": combined_text, "elements": json.dumps(combined_elements)}
```

---

## Survey Provider Specific Patterns

### SSI / Dynata
- **Pattern:** Surveys embedded via `https://www.surveysampling.com/...` or dedicated survey subdomain
- **Iframe behavior:** Cross-origin iframe, often with dynamic sizing
- **Detection:** Look for `src` containing `surveysampling.com`, `dynata.com`, or `ssisurveys.com`
- **Strategy:** Approach A works if --disable-web-security is active; the `contentDocument` becomes accessible

### Cint
- **Pattern:** Surveys via `https://www.cint.com/...` or `https://exchange.cint.com/...`
- **Iframe behavior:** Cross-origin, uses postMessage for communication between parent and iframe
- **Detection:** Look for `src` containing `cint.com`, `exchange.cint.com`
- **Strategy:** Approach B if A fails — Cint surveys often have additional JS protections. Their iframe may navigate to different survey URLs internally.

### PureSpectrum
- **Pattern:** PureSpectrum does NOT officially support iframes per their docs ("iFrames are considered a security risk"). They use redirect-based survey delivery.
- **Strategy:** If the survey opens via redirect (not iframe), normal page reading works. If there IS an iframe, it's likely a third-party provider embedded BY PureSpectrum's panel partner.

### Kantar
- **Pattern:** Uses Cint for panel delivery per their partnership. Surveys may come via Cint's iframe system.
- **Strategy:** Apply Cint strategies.

### Lucid / Fulcrum
- **Pattern:** Marketplace platform. Survey delivery can be iframe or popup/redirect.
- **Detection:** Look for `src` containing `luc.id`, `fulcrum.com`
- **Strategy:** Both approaches work; Lucid surveys are typically standard iframe embeds.

### YouGov
- **Pattern:** Surveys typically open in a new tab or redirect, not iframes.
- **Strategy:** Standard page reading works. Iframe handling is usually unnecessary.

### Branded Surveys
- **Pattern:** Surveys launched from dashboard open in new tab or are iframed via survey routers.
- **Strategy:** After clicking "Take Survey," the survey may open in an iframe on the dashboard, or redirect to a new tab. Handle both.

---

## Implementation Order

### Phase 1: Quick Win (Approach A)
1. Add Chrome flags to `harness_backend.py` (`--disable-web-security`, etc.)
2. Create enhanced iframe probe JS (`/tmp/_bh_iframe_probe.js`)
3. Update `_page_reader.py` to use new probe and merge results
4. Update `_apply_answers` JS to search across iframes
5. Update `_click_next` JS to search across iframes

### Phase 2: Full Integration
6. Update `_generate_structured_answers` to pass merged iframe content to LLM
7. Add iframe-aware fallback in `complete_survey` loop (retry with iframe detection if sparse)
8. Update `_handle_region_selector` to search inside iframes

### Phase 3: Advanced (Approach B)
9. Create `iframe_manager.py` with raw CDP WebSocket handling
10. Create alternative page reader that uses CDP isolated worlds
11. Add configuration toggle to switch between approaches

---

## File Changes Summary

| File | Change |
|---|---|
| `harness_backend.py` | Add Chrome flags; update `_apply_answers`, `_click_next`, `_read_page_with_iframes` |
| `_page_reader.py` | Complete rewrite with recursive iframe probing |
| `llm_answers.py` (new) | Update `parse_question_groups` to handle `_frameId` metadata |
| `iframe_manager.py` (new) | Raw CDP WebSocket handler for Approach B |
| `tmp/_bh_iframe_probe.js` | New recursive iframe detection & content extraction JS |

---

## Testing Strategy

1. **Unit test:** Create a test HTML page with same-origin iframe, cross-origin iframe, nested iframes, and shadow DOM iframe
2. **Integration test:** Verify `_page_reader.py` correctly extracts content from all frame types
3. **Live test:** Run against a real SSI/Dynata survey provider to validate end-to-end
4. **Regression test:** Verify existing non-iframe surveys still work unchanged

---

## Error Handling & Edge Cases

- **No iframes present:** Page reader falls back to existing behavior seamlessly
- **Iframe not yet loaded:** Wait for `load` event on each iframe before probing
- **Iframe navigating mid-probe:** Content may change; re-probe on each page turn
- **All iframes cross-origin (no --disable-web-security):** Fall back to OCR/screenshot approach (existing CAPTCHA solver infrastructure can be reused)
- **Blank iframe (about:blank):** Skip — survey content hasn't loaded yet
- **Sandboxed iframes:** Some iframes have `sandbox` attribute preventing script access — Approach A may still fail; fall back to Approach B or coordinate-click approach
