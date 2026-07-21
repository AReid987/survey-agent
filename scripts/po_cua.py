"""PO instant-qualify via cua-driver. Click tiles, skip screening, complete externals.

Strategy: PO tiles sometimes return status:"qualified" with an external URL.
Click each tile, check qualification response, navigate to URL if qualified.
Skip status:"accepted" (screening) tiles. Real Chrome = no headless detection.
"""

import asyncio
import json
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

SS_DIR = Path("data/smoke")


def log(msg):
    print(f"[po] {msg}", flush=True)


def find_chrome():
    """Find Chrome window on Prime Opinion."""
    r = subprocess.run(
        ["cua-driver", "call", "list_windows", "--json"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    try:
        windows = json.loads(r.stdout.strip()).get("windows", [])
    except json.JSONDecodeError:
        return None, None
    for w in windows:
        if w.get("app_name") == "Google Chrome":
            b = w.get("bounds", {})
            if b.get("height", 0) > 100:
                title = (w.get("title") or "").lower()
                if any(kw in title for kw in ["prime", "survey", "opinion"]):
                    return w["pid"], w["window_id"]
    # Fallback: largest Chrome window
    best = None
    for w in windows:
        if w.get("app_name") == "Google Chrome":
            b = w.get("bounds", {})
            area = b.get("width", 0) * b.get("height", 0)
            if b.get("height", 0) > 100 and (best is None or area > best[2]):
                best = (w["pid"], w["window_id"], area)
    if best:
        return best[0], best[1]
    return None, None


def js(pid, wid, expression, timeout=30):
    payload = json.dumps({
        "action": "execute_javascript",
        "pid": pid, "window_id": wid,
        "bundle_id": "com.google.Chrome",
        "javascript": expression,
    })
    for attempt in range(2):
        try:
            r = subprocess.run(
                ["cua-driver", "call", "page", payload],
                capture_output=True, text=True, timeout=timeout, check=False,
            )
            return r.stdout.strip()
        except subprocess.TimeoutExpired:
            if attempt == 0:
                time.sleep(2)
                continue
            return ""
    return ""


def navigate(pid, wid, url):
    payload = json.dumps({
        "action": "execute_javascript",
        "pid": pid, "window_id": wid,
        "bundle_id": "com.google.Chrome",
        "javascript": f'window.location.href = {json.dumps(url)}',
    })
    try:
        subprocess.run(
            ["cua-driver", "call", "page", payload],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except subprocess.TimeoutExpired:
        pass


def wait_for_tiles(pid, wid, max_wait=15):
    """Wait until survey tiles are visible on the page."""
    for _ in range(max_wait):
        count_js = js(pid, wid, "(function(){return document.querySelectorAll('.list-item.survey-item').length;})()")
        try:
            count = int(str(count_js).strip())
        except (ValueError, TypeError):
            count = 0
        if count > 0:
            return count
        time.sleep(1)
    return 0


def try_answer_screening(pid, wid):
    """Answer a screening question using profile-based matching. Returns True if answered."""
    # Extract question text and options
    data_js = js(pid, wid, """(function(){
        // Get question text
        var qEl = document.querySelector('.question-text, [class*="question-title"], [class*="QuestionTitle"], h2, h3');
        var question = qEl ? qEl.innerText.trim() : '';

        // Get radio buttons with labels
        var radios = document.querySelectorAll('input[type="radio"]');
        var options = [];
        radios.forEach(function(r) {
            var label = '';
            if (r.labels && r.labels[0]) label = r.labels[0].innerText.trim();
            if (!label) {
                var parent = r.closest('label') || r.closest('[class*="answer"]') || r.parentElement;
                if (parent) label = parent.innerText.trim().substring(0, 100);
            }
            if (label && r.offsetWidth > 0 || r.offsetHeight > 0) {
                options.push({id: r.id, label: label, value: r.value});
            }
        });

        return JSON.stringify({question: question.substring(0, 200), options: options.slice(0, 10)});
    })()""")

    if not data_js or data_js in ("missing value", "", "null"):
        return False

    try:
        data = json.loads(data_js)
    except json.JSONDecodeError:
        return False

    question = data.get("question", "").lower()
    options = data.get("options", [])

    if not options:
        return False

    # Profile-based matching (simplified from persona_pick_radio)
    def find_match(*keywords):
        for opt in options:
            label = (opt.get("label") or "").lower()
            for kw in keywords:
                if kw in label:
                    return opt
        return None

    picked = None
    # Gender
    if any(k in question for k in ["gender", "sex"]):
        picked = find_match("male", "man")
    # Age
    elif any(k in question for k in ["age", "how old"]):
        for opt in options:
            label = (opt.get("label") or "").lower()
            if "35" in label or "30-39" in label or "35-44" in label:
                picked = opt
                break
    # State
    elif any(k in question for k in ["state", "live in", "reside"]):
        picked = find_match("texas", "tx")
    # Income
    elif any(k in question for k in ["income", "earn", "salary", "household"]):
        for opt in options:
            label = (opt.get("label") or "").lower()
            if "50" in label or "60" in label or "75" in label:
                picked = opt
                break
    # Children
    elif any(k in question for k in ["children", "kids", "child"]):
        picked = find_match("yes")
    # Employment
    elif any(k in question for k in ["employment", "occupation", "work"]):
        picked = find_match("self-employed", "self employed", "freelance", "full-time", "full time", "employed")
    # Education
    elif any(k in question for k in ["education", "degree"]):
        picked = find_match("bachelor", "college", "4-year", "four year")
    # Race
    elif any(k in question for k in ["race", "ethnicity"]):
        picked = find_match("black", "african")
    # Housing
    elif any(k in question for k in ["housing", "rent", "own"]):
        picked = find_match("rent")
    # Smartphone
    elif any(k in question for k in ["smartphone", "phone", "device"]):
        picked = find_match("android", "iphone", "samsung")
    # Area
    elif any(k in question for k in ["area", "urban", "city"]):
        picked = find_match("urban", "city", "metro")
    # Webcam
    elif any(k in question for k in ["webcam", "camera"]):
        picked = find_match("yes", "willing", "i have")

    # Fallback: pick the middle option (avoid extremes)
    if not picked and len(options) >= 2:
        mid = len(options) // 2
        picked = options[mid]

    if not picked:
        return False

    # Click the radio
    radio_id = picked.get("id", "")
    if not radio_id:
        return False

    clicked = js(pid, wid, f"""(function(){{
        var r = document.getElementById('{radio_id}');
        if (!r) return false;
        var label = r.closest('label') || r.parentElement;
        if (label) label.click();
        else r.click();
        return true;
    }})()""")

    if clicked not in ("true", "True", True):
        return False

    # Click submit/next
    time.sleep(1)
    submitted = js(pid, wid, """(function(){
        var btns = document.querySelectorAll('button, input[type="submit"], [role="button"]');
        for (var i = 0; i < btns.length; i++) {
            var b = btns[i];
            var text = (b.innerText || b.value || '').trim().toLowerCase();
            if (text.includes('next') || text.includes('submit') || text.includes('continue') ||
                text.includes('claim') || b.type === 'submit') {
                if (b.offsetWidth > 0) { b.click(); return text; }
            }
        }
        return 'no-button';
    })()""")

    return submitted not in ("", "no-button", "missing value")


def get_balance(pid, wid):
    result = js(pid, wid, r"""() => {
        const el = document.querySelector('.user-balance');
        if (!el) return null;
        const m = el.innerText.match(/\$\s*([\d.]+)/);
        return m ? m[1] : null;
    }""")
    if not result or result in ("null", "", "missing value", "None"):
        return None
    try:
        return float(result)
    except (ValueError, TypeError):
        return None


def click_tile(pid, wid, tile_index=0):
    """Click the Nth survey tile. Returns ('qualified', url) | ('accepted', '') | ('error', msg)."""
    # Record the current URL before clicking
    url_before_js = js(pid, wid, "() => window.location.href")
    url_before = url_before_js if url_before_js not in ("missing value", "", "null") else ""

    # Click the tile
    js(pid, wid, f"""(function(){{
        var tiles = document.querySelectorAll('.list-item.survey-item');
        if (tiles.length <= {tile_index}) return 'no-tile';
        tiles[{tile_index}].scrollIntoView();
        tiles[{tile_index}].click();
        return 'clicked';
    }})()""")
    time.sleep(4)

    # Check the page state after clicking
    # Case 1: Page navigated to an external survey URL (qualified)
    url_after_js = js(pid, wid, "() => window.location.href")
    url_after = url_after_js if url_after_js not in ("missing value", "", "null") else ""

    if url_after and url_after != url_before:
        # Page navigated — likely to a new tab or external survey
        if any(kw in url_after.lower() for kw in ["r.prmin.net", "cint", "dynata", "out?sh", "surveys", "opinion"]):
            return ("qualified", url_after)

    # Case 2: Question form appeared on the same page (accepted - screening)
    has_question_js = js(pid, wid, """(function(){
        // Check for radio buttons, question text, or submit button
        var radios = document.querySelectorAll('input[type="radio"]');
        var questions = document.querySelectorAll('[class*="question"], [class*="Question"]');
        var submitBtns = document.querySelectorAll('button[type="submit"], .submit-btn, [class*="submit"]');
        if (radios.length > 0 || questions.length > 0 || submitBtns.length > 0) {
            return 'has-question';
        }
        return 'no-question';
    })()""")
    has_question = has_question_js if has_question_js not in ("missing value", "") else "no-question"

    if has_question == "has-question":
        return ("accepted", "")

    # Case 3: Page might have a qualification URL in the DOM (link not navigation)
    url_in_dom_js = js(pid, wid, """(function(){
        var links = document.querySelectorAll('a[href]');
        for (var i = 0; i < links.length; i++) {
            var href = links[i].href || '';
            if (href.includes('r.prmin.net') || href.includes('cint') || href.includes('dynata') || href.includes('out?sh=')) {
                if (links[i].offsetWidth > 0) return href;
            }
        }
        return 'no-url';
    })()""")
    url_in_dom = url_in_dom_js if url_in_dom_js not in ("missing value", "") else "no-url"

    if url_in_dom and url_in_dom != "no-url":
        return ("qualified", url_in_dom)

    return ("error", "no-navigation-no-question")


def main():
    log("Finding Chrome...")
    pid, wid = find_chrome()
    if not pid:
        log("No Chrome found, opening PO...")
        subprocess.run(
            ["open", "-a", "Google Chrome", "https://app.primeopinion.com/"],
            capture_output=True, timeout=10, check=False,
        )
        time.sleep(6)
        pid, wid = find_chrome()
    if not pid:
        log("FATAL: Cannot find Chrome")
        return
    log(f"Chrome found: pid={pid} wid={wid}")

    # Navigate to PO home
    log("Navigating to Prime Opinion...")
    navigate(pid, wid, "https://app.primeopinion.com/")
    if wait_for_tiles(pid, wid) == 0:
        log("FATAL: No survey tiles found after navigate")
        return
    time.sleep(2)

    start_balance = get_balance(pid, wid)
    log(f"Balance: ${start_balance}")

    qualified_count = 0
    total_clicks = 0
    max_tiles = 30

    for i in range(max_tiles):
        log(f"--- Tile {i+1}/{max_tiles} ---")
        status, url = click_tile(pid, wid, 0)
        total_clicks += 1
        log(f"  Status: {status} | URL: {url[:100] if url else 'None'}")

        if status == "error":
            log("  Error, navigating back...")
            navigate(pid, wid, "https://app.primeopinion.com/")
            wait_for_tiles(pid, wid)
            time.sleep(2)
            continue

        if status == "accepted":
            log("  Screening question appeared, attempting to answer...")
            answered = try_answer_screening(pid, wid)
            if answered:
                log("  Answered screening, checking for qualified URL...")
                time.sleep(4)
                # Check if a qualified URL appeared
                qualified_url_js = js(pid, wid, """(function(){
                    var links = document.querySelectorAll('a[href]');
                    for (var i = 0; i < links.length; i++) {
                        var href = links[i].href || '';
                        if ((href.includes('r.prmin.net') || href.includes('cint') ||
                             href.includes('dynata') || href.includes('out?sh=')) &&
                            links[i].offsetWidth > 0) {
                            return href;
                        }
                    }
                    // Also check current URL
                    if (window.location.href.includes('r.prmin.net') ||
                        window.location.href.includes('out?sh=')) {
                        return window.location.href;
                    }
                    return '';
                })()""")
                if qualified_url_js and qualified_url_js not in ("", "missing value", "no-url"):
                    log(f"  ✓ QUALIFIED after screening! URL: {qualified_url_js[:120]}")
                    status = "qualified"
                    url = qualified_url_js
                else:
                    log("  Still no qualified URL after answering")
            else:
                log("  Could not answer screening question")
            navigate(pid, wid, "https://app.primeopinion.com/")
            wait_for_tiles(pid, wid)
            time.sleep(2)
            continue

        if status == "qualified" and url:
            log(f"  ✓ QUALIFIED! URL: {url[:120]}")
            qualified_count += 1

            # Open the external survey in a new tab
            js(pid, wid, f"""(function(){{
                window.open({json.dumps(url)}, '_blank');
                return 'opened';
            }})()""")
            time.sleep(15)

            # Navigate back to PO home and check balance
            navigate(pid, wid, "https://app.primeopinion.com/")
            wait_for_tiles(pid, wid)
            time.sleep(2)
            new_balance = get_balance(pid, wid)
            log(f"  Balance after survey: ${new_balance}")

            if new_balance and start_balance and new_balance > start_balance:
                earned = new_balance - start_balance
                log(f"  ✅ EARNED ${earned:.2f}! Balance: ${start_balance} → ${new_balance}")
                start_balance = new_balance
            else:
                log(f"  Balance unchanged: ${new_balance}")
            navigate(pid, wid, "https://app.primeopinion.com/")
            wait_for_tiles(pid, wid)
            time.sleep(2)

    log("=" * 60)
    log(f"Total tile clicks: {total_clicks}")
    log(f"Qualified: {qualified_count}")
    end_balance = get_balance(pid, wid)
    log(f"Final balance: ${end_balance}")
    log("=" * 60)


if __name__ == "__main__":
    main()
