"""
CUA Survey Agent v2 — drives the user's Chrome via cua-driver.

Uses:
- osascript for URL navigation
- cua-driver page tool for JS execution (reads page, clicks, fills forms)
- cua-driver screenshots + vision model for CAPTCHA reading
- OpenCode LLM for answer generation

Runs in a loop: 8-12 surveys per cycle, 15-45 min pause between cycles.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import subprocess
import tempfile
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cua_agent")

# ── Profile for answer generation ──────────────────────────────────────
PROFILE = {
    "age": 39,
    "gender": "Male",
    "race": "Black/African American",
    "relationship": "Domestic partnership",
    "city": "Austin",
    "state": "TX",
    "zip": "78736",
    "education": "Trade/vocational school",
    "employment": "Self-employed",
    "occupation": "CTO/founder of independent music label",
    "income": "$50,000 - $74,999",
    "household_size": 5,
    "children": 3,
    "children_ages": "4, 5, 5",
    "housing": "Rent",
    "smoking": "Former smoker",
    "drinking": "Never",
    "interests": "music production, streaming, film editing, gaming, fashion",
}


# ── Chrome interaction via cua-driver ──────────────────────────────────
def find_chrome():
    """Find the user's Chrome PID and best window ID (prefer survey windows)."""
    r = subprocess.run(
        ["cua-driver", "call", "list_windows", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    try:
        windows = json.loads(r.stdout.strip()).get("windows", [])
    except json.JSONDecodeError:
        return None, None

    # First pass: look for survey-related windows
    best_pid, best_wid, best_area = None, None, 0
    for w in windows:
        if w.get("app_name") == "Google Chrome":
            b = w.get("bounds", {})
            title = (w.get("title") or "").lower()
            area = b.get("width", 0) * b.get("height", 0)
            if (
                b.get("height", 0) > 100
                and area > best_area
                and any(kw in title for kw in ["survey", "branded", "purespectrum", "screener"])
            ):
                best_pid = w["pid"]
                best_wid = w["window_id"]
                best_area = area

    # Fallback: largest Chrome window
    if not best_pid:
        for w in windows:
            if w.get("app_name") == "Google Chrome":
                b = w.get("bounds", {})
                area = b.get("width", 0) * b.get("height", 0)
                if b.get("height", 0) > 100 and area > best_area:
                    best_pid = w["pid"]
                    best_wid = w["window_id"]
                    best_area = area
    return best_pid, best_wid


def js(pid, wid, expression):
    """Execute JavaScript in the user's Chrome via cua-driver page tool."""
    payload = json.dumps(
        {
            "action": "execute_javascript",
            "pid": pid,
            "window_id": wid,
            "bundle_id": "com.google.Chrome",
            "javascript": expression,
        }
    )
    r = subprocess.run(
        ["cua-driver", "call", "page", payload],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return r.stdout.strip()


def navigate(url):
    """Navigate Chrome to a URL via osascript."""
    safe_url = url.replace('"', '\\"')
    subprocess.run(
        [
            "osascript",
            "-e",
            f'tell application "Google Chrome" to set URL of active tab of front window to "{safe_url}"',
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def get_page_text(pid, wid):
    """Get visible text from the current page. Uses JS first, falls back to AX tree."""
    result = js(pid, wid, "document.body ? document.body.innerText.substring(0, 5000) : ''")
    if result not in ("missing value", "", "error: Unsupported browser: bundle_id="):
        return result

    # Fallback: use cua-driver AX tree
    r = subprocess.run(
        ["cua-driver", "call", "get_window_state", json.dumps({"pid": pid, "window_id": wid, "max_elements": 200})],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    try:
        data = json.loads(r.stdout.strip())
        texts = []
        for el in data.get("elements", []):
            label = el.get("label", "")
            if label and len(label) > 1:
                texts.append(label)
        return "\n".join(texts)
    except json.JSONDecodeError:
        return ""


def get_page_url(pid, wid):
    """Get current page URL."""
    result = js(pid, wid, "document.location.href")
    if result not in ("missing value", "", "error: Unsupported browser: bundle_id="):
        return result
    return ""


def get_page_title(pid, wid):
    """Get current page title."""
    result = js(pid, wid, "document.title")
    if result not in ("missing value", "", "error: Unsupported browser: bundle_id="):
        return result
    r = subprocess.run(
        ["cua-driver", "call", "list_windows", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    try:
        for w in json.loads(r.stdout.strip()).get("windows", []):
            if w.get("pid") == pid and w.get("window_id") == wid:
                return w.get("title", "")
    except json.JSONDecodeError:
        pass
    return ""


def set_radio(pid, wid, index):
    """Set a radio button as selected by index."""
    return js(
        pid,
        wid,
        f'(function(){{var r=document.querySelectorAll("input[type=radio]");'
        f"if(r[{index}]){{r[{index}].checked=true;"
        f'r[{index}].dispatchEvent(new Event("change",{{bubbles:true}}));}}return "ok"}})()',
    )


def set_checkbox(pid, wid, index):
    """Set a checkbox as checked by index."""
    return js(
        pid,
        wid,
        f'(function(){{var c=document.querySelectorAll("input[type=checkbox]");'
        f"if(c[{index}]){{c[{index}].checked=true;"
        f'c[{index}].dispatchEvent(new Event("change",{{bubbles:true}}));}}return "ok"}})()',
    )


def fill_text(pid, wid, selector, value):
    """Fill a text input by selector."""
    escaped = value.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"')
    return js(
        pid,
        wid,
        f'(function(){{var el=document.querySelector("{selector}");'
        f'if(el){{el.focus();el.value="{escaped}";'
        f'el.dispatchEvent(new Event("input",{{bubbles:true}}));'
        f'el.dispatchEvent(new Event("change",{{bubbles:true}}));}}return "ok"}})()',
    )


def submit_form(pid, wid):
    """Try multiple methods to submit/advance the page."""
    # Method 1: Click submit/next buttons
    result = js(
        pid,
        wid,
        '(function(){var btns=document.querySelectorAll("input[type=submit], button[type=submit], '
        'button.next_btn, .next-btn, [id*=next], [id*=submit], [class*=next], [class*=submit]");'
        'for(var i=0;i<btns.length;i++){if(btns[i].offsetParent!==null){btns[i].click();return "clicked:"+btns[i].tagName;}}'
        'return "no_btn"})()',
    )
    if "clicked" in str(result):
        return result

    # Method 2: Try form.submit()
    result2 = js(
        pid,
        wid,
        '(function(){var forms=document.querySelectorAll("form");'
        'for(var i=0;i<forms.length;i++){if(forms[i].offsetParent!==null){forms[i].submit();return "form_submitted";}}'
        'return "no_form"})()',
    )
    if "submitted" in str(result2):
        return result2

    return "no_submit_method"


def count_inputs(pid, wid):
    """Count radio, checkbox, text, and select inputs on the page."""
    radios = js(pid, wid, 'document.querySelectorAll("input[type=radio]").length')
    checks = js(pid, wid, 'document.querySelectorAll("input[type=checkbox]").length')
    texts = js(
        pid,
        wid,
        'document.querySelectorAll("input[type=text], textarea, input[type=email], input[type=number]").length',
    )
    selects = js(pid, wid, 'document.querySelectorAll("select").length')

    def safe_int(v):
        try:
            return int(v) if v.strip().isdigit() else 0
        except (ValueError, AttributeError):
            return 0

    return {
        "radio": safe_int(radios),
        "checkbox": safe_int(checks),
        "text": safe_int(texts),
        "select": safe_int(selects),
    }


def get_input_labels(pid, wid):
    """Get labels for all inputs on the page."""
    result = js(
        pid,
        wid,
        'Array.from(document.querySelectorAll("label")).map(function(l){'
        'var input=l.querySelector("input")||document.getElementById(l.htmlFor);'
        'var type=input?type:"unknown";'
        'return type+":"+l.textContent.trim().substring(0,60)}).join("|")',
    )
    if result in ("missing value", ""):
        return []
    return result.split("|")


def find_survey_window(original_pid, original_wid):
    """Find a new Chrome window that opened after clicking a survey link."""
    r = subprocess.run(
        ["cua-driver", "call", "list_windows", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    try:
        windows = json.loads(r.stdout.strip()).get("windows", [])
    except json.JSONDecodeError:
        return None, None

    for w in windows:
        if w.get("app_name") == "Google Chrome":
            b = w.get("bounds", {})
            title = w.get("title", "")
            pid = w.get("pid")
            wid = w.get("window_id")
            if pid == original_pid and wid == original_wid:
                continue
            if b.get("height", 0) > 100 and any(
                kw in title.lower() for kw in ["survey", "purespectrum", "screener", "branded"]
            ):
                return pid, wid
    return None, None


def find_any_new_window(original_pid, original_wid):
    """Find ANY new Chrome window (not just survey-titled ones)."""
    r = subprocess.run(
        ["cua-driver", "call", "list_windows", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    try:
        windows = json.loads(r.stdout.strip()).get("windows", [])
    except json.JSONDecodeError:
        return None, None

    best_pid, best_wid, best_area = None, None, 0
    for w in windows:
        if w.get("app_name") == "Google Chrome":
            b = w.get("bounds", {})
            pid = w.get("pid")
            wid = w.get("window_id")
            if pid == original_pid and wid == original_wid:
                continue
            area = b.get("width", 0) * b.get("height", 0)
            if b.get("height", 0) > 100 and area > best_area:
                best_pid = pid
                best_wid = wid
                best_area = area
    return best_pid, best_wid


def dismiss_popups(pid, wid):
    """Dismiss notification/permission popups."""
    js(
        pid,
        wid,
        '(function(){var btns=document.querySelectorAll("button");'
        "for(var i=0;i<btns.length;i++){"
        "var t=btns[i].textContent.trim().toLowerCase();"
        'if(t==="block"||t==="dismiss"||t==="not now"||t==="no thanks"){btns[i].click();}}'
        'return "done"})()',
    )


# ── LLM answer generation ─────────────────────────────────────────────
def get_llm_answer(question_text, input_labels, profile_str):
    """Ask the LLM to answer a survey question."""
    import httpx  # noqa: PLC0415

    api_key = ""
    try:
        with open(Path.home() / ".zshrc") as f:
            for line in f:
                if "OPENCODE_API_KEY" in line and "=" in line:
                    api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    except FileNotFoundError:
        pass

    if not api_key:
        api_key = os.environ.get("OPENCODE_API_KEY", "")
    if not api_key:
        return []

    # Build prompt without f-strings to avoid curly brace issues
    prompt = (
        "You are answering a survey question as this person:\n"
        + profile_str
        + "\n\nQUESTION:\n"
        + question_text[:1000]
        + "\n\nAVAILABLE OPTIONS (type:label):\n"
        + chr(10).join(input_labels[:30])
        + "\n\nReturn a JSON array of actions. Each action has:\n"
        '- "index": the option index (0-based, counting from the first option of its type)\n'
        '- "action": "select" for radio/checkbox, "fill" for text\n'
        '- "value": for "fill" actions, the text to type\n'
        "\nRules:\n"
        "- IMPORTANT: There may be MULTIPLE questions on this page, each with its own radio group.\n"
        "- You MUST return one answer for EACH question/radio group on the page.\n"
        "- For single-choice (radio): select ONE option per question\n"
        "- For multi-choice (checkbox): select 1-3 options that fit the profile\n"
        "- For text: provide a realistic short answer\n"
        "- Be consistent with the profile demographics\n"
        "- Answer as a real person would, not as an AI\n"
        "- NEVER type the word ROBOT — it is a bot trap\n"
        "- Return ONLY the JSON array, nothing else\n"
        '\nExample for 3 radio groups: [{"index": 0, "action": "select"}, {"index": 3, "action": "select"}, {"index": 6, "action": "select"}]'
        '\nExample for text: [{"index": 0, "action": "fill", "value": "Austin, TX"}]'
    )

    try:
        with httpx.Client(timeout=60) as client:
            r = client.post(
                "https://opencode.ai/zen/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "deepseek-v4-flash-free",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 4000,
                },
            )
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            content = msg.get("content") or msg.get("reasoning_content") or ""
            # Extract JSON array
            match = re.search(r"\[.*\]", content, re.DOTALL)
            if match:
                answers = json.loads(match.group())
                # Filter ROBOT traps
                filtered = []
                for a in answers:
                    val = (a.get("value") or "").strip().upper()
                    if val == "ROBOT":
                        continue
                    filtered.append(a)
                return filtered
    except Exception as e:
        logger.warning("LLM error: %s", e)
    return []


# ── CAPTCHA handling ───────────────────────────────────────────────────
def read_captcha_vision(image_path):
    """Read CAPTCHA text using FreeModel Claude Haiku vision."""
    import base64  # noqa: PLC0415

    key = ""
    try:
        with open(Path.home() / ".zshrc") as f:
            for line in f:
                if "FREEMODEL_API_KEY" in line and "=" in line:
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    except FileNotFoundError:
        pass

    if not key:
        return ""

    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    payload = json.dumps(
        {
            "model": "claude-haiku-4-5-20251001",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "What text is shown in this image? Read the characters exactly as they appear, preserving case. Return only the characters.",
                        },
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                    ],
                }
            ],
            "max_tokens": 50,
        }
    )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write(payload)
        p = f.name

    try:
        r = subprocess.run(
            [
                "curl",
                "-s",
                "https://api.freemodel.dev/v1/chat/completions",
                "-H",
                "Authorization: Bearer " + key,
                "-H",
                "Content-Type: application/json",
                "-d",
                f"@{p}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if r.stdout:
            resp = json.loads(r.stdout)
            content = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            content = re.sub(r"[^A-Za-z0-9]", "", content)
            if content and 2 <= len(content) <= 20:
                return content
    except Exception as e:
        logger.debug("Vision CAPTCHA failed: %s", e)
    finally:
        Path(p).unlink(missing_ok=True)
    return ""


def screenshot_chrome(pid, wid):
    """Take a screenshot of the Chrome window via cua-driver."""
    r = subprocess.run(
        ["cua-driver", "call", "get_window_state", json.dumps({"pid": pid, "window_id": wid, "max_elements": 5})],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    try:
        data = json.loads(r.stdout.strip())
        img_b64 = data.get("screenshot_png_b64", "")
        if img_b64:
            import base64  # noqa: PLC0415

            img_data = base64.b64decode(img_b64)
            path = "/tmp/cua_screenshot.png"
            with open(path, "wb") as f:
                f.write(img_data)
            return path
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def handle_captcha(pid, wid):
    """Detect and solve CAPTCHAs on the current page."""
    text = get_page_text(pid, wid)
    text_lower = text.lower()

    if "type the following code" in text_lower or "type the characters" in text_lower:
        logger.info("Text CAPTCHA detected")
        screenshot_path = screenshot_chrome(pid, wid)
        if screenshot_path:
            captcha_text = read_captcha_vision(screenshot_path)
            if captcha_text:
                logger.info("CAPTCHA read: '%s'", captcha_text)
                fill_text(pid, wid, "input[type=text]", captcha_text)
                time.sleep(0.5)
                submit_form(pid, wid)
                time.sleep(2)
                return True
            else:
                logger.warning("Could not read CAPTCHA")
                return False

    if "drag" in text_lower and "drop" in text_lower:
        logger.info("Drag-drop CAPTCHA — skipping")
        return False

    return False


def check_balance_now(pid, wid):
    """Check balance without navigating away — use AX tree on current page."""
    # Try reading balance from current page
    text = get_page_text(pid, wid)
    match = re.search(r"(\d+)\s*(?:PTS|POINTS|points)", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def check_balance_via_nav():
    """Check balance by navigating to my-points page. Returns points or None."""
    pid, wid = find_chrome()
    if not pid:
        return None
    navigate("https://surveys.gobranded.com/my-points")
    time.sleep(8)
    text = get_page_text(pid, wid)
    match = re.search(r"(\d+)\s*POINTS?\s*UNTIL\s*PAYOUT", text, re.IGNORECASE)
    if match:
        return 500 - int(match.group(1))
    match = re.search(r"(\d+)/500", text)
    if match:
        return int(match.group(1))
    return None


# ── Main survey loop ──────────────────────────────────────────────────
def run_one_survey(pid, wid):  # noqa: PLR0915
    """Attempt to complete one survey. Returns (completed, points_earned)."""
    profile_str = "\n".join(f"{k}: {v}" for k, v in PROFILE.items())
    _dashboard_pid, _dashboard_wid = pid, wid

    # Navigate to dashboard and wait for it to fully load
    navigate("https://surveys.gobranded.com/users/dashboard")
    time.sleep(12)

    # Find survey URLs via JS (AX tree clicks don't trigger navigation)
    survey_urls = []
    for attempt in range(3):
        page_text = get_page_text(pid, wid)
        if "sign in" in page_text.lower() or "log in" in page_text.lower():
            logger.error("Not logged in")
            return False, 0

        raw_urls = js(
            pid,
            wid,
            'Array.from(document.querySelectorAll("a")).filter(function(a){'
            'return a.href&&a.href.includes("/surveys/")&&!a.href.includes("/listing")'
            '}).map(function(a){return a.href}).join("\\n")',
        )
        if raw_urls and raw_urls not in ("missing value", ""):
            survey_urls = [u.strip() for u in raw_urls.split(chr(10)) if u.strip() and "/listing" not in u]
        if survey_urls:
            break
        logger.info("  Attempt %d: no survey URLs, waiting...", attempt + 1)
        time.sleep(5)

    if not survey_urls:
        logger.warning("No survey URLs found on dashboard")
        return False, 0

    # Navigate directly to the survey pre-page (clicks don't work on this site)
    survey_url = survey_urls[0]
    logger.info("Navigating to survey: %s", survey_url[:80])
    navigate(survey_url)
    time.sleep(8)

    # On the pre-page, find the actual survey URL and navigate directly
    page_text = get_page_text(pid, wid)
    if "take survey" in page_text.lower() or "start your survey" in page_text.lower():
        logger.info("Pre-survey page loaded, finding survey URL")
        take_href = js(
            pid,
            wid,
            '(function(){var a=document.getElementById("btn-continue");'
            "if(a&&a.href)return a.href;"
            'var links=document.querySelectorAll("a[href*=take]");'
            "for(var i=0;i<links.length;i++){"
            'if(links[i].textContent.trim().toUpperCase().includes("TAKE SURVEY"))return links[i].href;}'
            'return "none"})()',
        )
        if take_href and take_href not in ("none", "missing value", ""):
            full_url = take_href if take_href.startswith("http") else "https://surveys.gobranded.com" + take_href
            logger.info("Navigating to actual survey: %s", full_url[:80])
            navigate(full_url)
            time.sleep(10)
        else:
            logger.warning("Could not find TAKE SURVEY href")
    else:
        logger.info("No pre-survey page, survey may have started directly")

    # Answer questions loop
    consecutive_empty = 0
    last_url = ""
    last_content_hash = ""
    same_page_count = 0
    for page_num in range(40):
        # Dismiss any popups
        dismiss_popups(pid, wid)
        time.sleep(0.5)

        # Get current page info
        page_text = get_page_text(pid, wid)
        page_url = get_page_url(pid, wid)
        page_title = get_page_title(pid, wid)

        if not page_text or len(page_text.strip()) < 10:
            consecutive_empty += 1
            if consecutive_empty >= 5:
                logger.warning("Page empty 5 times — giving up")
                break
            logger.info("  Page empty, waiting... (%d/5)", consecutive_empty)
            time.sleep(5)
            continue
        consecutive_empty = 0

        # Stuck detection: same URL and similar content for 3+ iterations
        import hashlib

        content_hash = hashlib.md5(page_text.encode()).hexdigest()[:12]
        if page_url == last_url and content_hash == last_content_hash:
            same_page_count += 1
            if same_page_count >= 3:
                logger.warning("Stuck on same page for 3 iterations — giving up")
                break
        else:
            same_page_count = 0
        last_url = page_url
        last_content_hash = content_hash

        logger.info("[Page %d] %s | %d chars | %s", page_num + 1, page_title[:40], len(page_text), page_url[:60])

        # Check for completion
        if any(
            kw in page_text.lower()
            for kw in ["thank you", "survey complete", "you have completed", "congratulations", "you earned"]
        ):
            logger.info("SURVEY COMPLETED!")
            return True, 0

        # Check for disqualification
        if any(
            kw in page_text.lower()
            for kw in ["not qualified", "disqualified", "do not qualify", "survey not a match", "not a match"]
        ):
            logger.info("Disqualified from survey")
            return False, 0

        # Check if we're on a non-survey page (dashboard, members, etc.)
        is_survey_url = any(
            kw in page_url.lower()
            for kw in [
                "/surveys/",
                "/survey/",
                "question",
                "prescreener",
                "screener",
                "samplicio",
                "insights-today",
                "yoursurveynow",
            ]
        )
        if not is_survey_url and ("dashboard" in page_url.lower() or "/members" in page_url.lower()):
            if page_num == 0:
                # Page 1 is already the dashboard — survey never started
                logger.info("Page 1 is non-survey URL: %s", page_url[:60])
                return False, 0
            else:
                logger.info("Redirected to dashboard — survey done")
                return True, 0

        # Handle CAPTCHA
        if handle_captcha(pid, wid):
            continue

        # Count inputs
        inputs = count_inputs(pid, wid)
        total = sum(inputs.values())

        if total == 0:
            # No inputs — might be a ranking or info page, try submit
            submit_form(pid, wid)
            time.sleep(3)
            continue

        # Get input labels for LLM
        labels = get_input_labels(pid, wid)

        # Ask LLM for answers
        answers = get_llm_answer(page_text, labels, profile_str)
        logger.info("  LLM: %d answers for %d inputs", len(answers), total)

        if answers:
            for ans in answers:
                idx = ans.get("index", 0)
                action = ans.get("action", "select")
                if action == "select":
                    if inputs["radio"] > 0:
                        set_radio(pid, wid, idx)
                    elif inputs["checkbox"] > 0:
                        set_checkbox(pid, wid, idx)
                elif action == "fill":
                    value = ans.get("value", "")
                    fill_text(pid, wid, "input[type=text], textarea", value)
        # Fallback: select first radio or checkbox
        elif inputs["radio"] > 0:
            set_radio(pid, wid, 0)
        elif inputs["checkbox"] > 0:
            set_checkbox(pid, wid, 0)

        time.sleep(0.5)

        # Submit
        submit_form(pid, wid)
        time.sleep(3)

        time.sleep(2)

    return False, 0


def main():
    """Main loop — run 8-12 surveys per cycle, 15-45 min pause between cycles."""
    logger.info("CUA Survey Agent v2 starting")

    pid, wid = find_chrome()
    if not pid:
        logger.error("No Chrome window found. Please open Chrome.")
        return

    logger.info("Found Chrome: pid=%d wid=%d", pid, wid)

    initial_balance = check_balance_via_nav()
    logger.info("Initial balance: %s", initial_balance)

    cycle = 0
    while True:
        cycle += 1
        surveys_this_cycle = random.randint(8, 12)
        logger.info("\n=== Cycle %d: %d surveys ===", cycle, surveys_this_cycle)

        completed_count = 0
        for survey_num in range(surveys_this_cycle):
            logger.info("\n--- Survey %d/%d ---", survey_num + 1, surveys_this_cycle)

            try:
                completed, _ = run_one_survey(pid, wid)
                if completed:
                    completed_count += 1
                    logger.info("Survey %d completed! (%d total this cycle)", survey_num + 1, completed_count)
                else:
                    logger.info("Survey %d not completed", survey_num + 1)
            except Exception as e:
                logger.error("Survey %d error: %s", survey_num + 1, e)

            # Check balance periodically
            new_balance = check_balance_via_nav()
            if new_balance and initial_balance and new_balance > initial_balance:
                earned = new_balance - initial_balance
                logger.info("*** EARNED %d POINTS! Balance: %d -> %d ***", earned, initial_balance, new_balance)
                initial_balance = new_balance

            # Pause between surveys (3-8 seconds)
            pause = random.randint(3, 8)
            logger.info("Pausing %d seconds...", pause)
            time.sleep(pause)

        # Pause between cycles (15-45 minutes)
        pause_min = random.randint(15, 45)
        logger.info(
            "\n=== Cycle %d done: %d/%d completed. Pausing %d minutes... ===",
            cycle,
            completed_count,
            surveys_this_cycle,
            pause_min,
        )
        time.sleep(pause_min * 60)


if __name__ == "__main__":
    main()
