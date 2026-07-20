"""CDP Iframe Manager — raw WebSocket CDP interface for cross-origin iframe access.

This module provides an alternative approach (Approach B) for accessing
cross-origin iframe content WITHOUT needing --disable-web-security.

How it works:
1. Connects to Chrome's CDP via WebSocket (alongside browser-harness)
2. Enables Target.setAutoAttach to get notified of cross-origin iframe targets
3. Uses Page.getFrameTree() to discover all frames
4. For each cross-origin iframe, creates an isolated world via
   Page.createIsolatedWorld() with grantUniversalAccess=true
5. Executes JS in the iframe's context using Runtime.evaluate with contextId

Usage:
    manager = CdpIframeManager("http://127.0.0.1:19333")
    await manager.connect()
    contents = await manager.get_all_iframe_contents()
    # contents = [{"frameId": "...", "text": "...", "elements": [...]}]
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ── JS snippet to extract interactive elements from a frame ───────────
_FRAME_ELEMENTS_JS = """
(() => {
  if (!document.body) return '[]';
  const els = [];
  const FORM_SELECTOR = 'input, select, textarea, button, [role="button"], ' +
    '[role="radio"], [role="option"], [role="checkbox"], [role="switch"], ' +
    '[role="tab"], [role="link"], [role="searchbox"], [role="combobox"]';
  const formEls = document.querySelectorAll(FORM_SELECTOR);
  formEls.forEach(function(el) {
    if (el.offsetParent === null && el.type !== 'radio' && el.type !== 'checkbox') return;
    const tag = el.tagName.toLowerCase();
    let text = (el.textContent || el.value || '').trim().substring(0, 100);
    if ((el.type === 'radio' || el.type === 'checkbox') && !text) {
      text = el.getAttribute('aria-label') || '';
      if (!text && el.id) {
        const lbl = document.querySelector('label[for="' + el.id + '"]');
        if (lbl) text = lbl.textContent.trim();
      }
      if (!text) {
        const p = el.closest('label') || el.parentElement;
        if (p) text = (p.textContent || '').trim().replace(/\\s+/g, ' ').substring(0, 100);
      }
    }
    const type = el.type || el.getAttribute('role') || '';
    const name = el.name || '';
    const id = el.id || '';
    const cls = (el.className || '').substring(0, 60);
    const checked = el.checked || false;
    const placeholder = el.placeholder || '';
    const opts = tag === 'select' ? Array.from(el.options).map(o => ({v: o.value, t: o.text.trim()})).slice(0, 20) : [];
    els.push({tag, type, name, id, text, cls, checked, placeholder, opts});
  });
  return JSON.stringify(els.slice(0, 100));
})()
"""

# Default CDP port (same as harness_backend.CDP_PORT)
_CDP_PORT = 19333


class CdpIframeError(Exception):
    """Base exception for CDP iframe operations."""


class CdpConnectionError(CdpIframeError):
    """Failed to connect to Chrome CDP endpoint."""


class CdpIframeManager:
    """Manages cross-origin iframe access via raw CDP WebSocket.

    Architecture:
    - Opens a raw WebSocket to Chrome's CDP (separate from browser-harness)
    - Uses Target.setAutoAttach + Page.createIsolatedWorld to access
      cross-origin iframe content
    - Maintains a mapping of frameId -> executionContextId
    """

    def __init__(self, cdp_http_url: str = f"http://127.0.0.1:{_CDP_PORT}"):
        self._cdp_http_url = cdp_http_url
        self._ws_url: str | None = None
        self._ws: Any | None = None  # websocket.WebSocket
        self._msg_id = 0
        self._root_frame_id: str | None = None
        self._frames: dict[str, dict] = {}  # frameId -> frame info
        self._contexts: dict[str, int] = {}  # frameId -> isolated world contextId
        self._sessions: dict[str, str] = {}  # targetId -> sessionId
        self._connected = False
        self._pending: dict[int, asyncio.Future] = {}

    async def connect(self) -> None:
        """Connect to Chrome CDP and set up frame monitoring."""
        # Step 1: Get WebSocket URL from HTTP endpoint
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(self._cdp_http_url + "/json/version", timeout=5)
                self._ws_url = resp.json().get("webSocketDebuggerUrl")
        except Exception as e:
            raise CdpConnectionError(
                f"Cannot connect to {self._cdp_http_url}/json/version: {e}"
            ) from e

        if not self._ws_url:
            raise CdpConnectionError(
                f"No webSocketDebuggerUrl at {self._cdp_http_url}"
            )

        # Step 2: Open WebSocket connection
        try:
            import websockets
            self._ws = await websockets.connect(self._ws_url, max_size=10 * 1024 * 1024)
        except ImportError:
            # Fallback: try websocket-client
            import websocket
            self._ws = websocket.create_connection(self._ws_url, timeout=10)

        # Step 3: Enable domains and set up auto-attach
        await self._send("Target.setAutoAttach", {
            "autoAttach": True,
            "waitForDebuggerOnStart": False,
            "flatten": True,
        })

        await self._send("Page.enable")
        await self._send("Runtime.enable")

        # Step 4: Get frame tree
        tree_result = await self._send("Page.getFrameTree")
        frame_tree = tree_result.get("frameTree", {})
        self._process_frame_tree(frame_tree)

        self._connected = True
        logger.info(
            "CDP Iframe Manager connected. Root frame: %s, Total frames: %d",
            self._root_frame_id,
            len(self._frames),
        )

    async def disconnect(self) -> None:
        """Close the CDP WebSocket connection."""
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._connected = False

    async def _send(self, method: str, params: dict | None = None) -> dict:
        """Send a CDP command and wait for the response."""
        self._msg_id += 1
        msg = json.dumps({
            "id": self._msg_id,
            "method": method,
            "params": params or {},
        })

        if hasattr(self._ws, "send"):
            await self._ws.send(msg)
        else:
            self._ws.send(msg)

        # Wait for our response
        while True:
            if hasattr(self._ws, "recv"):
                raw = await self._ws.recv()
            else:
                raw = self._ws.recv()

            resp = json.loads(raw)

            # Check if this is the response to our command
            if resp.get("id") == self._msg_id:
                if "error" in resp:
                    raise CdpIframeError(
                        f"CDP error for {method}: {resp['error']}"
                    )
                return resp.get("result", {})

            # Handle events (store for future use)
            if "method" in resp:
                await self._handle_event(resp["method"], resp.get("params", {}))

    async def _handle_event(self, method: str, params: dict) -> None:
        """Process CDP events for frame tracking."""
        if method == "Page.frameAttached":
            frame_id = params.get("frameId", "")
            parent_id = params.get("parentFrameId", "")
            self._frames[frame_id] = {
                "id": frame_id,
                "parentId": parent_id,
                "url": "",
            }

        elif method == "Page.frameNavigated":
            frame = params.get("frame", {})
            frame_id = frame.get("id", "")
            if frame_id in self._frames:
                self._frames[frame_id]["url"] = frame.get("url", "")
            else:
                # New frame not yet tracked
                self._frames[frame_id] = {
                    "id": frame_id,
                    "parentId": frame.get("parentId"),
                    "url": frame.get("url", ""),
                }

        elif method == "Page.frameDetached":
            frame_id = params.get("frameId", "")
            self._frames.pop(frame_id, None)
            self._contexts.pop(frame_id, None)

        elif method == "Target.attachedToTarget":
            # Cross-origin iframe attached as separate target
            target_info = params.get("targetInfo", {})
            target_id = target_info.get("targetId", "")
            session_id = params.get("sessionId", "")
            if target_id and session_id:
                self._sessions[target_id] = session_id

    def _process_frame_tree(self, tree: dict, parent_id: str | None = None) -> None:
        """Recursively process the CDP frame tree."""
        frame = tree.get("frame", {})
        fid = frame.get("id", "")
        self._frames[fid] = {
            "id": fid,
            "url": frame.get("url", ""),
            "parentId": parent_id,
        }
        if not parent_id:
            self._root_frame_id = fid

        for child in tree.get("childFrames", []):
            self._process_frame_tree(child, fid)

    async def _create_isolated_world(self, frame_id: str) -> int:
        """Create an isolated JS world in a frame with universal access.

        NOTE: The CDP API has a typo — the parameter is 'grantUniveralAccess'
        (missing the 's' in 'Univeral'). This is NOT our typo; it's in Chrome's
        own protocol definition.
        """
        result = await self._send("Page.createIsolatedWorld", {
            "frameId": frame_id,
            "worldName": f"survey_agent_iframe::{frame_id[:8]}",
            "grantUniveralAccess": True,
        })
        context_id = result.get("executionContextId")
        if not context_id:
            raise CdpIframeError(
                f"No executionContextId from createIsolatedWorld for {frame_id}"
            )
        self._contexts[frame_id] = context_id
        return context_id

    async def evaluate_in_frame(self, frame_id: str, expression: str) -> str:
        """Evaluate JavaScript in a specific frame's context.

        Creates an isolated world on first access, reuses on subsequent calls.
        """
        # Create isolated world if not yet created
        if frame_id not in self._contexts:
            await self._create_isolated_world(frame_id)

        context_id = self._contexts[frame_id]

        result = await self._send("Runtime.evaluate", {
            "contextId": context_id,
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })

        if "exceptionDetails" in result:
            exc = result["exceptionDetails"]
            text = exc.get("text", "unknown error")
            desc = exc.get("exception", {}).get("description", "")
            return f"EVAL_ERROR: {text}\n{desc}"

        value = result.get("result", {}).get("value", "")
        return str(value) if value is not None else ""

    async def get_frame_text(self, frame_id: str) -> str:
        """Get innerText from a specific frame."""
        return await self.evaluate_in_frame(
            frame_id,
            "document.body ? document.body.innerText.substring(0, 5000) : ''"
        )

    async def get_frame_elements(self, frame_id: str) -> list[dict]:
        """Get interactive elements from a specific frame."""
        raw = await self.evaluate_in_frame(frame_id, _FRAME_ELEMENTS_JS)
        try:
            return json.loads(raw) if raw and not raw.startswith("EVAL_ERROR") else []
        except (json.JSONDecodeError, TypeError):
            return []

    async def get_all_iframe_contents(
        self,
        include_nested: bool = True,
    ) -> list[dict]:
        """Get text content and interactive elements from ALL iframes.

        Returns a list of dicts:
        [
            {
                "frameId": "...",
                "parentId": "...",
                "url": "...",
                "text": "...",
                "elements": [...],
            },
            ...
        ]
        """
        if not self._connected:
            raise CdpIframeError("Not connected. Call connect() first.")

        results = []

        for frame_id, info in self._frames.items():
            # Skip the root (main page) frame — browser-harness handles that
            if frame_id == self._root_frame_id:
                continue

            # Skip about:blank frames (usually loading placeholders)
            url = info.get("url", "")
            if url == "about:blank":
                continue

            try:
                text = await self.get_frame_text(frame_id)
                elements = await self.get_frame_elements(frame_id)

                # Only report frames with actual content
                if text and len(text.strip()) > 20:
                    results.append({
                        "frameId": frame_id,
                        "parentId": info.get("parentId"),
                        "url": url,
                        "text": text,
                        "elements": elements,
                        "elementCount": len(elements),
                    })
            except Exception as e:
                logger.warning("Could not read frame %s: %s", frame_id, e)
                continue

        if include_nested:
            # Nested frames are already in self._frames with parentId chain;
            # no special handling needed since we iterate all frames
            pass

        return results

    async def execute_interaction(
        self,
        frame_id: str,
        selector: str,
        action: str = "click",
        value: str = "",
    ) -> bool:
        """Execute an action (click, fill, select) on an element inside a frame."""
        js_code = ""

        if action == "click":
            js_code = f"""
(() => {{
    const el = document.querySelector('{selector}');
    if (!el) return false;
    el.click();
    return true;
}})()
            """
        elif action == "fill":
            safe_value = value.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"')
            js_code = f"""
(() => {{
    const el = document.querySelector('{selector}');
    if (!el) return false;
    el.focus();
    el.value = '{safe_value}';
    el.dispatchEvent(new Event('input', {{bubbles: true}}));
    el.dispatchEvent(new Event('change', {{bubbles: true}}));
    return true;
}})()
            """
        elif action == "select":
            safe_value = value.replace("'", "\\'")
            js_code = f"""
(() => {{
    const el = document.querySelector('{selector}');
    if (!el) return false;
    el.value = '{safe_value}';
    el.dispatchEvent(new Event('change', {{bubbles: true}}));
    return true;
}})()
            """

        result = await self.evaluate_in_frame(frame_id, js_code)
        return result == "true"

    async def click_next_in_frame(self, frame_id: str) -> bool:
        """Find and click a 'Next'/'Continue' button inside a frame."""
        js_code = """
(() => {
    const keywords = ['next', 'continue', 'submit', 'proceed', 'done', 'finish', '>>'];
    const btns = document.querySelectorAll(
        'button, input[type=submit], a, [role=button]'
    );
    for (const btn of btns) {
        const text = (btn.textContent || btn.value || '').toLowerCase().trim();
        if (keywords.some(k => text.includes(k)) && btn.offsetParent !== null) {
            btn.click();
            return true;
        }
    }
    return false;
})()
        """
        result = await self.evaluate_in_frame(frame_id, js_code)
        return result == "true"

    def get_frame_tree_summary(self) -> dict:
        """Get a summary of the current frame tree for logging/diagnostics."""
        return {
            "rootFrameId": self._root_frame_id,
            "totalFrames": len(self._frames),
            "accessibleWorlds": len(self._contexts),
            "frames": [
                {
                    "id": fid,
                    "url": info.get("url", "")[:80],
                    "parentId": info.get("parentId"),
                    "hasContext": fid in self._contexts,
                }
                for fid, info in self._frames.items()
            ],
        }


# ── Standalone test ────────────────────────────────────────────────────

async def main():
    """Quick test: connect and probe iframes."""
    import sys

    manager = CdpIframeManager(
        sys.argv[1] if len(sys.argv) > 1 else f"http://127.0.0.1:{_CDP_PORT}"
    )

    try:
        await manager.connect()
        print("Frame tree:", json.dumps(manager.get_frame_tree_summary(), indent=2))

        iframe_contents = await manager.get_all_iframe_contents()
        print(f"\nFound {len(iframe_contents)} iframes with content:")

        for i, content in enumerate(iframe_contents):
            print(f"\n  [{i}] Frame: {content['frameId'][:12]}...")
            print(f"      URL: {content['url'][:80]}")
            print(f"      Text: {len(content['text'])} chars")
            print(f"      Elements: {content['elementCount']}")
            if content['text']:
                print(f"      Preview: {content['text'][:200]}...")

        await manager.disconnect()

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
