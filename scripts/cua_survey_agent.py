#!/usr/bin/env python3
"""
CUA Survey Agent — drives Chrome at OS level via cua-driver.

Uses:
- cua-driver AX tree for page structure (elements, text, roles)
- cua-driver screenshots + Tesseract OCR for image/iframe text
- OpenCode API directly for answer decisions
- cua-driver clicks/typing for interactions
"""

import asyncio
import base64
import json
import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cua_agent")

# ── cua-driver helpers ────────────────────────────────────────────────


def cua_call(tool, args=None, timeout=30):
    cmd = ["cua-driver", "call", tool]
    if args:
        cmd.append(json.dumps(args))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    try:
        return json.loads(r.stdout.strip())
    except json.JSONDecodeError:
        return {"raw": r.stdout.strip(), "error": r.stderr.strip()}


def find_agent_chrome():
    """Find the agent's Chrome (launched with --remote-debugging-port=19333)."""
    import psutil

    # Find the agent Chrome PID by its command line
    agent_pid = None
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = " ".join(proc.info["cmdline"] or [])
            if "remote-debugging-port=19333" in cmdline:
                agent_pid = proc.info["pid"]
                break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if not agent_pid:
        return None

    # Find the agent Chrome window — pick the largest one (content window, not toolbar)
    data = cua_call("list_windows", None)
    windows = data.get("windows", [])
    best = None
    best_area = 0
    for w in windows:
        if w.get("app_name") == "Google Chrome" and w.get("pid") == agent_pid:
            b = w.get("bounds", {})
            area = b.get("width", 0) * b.get("height", 0)
            if b.get("height", 0) > 100 and area > best_area:
                best = (agent_pid, w["window_id"])
                best_area = area
    return best


def take_screenshot(pid, wid):
    data = cua_call("get_window_state", {"pid": pid, "window_id": wid, "max_elements": 200})
    return data.get("screenshot_png_b64", ""), data.get("elements", [])


def ocr_screenshot(screenshot_b64):
    try:
        import pytesseract
        from PIL import Image

        img_data = base64.b64decode(screenshot_b64)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(img_data)
            tmp_path = f.name
        img = Image.open(tmp_path)
        text = pytesseract.image_to_string(img)
        Path(tmp_path).unlink(missing_ok=True)
        return text.strip()
    except Exception as e:
        logger.warning("OCR failed: %s", e)
        return ""


def click_element(pid, wid, element_index):
    # Must call get_window_state first to refresh element cache
    cua_call("get_window_state", {"pid": pid, "window_id": wid, "max_elements": 5})
    result = cua_call("click", {"pid": pid, "window_id": wid, "element_index": element_index})
    raw = str(result.get("raw", ""))
    return "Performed" in raw or raw == ""


def type_text(pid, wid, text):
    result = cua_call("type", {"pid": pid, "window_id": wid, "text": text})
    return "Performed" in str(result.get("raw", ""))


def press_key(pid, wid, key):
    result = cua_call("press_key", {"pid": pid, "window_id": wid, "key": key})
    return "Performed" in str(result.get("raw", ""))


# ── LLM via direct HTTP ──────────────────────────────────────────────

SYSTEM_PROMPT = """You are a REAL HUMAN taking a survey. Given page text and interactive elements, return a JSON array of actions.

Each action: {"element_index": N, "action": "click|fill", "value": ""}

Rules:
- Radio/checkbox: action=click, element_index=N
- Text/textarea: action=fill, element_index=N, value="answer"
- Profile: 39yo Black male, Austin TX, self-employed tech founder, trade/vocational degree, 5 person household, 3 kids, rent, $50-75K, former smoker, never drinks, exercises several times/week
- NEVER type "ROBOT" — it's a bot trap
- Answer naturally like a human
- For "select all": pick 2-4 options
- Return ONLY the JSON array, no explanation"""


async def get_llm_answer(page_text, elements_desc):
    import httpx

    api_key = "sk-iCTCNwKfnSBLmud9qeOzloYdRj5PQdNgof19IbkGljYRBXQfJMuDzkkJM7qIIhmh"
    user_msg = (
        f"PAGE TEXT:\n{page_text[:2000]}\n\nINTERACTIVE ELEMENTS:\n{elements_desc[:2000]}\n\nReturn the JSON array."
    )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://opencode.ai/zen/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "deepseek-v4-flash-free",
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_msg},
                    ],
                    "max_tokens": 4000,
                },
            )
            r.raise_for_status()
            resp = r.json()
            msg = resp["choices"][0]["message"]
            # Reasoning models put content in reasoning_content
            raw = msg.get("content") or msg.get("reasoning_content") or ""

        # Extract JSON array
        if "[" in raw:
            raw = raw[raw.index("[") : raw.rindex("]") + 1]

        answers = json.loads(raw)

        # Filter CAPTCHA fields
        filtered = []
        for a in answers:
            val = (a.get("value", "") or "").upper()
            if val == "ROBOT":
                continue
            if a.get("element_index") is not None:
                filtered.append(a)
        return filtered
    except Exception as e:
        logger.warning("LLM error: %s", e)
        return []


# ── Main agent loop ───────────────────────────────────────────────────


async def run_survey_cycle(pid, wid, profile=None):  # noqa: PLR0915,ARG001
    stats = {"pages": 0, "answers_applied": 0, "status": "unknown"}

    for page_num in range(40):
        screenshot, elements = take_screenshot(pid, wid)
        if not screenshot:
            logger.warning("No screenshot")
            stats["status"] = "chrome_closed"
            break

        # Build page description
        page_text_parts = []
        elements_desc_parts = []
        interactive_elements = []

        for el in elements:
            role = el.get("role", "")
            label = el.get("label", "")
            idx = el.get("element_index")

            if label:
                page_text_parts.append(label)

            if role in (
                "AXButton",
                "AXLink",
                "AXRadioButton",
                "AXCheckBox",
                "AXTextField",
                "AXTextArea",
                "AXPopUpButton",
            ):
                elements_desc_parts.append(f"[{idx}] {role}: {label}")
                interactive_elements.append(el)

        page_text = "\n".join(page_text_parts)
        elements_desc = "\n".join(elements_desc_parts)

        # OCR if sparse
        if len(page_text.strip()) < 200:
            ocr_text = ocr_screenshot(screenshot)
            if ocr_text and len(ocr_text) > len(page_text):
                page_text = ocr_text
                logger.info("OCR (%d chars): %s", len(ocr_text), ocr_text[:100])

        logger.info("Page %d: %d chars, %d elements", page_num + 1, len(page_text), len(interactive_elements))

        # If on dashboard, click a survey link
        page_lower = page_text.lower()
        if "dashboard" in page_lower or "my points" in page_lower:
            for el in interactive_elements:
                label = (el.get("label") or "").lower()
                if "take survey" in label or "take" in label:
                    idx = el.get("element_index")
                    if idx is not None:
                        # Use osascript to click the link (triggers actual navigation)
                        osascript_cmd = """
                        tell application "Google Chrome"
                            execute javascript "document.querySelectorAll('a[href*=/surveys/pre]')[0].click()" in front tab of front window
                        end tell
                        """
                        subprocess.run(["osascript", "-e", osascript_cmd], capture_output=True, timeout=10, check=False)
                        logger.info("Clicked survey link via osascript")
                        time.sleep(8)
                        # Re-read page
                        screenshot, elements = take_screenshot(pid, wid)
                        page_text_parts = []
                        elements_desc_parts = []
                        interactive_elements = []
                        for el2 in elements:
                            role = el2.get("role", "")
                            label2 = el2.get("label", "")
                            if label2:
                                page_text_parts.append(label2)
                            if role in (
                                "AXButton",
                                "AXLink",
                                "AXRadioButton",
                                "AXCheckBox",
                                "AXTextField",
                                "AXTextArea",
                                "AXPopUpButton",
                            ):
                                elements_desc_parts.append(f"[{el2.get('element_index')}] {role}: {label2}")
                                interactive_elements.append(el2)
                        page_text = "\n".join(page_text_parts)
                        elements_desc = "\n".join(elements_desc_parts)
                        text_lower = page_text.lower()
                        logger.info("After nav: %d chars, %d elements", len(page_text), len(interactive_elements))
                        break

        # Disqualification check
        text_lower = page_text.lower()
        if any(kw in text_lower for kw in ["not a match", "don't qualify", "disqualified", "screened out"]):
            logger.info("Disqualified")
            stats["status"] = "disqualified"
            break

        # Completion check
        if any(kw in text_lower for kw in ["thank you", "survey complete", "successfully completed"]):
            logger.info("Survey COMPLETED!")
            stats["status"] = "completed"
            break

        # Get LLM answers
        if not interactive_elements:
            press_key(pid, wid, "Return")
            time.sleep(2)
            continue

        answers = await get_llm_answer(page_text, elements_desc)
        logger.info("LLM: %d answers: %s", len(answers), json.dumps(answers)[:200])

        # Apply answers
        for ans in answers:
            idx = ans.get("element_index")
            action = ans.get("action", "click")
            value = ans.get("value", "")

            if action == "click":
                if click_element(pid, wid, idx):
                    stats["answers_applied"] += 1
                    logger.info("  clicked [%d]", idx)
            elif action == "fill" and click_element(pid, wid, idx):
                time.sleep(0.3)
                type_text(pid, wid, value)
                stats["answers_applied"] += 1
                logger.info("  filled [%d] = '%s'", idx, value[:30])
            time.sleep(0.5)

        # Click Next
        next_clicked = False
        for el in interactive_elements:
            label = (el.get("label") or "").lower()
            role = el.get("role", "")
            if role == "AXButton" and any(kw in label for kw in ["next", "continue", "submit", "done"]):
                idx = el.get("element_index")
                if idx is not None:
                    click_element(pid, wid, idx)
                    next_clicked = True
                    logger.info("  next: '%s'", label[:20])
                    break

        if not next_clicked:
            press_key(pid, wid, "Return")
            logger.info("  no next button, pressed Enter")

        stats["pages"] += 1
        time.sleep(3)

    return stats


async def main():
    logger.info("CUA Survey Agent starting")

    result = find_agent_chrome()
    if not result:
        logger.error("No Chrome window found")
        return

    pid, wid = result
    logger.info("Chrome: pid=%d wid=%d", pid, wid)

    stats = await run_survey_cycle(pid, wid)
    logger.info("Result: %s", stats)


if __name__ == "__main__":
    asyncio.run(main())
