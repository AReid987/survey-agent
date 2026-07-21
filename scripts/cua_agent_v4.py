"""
CUA Survey Agent v4 — text-matching approach.

Key insight: instead of trying to detect HTML form inputs (which survey platforms
disguise), use the LLM to return answer TEXT and click elements by matching their
visible text content. This works regardless of element type (div, span, button, etc.)
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
            area = b.get("width", 0) * b.get("height", 0)
            title = (w.get("title") or "").lower()
            if (
                b.get("height", 0) > 100
                and area > best_area
                and any(kw in title for kw in ["survey", "branded", "dashboard"])
            ):
                best_pid, best_wid, best_area = w["pid"], w["window_id"], area
    if not best_pid:
        for w in windows:
            if w.get("app_name") == "Google Chrome":
                b = w.get("bounds", {})
                area = b.get("width", 0) * b.get("height", 0)
                if b.get("height", 0) > 100 and area > best_area:
                    best_pid, best_wid, best_area = w["pid"], w["window_id"], area
    return best_pid, best_wid


def js(pid, wid, expression):
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
    with contextlib.suppress(Exception):
        subprocess.run(
            ["cua-driver", "call", "hotkey", json.dumps({"pid": pid, "window_id": wid, "keys": ["cmd", "w"]})],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )


def get_page_text(pid, wid):
    result = js(pid, wid, "document.body ? document.body.innerText.substring(0, 5000) : ''")
    if result not in ("missing value", "", "error: Unsupported browser: bundle_id="):
        return result
    return ""


def get_page_url(pid, wid):
    result = js(pid, wid, "document.location.href")
    return result if result not in ("missing value", "", "error: Unsupported browser: bundle_id=") else ""


def get_page_title(pid, wid):
    result = js(pid, wid, "document.title")
    return result if result not in ("missing value", "", "error: Unsupported browser: bundle_id=") else ""


def click_element_by_text(pid, wid, text):
    """Click an element whose visible text matches. Returns True if clicked."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("'", "\\'")
    result = js(
        pid,
        wid,
        f"(function(){{"
        f'var els=document.querySelectorAll("div,span,button,a,label,li,p,td");'
        f"for(var i=0;i<els.length;i++){{"
        f"var t=els[i].textContent.trim();"
        f'if(t==="{escaped}"&&els[i].offsetParent!==null){{els[i].click();return "clicked";}}}}'
        f'return "not_found"}})()',
    )
    return "clicked" in str(result)


def click_element_by_index(pid, wid, element_type, index):
    """Click the nth element of a type. Returns True if clicked."""
    result = js(
        pid,
        wid,
        f"(function(){{"
        f'var els=document.querySelectorAll("{element_type}");'
        f"var vis=[];for(var i=0;i<els.length;i++){{if(els[i].offsetParent!==null)vis.push(els[i]);}}"
        f'if(vis[{index}]){{vis[{index}].click();return "clicked";}}'
        f'return "not_found"}})()',
    )
    return "clicked" in str(result)


def fill_text_by_index(pid, wid, index, value):
    """Fill the nth visible text input."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("'", "\\'")
    result = js(
        pid,
        wid,
        f"(function(){{"
        f'var els=document.querySelectorAll("input[type=text],textarea,input[type=email],input[type=number]");'
        f"var vis=[];for(var i=0;i<els.length;i++){{if(els[i].offsetParent!==null)vis.push(els[i]);}}"
        f'if(vis[{index}]){{vis[{index}].focus();vis[{index}].value="{escaped}";'
        f'vis[{index}].dispatchEvent(new Event("input",{{bubbles:true}}));'
        f'vis[{index}].dispatchEvent(new Event("change",{{bubbles:true}}));return "filled";}}'
        f'return "not_found"}})()',
    )
    return "filled" in str(result)


def submit_form(pid, wid):
    """Click submit/next/continue button."""
    # Find and click buttons with submit-like text
    result = js(
        pid,
        wid,
        "(function(){"
        'var btns=document.querySelectorAll("button,input[type=submit],a,div[role=button]");'
        "for(var i=0;i<btns.length;i++){"
        "var t=btns[i].textContent.trim().toLowerCase();"
        'if(btns[i].offsetParent!==null&&(t.includes("next")||t.includes("submit")||'
        't.includes("continue")||t.includes("done")||t.includes("finish")||'
        't.includes("proceed"))){btns[i].click();return "clicked:"+t;}}'
        'return "no_btn"})()',
    )
    if "clicked" in str(result):
        return result

    # Click last visible button (often the submit)
    result2 = js(
        pid,
        wid,
        "(function(){"
        'var btns=document.querySelectorAll("button,input[type=submit]");'
        "var vis=[];for(var i=0;i<btns.length;i++){"
        "if(btns[i].offsetParent!==null)vis.push(btns[i]);}"
        'if(vis.length>0){vis[vis.length-1].click();return "clicked_last:"+vis[vis.length-1].textContent.trim().substring(0,30);}'
        'return "no_visible_btn"})()',
    )
    if "clicked" in str(result2):
        return result2

    # Press Enter
    subprocess.run(
        ["osascript", "-e", 'tell application "System Events" to keystroke return'],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return "enter"


def handle_consent(pid, wid):
    """Click consent/agree buttons."""
    text = get_page_text(pid, wid)
    if "agree and continue" in text.lower():
        logger.info("Consent page detected, clicking agree")
        # Try clicking by text
        if click_element_by_text(pid, wid, "Agree and Continue"):
            time.sleep(3)
            return True
        # Try clicking by partial text
        js(
            pid,
            wid,
            '(function(){var els=document.querySelectorAll("button,a,div");'
            "for(var i=0;i<els.length;i++){"
            "var t=els[i].textContent.trim().toLowerCase();"
            'if(t.includes("agree")&&els[i].offsetParent!==null){els[i].click();return "clicked";}}'
            'return "none"})()',
        )
        time.sleep(3)
        return True
    return False


def get_visible_options(pid, wid):
    """Get visible text options on the page that look like answer choices."""
    result = js(
        pid,
        wid,
        "(function(){"
        'var els=document.querySelectorAll("div,span,button,a,label,li,td");'
        "var opts=[];"
        "for(var i=0;i<els.length;i++){"
        "var t=els[i].textContent.trim();"
        "if(t.length>0&&t.length<80&&els[i].offsetParent!==null&&"
        "els[i].children.length===0&&"
        '!t.includes("Skip")&&!t.includes("FAQ")&&!t.includes("Logout")&&'
        '!t.includes("Dashboard")&&!t.includes("Points")&&!t.includes("Branded")){'
        "opts.push(t);}}"
        "return JSON.stringify([...new Set(opts)].slice(0,30));})()",
    )
    try:
        return json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return []


# ── LLM ───────────────────────────────────────────────────────────────
def get_llm_answer(page_text, visible_options, profile_str):
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

    options_str = ", ".join(visible_options[:30]) if visible_options else "(no visible options)"

    prompt = (
        "You are answering a survey question as this person:\n"
        + profile_str
        + "\n\nPAGE TEXT:\n"
        + page_text[:1500]
        + "\n\nVISIBLE OPTIONS ON PAGE:\n"
        + options_str
        + "\n\nReturn a JSON array with the actions to take on this page.\n"
        "Each action has:\n"
        '- "type": "click" (click an option) or "fill" (type into a text field)\n'
        '- "text": the EXACT text of the option to click (for "click" type)\n'
        '- "value": the text to type (for "fill" type)\n'
        "\nRULES:\n"
        "- Answer ALL questions on the page\n"
        "- For click: 'text' must EXACTLY match one of the VISIBLE OPTIONS\n"
        "- For fill: provide a realistic short answer\n"
        "- Be consistent with the profile demographics\n"
        "- NEVER type or click the word ROBOT\n"
        "- Return ONLY the JSON array\n"
        '\nExample: [{"type":"click","text":"Male"},{"type":"click","text":"Full Time"},{"type":"fill","value":"Austin"}]'
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
                    raw = match.group()
                    raw = re.sub(r",\s*([}\]])", r"\1", raw)
                    try:
                        answers = json.loads(raw)
                    except json.JSONDecodeError:
                        return []
                return [
                    a
                    for a in answers
                    if isinstance(a, dict) and (a.get("text") or a.get("value") or "").strip().upper() != "ROBOT"
                ]
    except Exception as e:
        logger.warning("LLM error: %s", e)
    return []


# ── Balance ───────────────────────────────────────────────────────────
def check_balance():
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
    """Run one survey in a dedicated window."""
    profile_str = "\n".join(f"{k}: {v}" for k, v in PROFILE.items())

    navigate("https://surveys.gobranded.com/users/dashboard")
    time.sleep(10)

    survey_urls = []
    for attempt in range(3):
        page_text = get_page_text(dash_pid, dash_wid)
        if "sign in" in page_text.lower():
            logger.error("Not logged in")
            return False
        survey_urls = get_survey_urls(dash_pid, dash_wid)
        if survey_urls:
            break
        logger.info("  Attempt %d: no survey URLs, waiting...", attempt + 1)
        time.sleep(5)

    if not survey_urls:
        logger.warning("No survey URLs found")
        return False

    survey_url = survey_urls[0]
    logger.info("Opening survey: %s", survey_url[:80])
    pid, wid = open_new_window(survey_url)
    if not pid:
        logger.warning("Could not open new window")
        return False
    time.sleep(8)

    try:
        handle_consent(pid, wid)
        time.sleep(2)

        page_text = get_page_text(pid, wid)
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
                logger.info("Navigating to survey: %s", take_href[:80])
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
                handle_consent(pid, wid)
                time.sleep(2)

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
                    logger.warning("Page empty")
                    break

            # Stuck detection using visibility-aware hash
            visible_hash_source = js(
                pid,
                wid,
                '(function(){var els=document.querySelectorAll("*");var t="";'
                "for(var i=0;i<els.length;i++){"
                "if(els[i].offsetParent!==null&&els[i].childNodes.length<=3&&els[i].textContent.trim().length>0){"
                't+=els[i].textContent.trim().substring(0,50)+"|";}}'
                "return t.substring(0,2000)})()",
            )
            hash_source = visible_hash_source if visible_hash_source not in ("missing value", "") else page_text
            content_hash = hashlib.md5(hash_source.encode()).hexdigest()[:12]

            if content_hash == last_hash:
                same_count += 1
                if same_count >= 3:
                    logger.warning("Stuck on same page for 3 iterations")
                    break
            else:
                same_count = 0
            last_hash = content_hash

            # Skip iframe surveys
            iframe_count = js(pid, wid, 'document.querySelectorAll("iframe").length')
            try:
                num_iframes = int(str(iframe_count).strip()) if str(iframe_count).strip().isdigit() else 0
            except (ValueError, AttributeError):
                num_iframes = 0
            if num_iframes > 0 and len(page_text.strip()) < 200:
                logger.info("Iframe survey (%d iframes, %d chars), skipping", num_iframes, len(page_text))
                break

            logger.info("[Page %d] %s | %d chars | %s", page_num + 1, page_title[:40], len(page_text), page_url[:60])

            # Check completion
            tl = page_text.lower()
            if any(
                kw in tl
                for kw in ["thank you", "survey complete", "you have completed", "congratulations", "you earned"]
            ):
                logger.info("SURVEY COMPLETED!")
                return True

            # Check disqualification
            if any(
                kw in tl
                for kw in ["not qualified", "disqualified", "do not qualify", "survey not a match", "not a match"]
            ):
                logger.info("Disqualified")
                return False

            # Check non-survey page
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
                    return False
                logger.info("Back on dashboard")
                return True

            handle_consent(pid, wid)

            # Get visible options and ask LLM
            options = get_visible_options(pid, wid)
            logger.info("  Options: %d visible", len(options))
            answers = get_llm_answer(page_text, options, profile_str)
            logger.info("  LLM: %d answers", len(answers))

            # Apply answers
            for ans in answers:
                action_type = ans.get("type", "click")
                if action_type == "click":
                    text = ans.get("text", "")
                    if text:
                        clicked = click_element_by_text(pid, wid, text)
                        if not clicked:
                            # Try partial match
                            js(
                                pid,
                                wid,
                                f'(function(){{var els=document.querySelectorAll("div,span,button,a,label,li");'
                                f"for(var i=0;i<els.length;i++){{"
                                f"var t=els[i].textContent.trim().toLowerCase();"
                                f'if(t.includes("{text.lower()}")&&els[i].offsetParent!==null){{els[i].click();return "clicked";}}}}'
                                f'return "none"}})()',
                            )
                elif action_type == "fill":
                    value = ans.get("value", "")
                    if value:
                        fill_text_by_index(pid, wid, 0, value)

            time.sleep(0.5)
            submit_result = submit_form(pid, wid)
            logger.info("  Submit: %s", submit_result)
            time.sleep(3)

        return False

    finally:
        logger.info("Closing survey window")
        close_window(pid, wid)
        time.sleep(2)


def main():
    logger.info("CUA Survey Agent v4 starting")

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
                if run_one_survey(dash_pid, dash_wid):
                    completed_count += 1
                    logger.info("Survey %d completed! (%d total)", i + 1, completed_count)
                else:
                    logger.info("Survey %d not completed", i + 1)
            except Exception as e:
                logger.error("Survey %d error: %s", i + 1, e)

            new_balance = check_balance()
            if new_balance and initial_balance and new_balance > initial_balance:
                earned = new_balance - initial_balance
                logger.info("*** EARNED %d POINTS! Balance: %d -> %d ***", earned, initial_balance, new_balance)
                initial_balance = new_balance

            time.sleep(random.randint(3, 8))

        pause_min = random.randint(15, 45)
        logger.info(
            "\n=== Cycle %d done: %d/%d completed. Pausing %d min ===", cycle, completed_count, num_surveys, pause_min
        )
        time.sleep(pause_min * 60)


if __name__ == "__main__":
    main()
