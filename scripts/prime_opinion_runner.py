#!/usr/bin/env python3
"""
Prime Opinion survey runner — uses cua-driver to interact with
the user's Chrome that's already logged in to primopinion.com.

Runs in parallel with the Branded Surveys harness_backend.
"""

import base64
import json
import logging
import subprocess
import tempfile
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("prime_opinion")

PROFILE = "39yo Black male, Austin TX, self-employed tech founder, trade/vocational degree, 5 person household, 3 kids, rent, $50-75K"


def cua(tool, args=None, timeout=30):
    cmd = ["cua-driver", "call", tool]
    if args:
        cmd.append(json.dumps(args))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    try:
        return json.loads(r.stdout.strip())
    except json.JSONDecodeError:
        return {"raw": r.stdout.strip(), "error": r.stderr.strip()}


def find_prime_opinion_chrome():
    """Find the user's Chrome window with Prime Opinion."""
    data = cua("list_windows")
    for w in data.get("windows", []):
        if w.get("app_name") == "Google Chrome":
            title = w.get("title", "")
            b = w.get("bounds", {})
            if "prime opinion" in title.lower() and b.get("height", 0) > 100:
                area = b.get("width", 0) * b.get("height", 0)
                return w.get("pid"), w.get("window_id"), area
    # Fallback: any large Chrome window
    best = None
    best_area = 0
    for w in data.get("windows", []):
        if w.get("app_name") == "Google Chrome":
            b = w.get("bounds", {})
            area = b.get("width", 0) * b.get("height", 0)
            if b.get("height", 0) > 100 and area > best_area:
                best = (w.get("pid"), w.get("window_id"), area)
                best_area = area
    return best


def get_page_elements(pid, wid, max_elements=200):
    """Get page elements from cua-driver."""
    data = cua("get_window_state", {"pid": pid, "window_id": wid, "max_elements": max_elements})
    return data.get("screenshot_png_b64", ""), data.get("elements", [])


def click_element(pid, wid, element_index):
    """Click an element by index."""
    cua("get_window_state", {"pid": pid, "window_id": wid, "max_elements": 5})
    result = cua("click", {"pid": pid, "window_id": wid, "element_index": element_index})
    return "Posted" in str(result) or "Performed" in str(result)


def click_coord(pid, wid, x, y):
    """Click at coordinates."""
    result = cua("click", {"pid": pid, "window_id": wid, "x": x, "y": y})
    return "Posted" in str(result)


def ocr_screenshot(screenshot_b64):
    """OCR a screenshot using Tesseract."""
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


def navigate_to_surveys(pid, wid):
    """Navigate to the Surveys tab."""
    # Click on "Surveys" in the nav
    screenshot, elements = get_page_elements(pid, wid)
    for el in elements:
        label = (el.get("label") or "").lower()
        role = el.get("role", "")
        if role == "AXStaticText" and label == "surveys":
            # Get the position and click
            frame = el.get("frame", {})
            if frame:
                x = int(frame.get("x", 0) + frame.get("width", 0) / 2)
                y = int(frame.get("y", 0) + frame.get("height", 0) / 2)
                click_coord(pid, wid, x, y)
                logger.info("Clicked Surveys tab at (%d, %d)", x, y)
                time.sleep(3)
                return True
    logger.warning("Could not find Surveys tab")
    return False


def find_survey_cards(pid, wid):
    """Find available survey cards on the page."""
    screenshot, elements = get_page_elements(pid, wid)
    surveys = []

    # Look for survey cards with dollar amounts
    for i, el in enumerate(elements):
        label = el.get("label", "")
        role = el.get("role", "")
        if role == "AXStaticText" and "$" in label and any(c.isdigit() for c in label):
            # This might be a survey payout
            # Look for nearby elements with survey title
            title = ""
            for j in range(max(0, i - 5), min(len(elements), i + 5)):
                nearby = elements[j]
                nearby_label = nearby.get("label", "")
                nearby_role = nearby.get("role", "")
                if nearby_role == "AXStaticText" and len(nearby_label) > 10 and "$" not in nearby_label:
                    title = nearby_label
                    break

            surveys.append(
                {
                    "payout_text": label,
                    "title": title[:80],
                    "element_index": el.get("element_index"),
                }
            )

    logger.info("Found %d survey cards", len(surveys))
    return surveys


def run_one_survey(pid, wid):
    """Attempt to complete one survey. Returns True if completed."""
    # Navigate to surveys
    navigate_to_surveys(pid, wid)
    time.sleep(2)

    # Find survey cards
    surveys = find_survey_cards(pid, wid)
    if not surveys:
        logger.warning("No surveys found")
        return False

    # Pick the highest-paying survey
    best = max(surveys, key=lambda s: _parse_payout(s["payout_text"]))
    logger.info("Attempting: %s (%s)", best["title"][:40], best["payout_text"])

    # Click the survey
    idx = best.get("element_index")
    if idx:
        click_element(pid, wid, idx)
    time.sleep(5)

    # Read the new page
    screenshot, elements = get_page_elements(pid, wid)
    page_text = ocr_screenshot(screenshot) if screenshot else ""
    logger.info("Survey page: %d chars, %d elements", len(page_text), len(elements))

    # TODO: Answer questions using LLM + element interaction
    # For now, just report what we see
    return False


def _parse_payout(text):
    """Parse dollar amount from text like '$1.60' or '94¢'."""
    import re

    m = re.search(r"\$(\d+\.?\d*)", text)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+)¢", text)
    if m:
        return int(m.group(1)) / 100
    return 0


def main():
    """Main loop — run Prime Opinion surveys."""
    logger.info("Prime Opinion survey runner starting")

    result = find_prime_opinion_chrome()
    if not result:
        logger.error("No Chrome window found with Prime Opinion")
        return

    pid, wid, area = result
    logger.info("Found Prime Opinion Chrome: pid=%d wid=%d area=%d", pid, wid, area)

    while True:
        try:
            completed = run_one_survey(pid, wid)
            if completed:
                logger.info("Survey completed!")
            else:
                logger.info("Survey not completed, trying next")
        except Exception as e:
            logger.error("Error: %s", e)

        time.sleep(10)


if __name__ == "__main__":
    main()
