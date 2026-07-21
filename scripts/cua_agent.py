#!/usr/bin/env python3
"""CUA-based survey agent — drives Chrome at OS level via cua-driver.

Reads the accessibility tree, clicks elements, types text, handles surveys
like a human would. No CDP, no string escaping, no iframe issues.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── cua-driver wrapper ──────────────────────────────────────────────────


def cua_call(tool: str, args: dict | None = None, timeout: int = 30) -> dict:
    """Call a cua-driver tool and return parsed JSON."""
    cmd = ["cua-driver", "call", tool]
    if args:
        cmd.append(json.dumps(args))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        raw = result.stdout.strip()
        if not raw:
            return {"error": result.stderr.strip() or "empty response"}
        # Parse JSON from output
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}


def get_chrome_pid() -> int | None:
    """Get the PID of the running Chrome process."""
    result = subprocess.run(["pgrep", "-f", "Google Chrome"], capture_output=True, text=True, check=False)
    if result.returncode == 0:
        pids = result.stdout.strip().split("\n")
        return int(pids[0]) if pids else None
    return None


def get_chrome_window_id(pid: int) -> int | None:  # noqa: ARG001
    """Get the main Chrome window ID (works with minimized windows on macOS)."""
    data = cua_call("list_windows")
    content = data.get("content", [{}])
    if content:
        text = content[0].get("text", "") if isinstance(content[0], dict) else str(content[0])
        try:
            windows = json.loads(text) if isinstance(text, str) else text
            if isinstance(windows, dict):
                windows = windows.get("windows", [])
        except (json.JSONDecodeError, AttributeError):
            windows = []

        # Find largest Chrome window (even if minimized — cua-driver works on macOS bg windows)
        best = None
        for w in windows:
            if isinstance(w, dict) and w.get("app_name") == "Google Chrome":
                bounds = w.get("bounds", {})
                area = bounds.get("width", 0) * bounds.get("height", 0)
                if area > 5000 and (best is None or area > best[1]):  # Main content window
                    best = (w.get("window_id"), area)
        if best:
            return best[0]
    return None


def get_page_content(pid: int, window_id: int) -> dict:
    """Get the page content from Chrome's accessibility tree."""
    data = cua_call("get_window_state", {"pid": pid, "window_id": window_id, "max_elements": 200})
    elements = data.get("elements", [])
    tree_md = data.get("tree_markdown", "")

    # Extract interactive elements
    interactive = []
    for el in elements:
        role = el.get("role", "")
        label = el.get("label", "")
        idx = el.get("element_index")
        frame = el.get("frame", {})
        if (
            role
            in (
                "AXButton",
                "AXTextField",
                "AXTextArea",
                "AXRadioButton",
                "AXCheckBox",
                "AXPopUpButton",
                "AXLink",
                "AXStaticText",
                "AXGroup",
            )
            and label
        ):
            interactive.append(
                {
                    "index": idx,
                    "role": role,
                    "label": label[:200],
                    "x": frame.get("x", 0) + frame.get("w", 0) / 2,
                    "y": frame.get("y", 0) + frame.get("h", 0) / 2,
                    "w": frame.get("w", 0),
                    "h": frame.get("h", 0),
                }
            )

    # Extract text content from AXStaticText elements
    texts = []
    for el in elements:
        if el.get("role") == "AXStaticText" and el.get("label"):
            texts.append(el["label"])

    return {
        "elements": interactive,
        "text": " ".join(texts)[:5000],
        "tree": tree_md[:3000],
        "element_count": len(elements),
    }


def click_element(pid: int, window_id: int, element_index: int) -> bool:
    """Click an element by its index."""
    result = cua_call("click", {"pid": pid, "window_id": window_id, "element_index": element_index})
    return "error" not in result


def type_text(text: str, pid: int | None = None) -> bool:
    """Type text."""
    args: dict = {"text": text}
    if pid:
        args["pid"] = pid
    result = cua_call("type_text", args)
    return "error" not in result


def press_key(key: str, pid: int | None = None) -> bool:
    """Press a key."""
    args: dict = {"key": key}
    if pid:
        args["pid"] = pid
    result = cua_call("press_key", args)
    return "error" not in result


def navigate_to(url: str, pid: int, window_id: int) -> bool:  # noqa: ARG001
    """Navigate to a URL by typing it in the address bar."""
    # Focus address bar
    press_key("cmd+l", pid)
    time.sleep(0.5)
    type_text(url, pid)
    time.sleep(0.3)
    press_key("return", pid)
    time.sleep(3)
    return True


# ── Survey profile ──────────────────────────────────────────────────────

PROFILE = {
    "age": "39",
    "gender": "male",
    "income": "50000-74999",
    "education": "bachelor",
    "employment": "full-time",
    "race": "black",
    "ethnicity": "hispanic",
    "household": "2",
    "state": "california",
    "zip": "90210",
}


# ── Main survey loop ────────────────────────────────────────────────────


def find_and_click(
    pid: int, window_id: int, elements: list, search_terms: list[str], role_filter: str | None = None
) -> bool:
    """Find an element matching search terms and click it."""
    for el in elements:
        label = el.get("label", "").lower()
        role = el.get("role", "")
        if role_filter and role != role_filter:
            continue
        for term in search_terms:
            if term.lower() in label:
                logger.info("Clicking element %d: '%s' (role=%s)", el["index"], el["label"][:50], role)
                return click_element(pid, window_id, el["index"])
    return False


def run_survey_cycle(pid: int, window_id: int) -> dict:  # noqa: PLR0915
    """Run one survey attempt. Returns stats."""
    stats = {"pages": 0, "answers": 0, "status": "unknown"}

    for page in range(30):
        time.sleep(3)
        content = get_page_content(pid, window_id)
        text = content["text"].lower()
        elements = content["elements"]
        stats["pages"] = page + 1

        logger.info("Page %d: %d elements, text='%s...'", page + 1, len(elements), text[:100])

        # Check for completion
        if any(kw in text for kw in ["thank you", "survey complete", "congratulations", "you've earned"]):
            stats["status"] = "completed"
            logger.info("SURVEY COMPLETED!")
            return stats

        # Check for disqualification
        if any(kw in text for kw in ["not a match", "disqualified", "screened out", "don't qualify"]):
            stats["status"] = "disqualified"
            logger.info("Disqualified from survey")
            return stats

        # Check for CAPTCHA
        if any(kw in text for kw in ["captcha", "robot", "verification code"]):
            logger.info("CAPTCHA detected — need to solve")
            # TODO: OCR the CAPTCHA image
            stats["status"] = "captcha_blocked"
            return stats

        # Find and click answer options
        answered = False

        # Look for radio buttons / checkboxes
        radio_elements = [e for e in elements if e["role"] in ("AXRadioButton", "AXCheckBox")]
        if radio_elements:
            # Pick the first unselected option (simple heuristic)
            for el in radio_elements:
                label = el["label"].lower()
                # Match profile
                if (
                    "male" in label
                    and PROFILE["gender"] == "male"
                    or "35" in label
                    or "39" in label
                    or "25-44" in label
                    or "bachelor" in label
                    or "college" in label
                    or "full-time" in label
                    or "employed" in label
                    or "black" in label
                    or "african" in label
                    or "hispanic" in label
                    or "latino" in label
                ):
                    click_element(pid, window_id, el["index"])
                    answered = True
                    break

            # If nothing matched, click the first option
            if not answered and radio_elements:
                click_element(pid, window_id, radio_elements[0]["index"])
                answered = True

        # Look for text fields
        if not answered:
            text_fields = [e for e in elements if e["role"] in ("AXTextField", "AXTextArea")]
            if text_fields:
                click_element(pid, window_id, text_fields[0]["index"])
                time.sleep(0.3)
                type_text("I enjoy sharing my opinions and helping improve products.", pid)
                answered = True

        # Look for dropdowns
        if not answered:
            dropdowns = [e for e in elements if e["role"] == "AXPopUpButton"]
            if dropdowns:
                click_element(pid, window_id, dropdowns[0]["index"])
                time.sleep(0.5)
                # Click first menu item
                content2 = get_page_content(pid, window_id)
                menu_items = [e for e in content2["elements"] if e["role"] in ("AXMenuItem", "AXStaticText")]
                if menu_items:
                    click_element(pid, window_id, menu_items[0]["index"])
                answered = True

        if answered:
            stats["answers"] += 1
            logger.info("Answered on page %d", page + 1)

        # Click Next/Continue/Submit
        time.sleep(1)
        next_found = find_and_click(pid, window_id, elements, ["next", "continue", "submit", "proceed", "done"])
        if not next_found:
            # Try pressing Enter
            press_key("return", pid)
            logger.info("Pressed Enter (no next button found)")

        time.sleep(2)

    stats["status"] = "max_pages_reached"
    return stats


def main() -> None:  # noqa: PLR0915
    """Main entry point."""
    logger.info("=" * 60)
    logger.info("CUA Survey Agent — starting")
    logger.info("=" * 60)

    # Find Chrome
    pid = get_chrome_pid()
    if not pid:
        logger.error("Chrome not running. Please start Chrome first.")
        sys.exit(1)
    logger.info("Chrome PID: %d", pid)

    # Get window
    window_id = get_chrome_window_id(pid)
    if not window_id:
        logger.error("No visible Chrome window found.")
        sys.exit(1)
    logger.info("Chrome window ID: %d", window_id)

    # Navigate to dashboard
    logger.info("Navigating to Branded Surveys dashboard...")
    navigate_to("https://surveys.gobranded.com/users/dashboard", pid, window_id)
    time.sleep(5)

    # Check if logged in
    content = get_page_content(pid, window_id)
    if "log in" in content["text"].lower() or "sign in" in content["text"].lower():
        logger.info("Not logged in — navigating to login page")
        navigate_to("https://surveys.gobranded.com/users/sign_in", pid, window_id)
        time.sleep(3)
        # TODO: handle login
        logger.error("Login not implemented yet. Please log in manually.")
        sys.exit(1)

    logger.info("Logged in. Starting survey loop...")

    # Survey loop
    total_surveys = 0
    total_pages = 0

    for survey_num in range(10):
        logger.info("\n--- Survey %d ---", survey_num + 1)

        # Get dashboard content
        navigate_to("https://surveys.gobranded.com/users/dashboard", pid, window_id)
        time.sleep(3)
        content = get_page_content(pid, window_id)

        # Find "Take Survey" link
        survey_link = None
        for el in content["elements"]:
            if "take survey" in el.get("label", "").lower():
                survey_link = el
                break

        if not survey_link:
            logger.info("No surveys available. Waiting...")
            time.sleep(60)
            continue

        # Click survey link
        logger.info("Clicking survey: '%s'", survey_link["label"][:50])
        click_element(pid, window_id, survey_link["index"])
        time.sleep(5)

        # Handle pre-survey page
        content = get_page_content(pid, window_id)
        if "take survey" in content["text"].lower():
            find_and_click(pid, window_id, content["elements"], ["take survey"])
            time.sleep(5)

        # Run the survey
        stats = run_survey_cycle(pid, window_id)
        total_surveys += 1
        total_pages += stats["pages"]

        logger.info(
            "Survey %d result: %s (%d pages, %d answers)",
            survey_num + 1,
            stats["status"],
            stats["pages"],
            stats["answers"],
        )

        # Pause between surveys
        pause = 30 + (survey_num * 10)
        logger.info("Pausing %d seconds before next survey...", pause)
        time.sleep(pause)

    logger.info("\nTotal: %d surveys, %d pages", total_surveys, total_pages)


if __name__ == "__main__":
    main()
