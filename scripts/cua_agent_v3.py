"""
CUA Survey Agent v3 — dedicated window per survey.

Key changes from v2:
- Opens each survey in a NEW Chrome window (not a tab)
- Closes the window when done (no tab accumulation)
- Handles consent pages ("Agree and Continue")
- LLM answers ALL questions per page, not just 1-2
- No interference with user's existing Chrome tabs
"""

from __future__ import annotations

import contextlib
import hashlib
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

# ── Profile ───────────────────────────────────────────────────────────
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


# ── Chrome helpers ────────────────────────────────────────────────────
def find_dashboard_chrome():
    """Find the user's Chrome window with Branded Surveys dashboard."""
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
            title = (w.get("title") or "").lower()
            area = b.get("width", 0) * b.get("height", 0)
            if (
                b.get("height", 0) > 100
                and area > best_area
                and any(kw in title for kw in ["survey", "branded", "dashboard"])
            ):
                best_pid = w["pid"]
                best_wid = w["window_id"]
                best_area = area

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
    """Execute single JS expression via cua-driver page tool."""
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
    """Navigate the active tab to a URL via osascript."""
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


def open_new_window(url):
    """Open a NEW Chrome window with the given URL. Returns (pid, wid) or (None, None)."""
    # Record existing windows
    pre = set()
    r = subprocess.run(
        ["cua-driver", "call", "list_windows", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    try:
        for w in json.loads(r.stdout.strip()).get("windows", []):
            if w.get("app_name") == "Google Chrome" and w.get("bounds", {}).get("height", 0) > 100:
                pre.add(w.get("window_id"))
    except json.JSONDecodeError:
        pass

    # Open new window via osascript (two-step: create window, then navigate)
    safe_url = url.replace('"', '\\"')
    subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "Google Chrome" to set newWin to make new window',
            "-e",
            f'tell application "Google Chrome" to set URL of active tab of newWin to "{safe_url}"',
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    # Retry finding the new window (it may take a moment to appear)
    for _attempt in range(3):
        time.sleep(3)
        r2 = subprocess.run(
            ["cua-driver", "call", "list_windows", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        try:
            for w in json.loads(r2.stdout.strip()).get("windows", []):
                if (
                    w.get("app_name") == "Google Chrome"
                    and w.get("bounds", {}).get("height", 0) > 100
                    and w.get("window_id") not in pre
                ):
                    return w.get("pid"), w.get("window_id")
        except json.JSONDecodeError:
            pass
    return None, None


def close_window(pid, wid):
    """Close a Chrome window by sending cmd+w via cua-driver."""
    with contextlib.suppress(Exception):
        subprocess.run(
            ["cua-driver", "call", "hotkey", json.dumps({"pid": pid, "window_id": wid, "keys": ["cmd", "w"]})],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )


def get_page_text(pid, wid):
    """Get visible text. JS first, AX tree fallback."""
    result = js(pid, wid, "document.body ? document.body.innerText.substring(0, 5000) : ''")
    if result not in ("missing value", "", "error: Unsupported browser: bundle_id="):
        return result

    r = subprocess.run(
        ["cua-driver", "call", "get_window_state", json.dumps({"pid": pid, "window_id": wid, "max_elements": 200})],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    try:
        data = json.loads(r.stdout.strip())
        return "\n".join(
            el.get("label", "") for el in data.get("elements", []) if el.get("label") and len(el.get("label", "")) > 1
        )
    except json.JSONDecodeError:
        return ""


def get_page_url(pid, wid):
    result = js(pid, wid, "document.location.href")
    if result not in ("missing value", "", "error: Unsupported browser: bundle_id="):
        return result
    return ""


def get_page_title(pid, wid):
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
    return js(
        pid,
        wid,
        f'(function(){{var r=document.querySelectorAll("input[type=radio]");'
        f"if(r[{index}]){{r[{index}].checked=true;"
        f'r[{index}].dispatchEvent(new Event("change",{{bubbles:true}}));'
        f'r[{index}].dispatchEvent(new Event("click",{{bubbles:true}}));}}return "ok"}})()',
    )


def set_checkbox(pid, wid, index):
    return js(
        pid,
        wid,
        f'(function(){{var c=document.querySelectorAll("input[type=checkbox]");'
        f"if(c[{index}]){{c[{index}].checked=true;"
        f'c[{index}].dispatchEvent(new Event("change",{{bubbles:true}}));'
        f'c[{index}].dispatchEvent(new Event("click",{{bubbles:true}}));}}return "ok"}})()',
    )


def fill_text(pid, wid, selector, value):
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
    """Find and click submit/next button using SOM capture."""
    # Method 1: Use SOM to find and click submit/next button
    try:
        r = subprocess.run(
            ["cua-driver", "call", "get_window_state", json.dumps({"pid": pid, "window_id": wid, "max_elements": 200})],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        data = json.loads(r.stdout.strip())
        elements = data.get("elements", [])
        submit_keywords = ["next", "submit", "continue", "done", "finish", "proceed", "agree", "start"]
        for el in elements:
            label = (el.get("label") or "").lower()
            role = (el.get("role") or "").lower()
            if role in ("button", "link", "buttondefault") and any(kw in label for kw in submit_keywords):
                el_idx = el.get("index")
                if el_idx:
                    subprocess.run(
                        ["cua-driver", "call", "click", json.dumps({"pid": pid, "window_id": wid, "element": el_idx})],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    return "som_click:" + label[:30]
    except (json.JSONDecodeError, subprocess.TimeoutExpired):
        pass

    # Method 2: JS fallback — click any visible button
    result = js(
        pid,
        wid,
        "(function(){"
        'var btns=document.querySelectorAll("button,input[type=submit],a[role=button]");'
        "for(var i=0;i<btns.length;i++){"
        'if(btns[i].offsetParent!==null){btns[i].click();return "js_click:"+btns[i].textContent.trim().substring(0,30);}}'
        'return "no_btn"})()',
    )
    if "click" in str(result):
        return result

    # Method 3: Press Enter
    payload = json.dumps({"pid": pid, "window_id": wid, "keys": ["return"]})
    subprocess.run(
        ["cua-driver", "call", "hotkey", payload],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return "enter_pressed"


def count_inputs(pid, wid):
    def safe_int(v):
        try:
            return int(v.strip()) if v.strip().isdigit() else 0
        except (ValueError, AttributeError):
            return 0

    return {
        "radio": safe_int(js(pid, wid, 'document.querySelectorAll("input[type=radio]").length')),
        "checkbox": safe_int(js(pid, wid, 'document.querySelectorAll("input[type=checkbox]").length')),
        "text": safe_int(
            js(
                pid,
                wid,
                'document.querySelectorAll("input[type=text],textarea,input[type=email],input[type=number]").length',
            )
        ),
        "select": safe_int(js(pid, wid, 'document.querySelectorAll("select").length')),
    }


def get_som_interactive(pid, wid):
    """Get interactive elements from SOM accessibility tree."""
    try:
        r = subprocess.run(
            ["cua-driver", "call", "get_window_state", json.dumps({"pid": pid, "window_id": wid, "max_elements": 200})],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        data = json.loads(r.stdout.strip())
        elements = data.get("elements", [])
        interactive = []
        for el in elements:
            role = (el.get("role") or "").lower()
            label = (el.get("label") or "").strip()
            idx = el.get("index")
            if not idx:
                continue
            # Include clickable/interactive elements
            if any(
                r in role for r in ["button", "radio", "checkbox", "text", "link", "combobox", "menu", "tab", "option"]
            ):
                interactive.append({"index": idx, "role": role, "label": label[:80]})
        return interactive
    except (json.JSONDecodeError, subprocess.TimeoutExpired):
        return []


def get_input_groups(pid, wid):
    """Get interactive elements via SOM, falling back to JS."""
    som_elements = get_som_interactive(pid, wid)
    if som_elements:
        result = []
        for el in som_elements:
            result.append(f"SOM_{el['role'].upper()} index={el['index']} label='{el['label']}'")
        return result
    return []


def handle_consent(pid, wid):
    """Click consent buttons using SOM capture for reliable clicking."""
    text = get_page_text(pid, wid)
    if "agree and continue" in text.lower():
        logger.info("Consent page detected, finding button via SOM")
        try:
            r = subprocess.run(
                [
                    "cua-driver",
                    "call",
                    "get_window_state",
                    json.dumps({"pid": pid, "window_id": wid, "max_elements": 200}),
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            data = json.loads(r.stdout.strip())
            elements = data.get("elements", [])
            for el in elements:
                label = (el.get("label") or "").lower()
                role = (el.get("role") or "").lower()
                if ("agree" in label or "continue" in label) and "button" in role:
                    el_idx = el.get("index")
                    if el_idx:
                        logger.info("  Found consent element #%d: %s", el_idx, label[:40])
                        subprocess.run(
                            [
                                "cua-driver",
                                "call",
                                "click",
                                json.dumps({"pid": pid, "window_id": wid, "element": el_idx}),
                            ],
                            capture_output=True,
                            text=True,
                            timeout=10,
                            check=False,
                        )
                        time.sleep(3)
                        return True
        except (json.JSONDecodeError, subprocess.TimeoutExpired):
            pass
        logger.warning("  Could not find consent button")
        time.sleep(3)
        return True
    return False


# ── LLM ───────────────────────────────────────────────────────────────
def get_llm_answer(question_text, input_labels, profile_str, num_questions=0):  # noqa: ARG001
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

    prompt = (
        "You are answering survey questions as this person:\n"
        + profile_str
        + "\n\nPAGE TEXT:\n"
        + question_text[:1500]
        + "\n\nINTERACTIVE ELEMENTS (clickable via SOM):\n"
        + chr(10).join(input_labels[:50])
        + "\n\nReturn a JSON array with ONE action per input/question on this page.\n"
        '- "index": the absolute index (0-based) across all inputs of that type\n'
        '- "action": "select" for radio/checkbox, "fill" for text\n'
        '- "value": for "fill" actions, the text to type\n'
        "\nRules:\n"
        "- There are MULTIPLE questions per page — answer ALL of them\n"
        "- For radio groups: pick ONE option per group\n"
        "- For checkboxes: pick 1-3 options per group\n"
        "- For text: provide a realistic short answer\n"
        "- Be consistent with the profile demographics\n"
        "- Answer as a real person would\n"
        "- NEVER type the word ROBOT\n"
        "- Return ONLY the JSON array\n"
        '\nExample for 3 radio groups: [{"index": 0, "action": "select"}, {"index": 3, "action": "select"}, {"index": 7, "action": "select"}]'
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
            if isinstance(content, list):
                content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
            match = re.search(r"\[.*\]", content, re.DOTALL)
            if match:
                try:
                    answers = json.loads(match.group())
                except json.JSONDecodeError:
                    # Try fixing common LLM JSON errors
                    raw = match.group()
                    # Remove trailing commas before ] or }
                    raw = re.sub(r",\s*([}\]])", r"\1", raw)
                    # Remove comments
                    raw = re.sub(r"//.*?\n", "\n", raw)
                    try:
                        answers = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.debug("JSON parse failed: %s", raw[:200])
                        return []
                return [a for a in answers if isinstance(a, dict) and (a.get("value") or "").strip().upper() != "ROBOT"]
    except Exception as e:
        logger.warning("LLM error: %s", e)
    return []


# ── CAPTCHA ───────────────────────────────────────────────────────────
def read_captcha_vision(image_path):
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
    except Exception:
        pass
    finally:
        Path(p).unlink(missing_ok=True)
    return ""


def screenshot_chrome(pid, wid):
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

            path = "/tmp/cua_screenshot.png"
            with open(path, "wb") as f:
                f.write(base64.b64decode(img_b64))
            return path
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def handle_captcha(pid, wid):
    text = get_page_text(pid, wid)
    tl = text.lower()
    if "type the following code" in tl or "type the characters" in tl:
        logger.info("Text CAPTCHA detected")
        sp = screenshot_chrome(pid, wid)
        if sp:
            ct = read_captcha_vision(sp)
            if ct:
                logger.info("CAPTCHA read: '%s'", ct)
                fill_text(pid, wid, "input[type=text]", ct)
                time.sleep(0.5)
                submit_form(pid, wid)
                time.sleep(2)
                return True
            logger.warning("Could not read CAPTCHA")
            return False
    return False


def check_balance():
    """Check balance by navigating to my-points page."""
    pid, wid = find_dashboard_chrome()
    if not pid:
        return None
    navigate("https://surveys.gobranded.com/my-points")
    time.sleep(8)
    text = get_page_text(pid, wid)
    match = re.search(r"(\d+)/500", text)
    if match:
        return int(match.group(1))
    match = re.search(r"(\d+)\s*POINTS?\s*UNTIL\s*PAYOUT", text, re.IGNORECASE)
    if match:
        return 500 - int(match.group(1))
    return None


# ── Main survey logic ─────────────────────────────────────────────────
def get_survey_urls(pid, wid):
    """Get survey pre-page URLs from the dashboard."""
    raw = js(
        pid,
        wid,
        'Array.from(document.querySelectorAll("a")).filter(function(a){'
        'return a.href&&a.href.includes("/surveys/pre/")&&!a.href.includes("/listing")'
        '}).map(function(a){return a.href}).join("\\n")',
    )
    if raw and raw not in ("missing value", ""):
        return [u.strip() for u in raw.split("\n") if u.strip()]
    return []


def run_one_survey(dash_pid, dash_wid):  # noqa: PLR0915
    """Run one survey in a dedicated window. Returns (completed, points)."""
    profile_str = "\n".join(f"{k}: {v}" for k, v in PROFILE.items())

    # Navigate dashboard to get survey URLs
    navigate("https://surveys.gobranded.com/users/dashboard")
    time.sleep(10)

    # Get survey URLs
    survey_urls = []
    for attempt in range(3):
        page_text = get_page_text(dash_pid, dash_wid)
        if "sign in" in page_text.lower():
            logger.error("Not logged in")
            return False, 0
        survey_urls = get_survey_urls(dash_pid, dash_wid)
        if survey_urls:
            break
        logger.info("  Attempt %d: no survey URLs, waiting...", attempt + 1)
        time.sleep(5)

    if not survey_urls:
        logger.warning("No survey URLs found")
        return False, 0

    # Open survey in a NEW window
    survey_url = survey_urls[0]
    logger.info("Opening survey in new window: %s", survey_url[:80])
    pid, wid = open_new_window(survey_url)
    if not pid:
        logger.warning("Could not open new window")
        return False, 0
    time.sleep(8)

    try:
        # Handle consent page
        handle_consent(pid, wid)
        time.sleep(2)

        # Find the actual survey URL from the pre-page
        page_text = get_page_text(pid, wid)
        get_page_url(pid, wid)

        if "take survey" in page_text.lower() or "start your survey" in page_text.lower():
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
                logger.info("Navigating to actual survey: %s", take_href[:80])
                # Navigate within the same window
                safe_url = take_href.replace('"', '\\"')
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
                time.sleep(8)
                # Handle consent again if needed
                handle_consent(pid, wid)
                time.sleep(2)

        # Answer questions loop
        last_url = ""
        last_hash = ""
        same_count = 0

        for page_num in range(40):
            page_text = get_page_text(pid, wid)
            page_url = get_page_url(pid, wid)
            page_title = get_page_title(pid, wid)

            if not page_text or len(page_text.strip()) < 10:
                time.sleep(5)
                page_text = get_page_text(pid, wid)
                if not page_text or len(page_text.strip()) < 10:
                    logger.warning("Page empty, giving up")
                    break

            # Stuck detection
            # Use visibility-aware hash to detect real page changes on SPAs
            visible_text = js(
                pid,
                wid,
                '(function(){var els=document.querySelectorAll("*");var t="";'
                "for(var i=0;i<els.length;i++){"
                "if(els[i].offsetParent!==null&&els[i].childNodes.length<=3&&els[i].textContent.trim().length>0){"
                't+=els[i].textContent.trim().substring(0,50)+"|";}}'
                "return t.substring(0,2000)})()",
            )
            hash_source = visible_text if visible_text not in ("missing value", "") else page_text
            content_hash = hashlib.md5(hash_source.encode()).hexdigest()[:12]
            if page_url == last_url and content_hash == last_hash:
                same_count += 1
                if same_count >= 3:
                    logger.warning("Stuck on same page for 3 iterations")
                    break
            else:
                same_count = 0
            last_url = page_url
            last_hash = content_hash

            logger.info("[Page %d] %s | %d chars | %s", page_num + 1, page_title[:40], len(page_text), page_url[:60])

            # Check for completion
            tl = page_text.lower()
            if any(
                kw in tl
                for kw in ["thank you", "survey complete", "you have completed", "congratulations", "you earned"]
            ):
                logger.info("SURVEY COMPLETED!")
                return True, 0

            # Check for disqualification
            if any(
                kw in tl
                for kw in ["not qualified", "disqualified", "do not qualify", "survey not a match", "not a match"]
            ):
                logger.info("Disqualified")
                return False, 0

            # Check if on non-survey page
            is_survey = any(
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
                    "nfieldmr",
                    "purespectrum",
                    "decipherinc",
                ]
            )
            if not is_survey and any(kw in page_url.lower() for kw in ["dashboard", "/members"]):
                if page_num == 0:
                    logger.info("Never reached survey page")
                    return False, 0
                logger.info("Back on dashboard — survey done")
                return True, 0

            # Handle consent
            handle_consent(pid, wid)

            # Handle CAPTCHA
            if handle_captcha(pid, wid):
                continue

            # Count inputs
            inputs = count_inputs(pid, wid)
            total = sum(inputs.values())

            if total == 0:
                submit_form(pid, wid)
                time.sleep(3)
                continue

            # Get labels and ask LLM
            labels = get_input_groups(pid, wid)
            num_questions = sum(
                1
                for lbl in labels
                if any(
                    p in lbl for p in ["SOM_RADIO", "SOM_BUTTON", "SOM_OPTION", "SOM_TEXT", "SOM_COMBOBOX", "SOM_LINK"]
                )
            )
            logger.info("  Groups: %d questions from %d raw inputs", num_questions, total)
            for lbl in labels[:10]:
                logger.info("    %s", lbl[:100])
            answers = get_llm_answer(page_text, labels, profile_str, num_questions)
            logger.info("  LLM: %d answers for %d questions", len(answers), num_questions)

            if answers:
                for ans in answers:
                    idx = ans.get("index", 0)
                    action = ans.get("action", "select")
                    if action == "select":
                        # Click via SOM element index
                        payload = json.dumps({"pid": pid, "window_id": wid, "element": idx})
                        subprocess.run(
                            ["cua-driver", "call", "click", payload],
                            capture_output=True,
                            text=True,
                            timeout=10,
                            check=False,
                        )
                    elif action == "fill":
                        # Type into SOM element
                        payload = json.dumps(
                            {"pid": pid, "window_id": wid, "element": idx, "text": ans.get("value", "")}
                        )
                        subprocess.run(
                            ["cua-driver", "call", "type", payload],
                            capture_output=True,
                            text=True,
                            timeout=10,
                            check=False,
                        )
            else:
                # Fallback: click first SOM button/radio/option
                som_els = get_som_interactive(pid, wid)
                for el in som_els[:1]:
                    payload = json.dumps({"pid": pid, "window_id": wid, "element": el["index"]})
                    subprocess.run(
                        ["cua-driver", "call", "click", payload],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )

            time.sleep(0.5)
            submit_form(pid, wid)
            time.sleep(3)

        return False, 0

    finally:
        # Always close the survey window
        logger.info("Closing survey window")
        close_window(pid, wid)
        time.sleep(2)


def main():
    """Main loop: 8-12 surveys per cycle, 15-45 min pause."""
    logger.info("CUA Survey Agent v3 starting")

    dash_pid, dash_wid = find_dashboard_chrome()
    if not dash_pid:
        logger.error("No Chrome window found")
        return
    logger.info("Dashboard Chrome: pid=%d wid=%d", dash_pid, dash_wid)

    initial_balance = check_balance()
    logger.info("Initial balance: %s", initial_balance)

    cycle = 0
    while True:
        cycle += 1
        num_surveys = random.randint(8, 12)
        logger.info("\n=== Cycle %d: %d surveys ===", cycle, num_surveys)

        completed_count = 0
        for i in range(num_surveys):
            logger.info("\n--- Survey %d/%d ---", i + 1, num_surveys)
            try:
                completed, _ = run_one_survey(dash_pid, dash_wid)
                if completed:
                    completed_count += 1
                    logger.info("Survey %d completed! (%d total)", i + 1, completed_count)
                else:
                    logger.info("Survey %d not completed", i + 1)
            except Exception as e:
                logger.error("Survey %d error: %s", i + 1, e)

            # Check balance periodically
            new_balance = check_balance()
            if new_balance and initial_balance and new_balance > initial_balance:
                earned = new_balance - initial_balance
                logger.info("*** EARNED %d POINTS! Balance: %d -> %d ***", earned, initial_balance, new_balance)
                initial_balance = new_balance

            pause = random.randint(3, 8)
            logger.info("Pausing %d seconds...", pause)
            time.sleep(pause)

        pause_min = random.randint(15, 45)
        logger.info(
            "\n=== Cycle %d done: %d/%d completed. Pausing %d min ===", cycle, completed_count, num_surveys, pause_min
        )
        time.sleep(pause_min * 60)


if __name__ == "__main__":
    main()
