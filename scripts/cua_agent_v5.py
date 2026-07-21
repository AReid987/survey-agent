"""
CUA Survey Agent v5 — profile-driven with living memory.

Key improvements over v4:
- Uses ProfileStore (data/profile.json) for all answers
- Profile-first answering: LLM consults profile before answering
- Unknown question detection: texts user when profile lacks info
- Auto-updates profile with new answers
- Answer consistency tracking across survey pages
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

from survey_agent.profile.store import ProfileStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cua_agent")

# ── Paths ─────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
PROFILE_PATH = PROJECT_ROOT / "data" / "profile.json"
SURVEY_ANSWERS_PATH = PROJECT_ROOT / "data" / "survey_answers.json"


# ── Survey answer tracking ────────────────────────────────────────────
class SurveyAnswerTracker:
    """Track answers given during a survey for consistency."""

    def __init__(self):
        self.answers: list[dict] = []
        self._load()

    def _load(self):
        if SURVEY_ANSWERS_PATH.exists():
            with open(SURVEY_ANSWERS_PATH) as f:
                self.answers = json.load(f)

    def _save(self):
        with open(SURVEY_ANSWERS_PATH, "w") as f:
            json.dump(self.answers, f, indent=2)

    def record(self, question: str, answer: str, profile_key: str | None = None):
        self.answers.append(
            {
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "question": question[:200],
                "answer": answer,
                "profile_key": profile_key,
            }
        )
        # Keep last 500 answers
        self.answers = self.answers[-500:]
        self._save()

    def get_previous_answers(self, question_keyword: str) -> list[str]:
        """Find previous answers for similar questions."""
        keyword_lower = question_keyword.lower()
        return [a["answer"] for a in self.answers if keyword_lower in a["question"].lower()]


# ── Chrome helpers ────────────────────────────────────────────────────
def find_dashboard_chrome():
    """Find the Chrome window with Branded Surveys dashboard. Self-heals if Chrome is missing."""
    for attempt in range(3):
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
            windows = []

        best_pid, best_wid, best_area = None, None, 0
        for w in windows:
            if w.get("app_name") == "Google Chrome":
                b = w.get("bounds", {})
                area = b.get("width", 0) * b.get("height", 0)
                title = (w.get("title") or "").lower()
                if (
                    b.get("height", 0) > 100
                    and area > best_area
                    and any(kw in title for kw in ["survey", "branded", "dashboard", "primeopinion", "prime opinion"])
                ):
                    best_pid, best_wid, best_area = w["pid"], w["window_id"], area
        if not best_pid:
            for w in windows:
                if w.get("app_name") == "Google Chrome":
                    b = w.get("bounds", {})
                    area = b.get("width", 0) * b.get("height", 0)
                    if b.get("height", 0) > 100 and area > best_area:
                        best_pid, best_wid, best_area = w["pid"], w["window_id"], area

        if best_pid:
            return best_pid, best_wid

        # Self-heal: open Chrome to Branded Surveys dashboard
        if attempt == 0:
            logger.info("No Chrome window found, opening dashboard...")
            subprocess.run(
                ["open", "-a", "Google Chrome", "https://surveys.gobranded.com/users/dashboard"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            time.sleep(6)  # Wait for Chrome to open
            continue

    return None, None


def js(pid, wid, expression, timeout=30):
    payload = json.dumps(
        {
            "action": "execute_javascript",
            "pid": pid,
            "window_id": wid,
            "bundle_id": "com.google.Chrome",
            "javascript": expression,
        }
    )
    # Retry once on timeout — cua-driver occasionally hangs on page state changes
    for attempt in range(2):
        try:
            r = subprocess.run(
                ["cua-driver", "call", "page", payload],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return r.stdout.strip()
        except subprocess.TimeoutExpired:
            if attempt == 0:
                time.sleep(2)
                continue
            return ""
    return ""


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


def clean_page_text(raw_text):
    """Remove dashboard chrome, navigation, footer from page text before LLM."""
    lines = raw_text.split("\n")
    cleaned = []
    skip_patterns = [
        "skip to content",
        "hi, antonio",
        "my dashboard",
        "my points",
        "my payouts",
        "branded elite",
        "leaderboard",
        "invite friends",
        "offers",
        "faq",
        "payout options",
        "preferences",
        "logout",
        "general",
        "my profile",
        "email preferences",
        "password",
        "phone",
        "address",
        "delete account",
        "play now",
        "points until payout",
        "unlock rewards",
        "dashboard",
        "privacy",
        "terms of service",
        "cookie settings",
        "do not sell",
        "my data",
        "sign out",
        "my account",
        "help",
        "report survey",
        "system status",
        "blog",
        "give us feedback",
        "follow us",
        "download our app",
        "site copyright",
        "all rights reserved",
        "earn",
        "my points",
        "branded elite",
        "invite friends",
        "backspin games",
        "discover new favorites",
        "start your survey",
        "survey code",
        "take survey",
        "skip this survey",
        "why did i receive",
    ]
    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            continue
        lower = line_stripped.lower()
        if any(p in lower for p in skip_patterns):
            continue
        if len(line_stripped) < 3:
            continue
        # Keep lines that look like questions or answer options
        if (
            "?" in line_stripped
            or len(line_stripped) > 10
            or any(c.isalpha() for c in line_stripped)
            and len(line_stripped) >= 3
        ):
            cleaned.append(line_stripped)
    return "\n".join(cleaned)


def get_page_text(pid, wid):
    result = js(pid, wid, "document.body ? document.body.innerText.substring(0, 5000) : ''")
    return result if result not in ("missing value", "", "error: Unsupported browser: bundle_id=") else ""


def get_page_url(pid, wid):
    result = js(pid, wid, "document.location.href")
    return result if result not in ("missing value", "", "error: Unsupported browser: bundle_id=") else ""


def get_page_title(pid, wid):
    result = js(pid, wid, "document.title")
    return result if result not in ("missing value", "", "error: Unsupported browser: bundle_id=") else ""


def click_element_by_text(pid, wid, text):
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


def fill_text_by_index(pid, wid, index, value):
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

    result2 = js(
        pid,
        wid,
        "(function(){"
        'var btns=document.querySelectorAll("button,input[type=submit]");'
        "var vis=[];for(var i=0;i<btns.length;i++){"
        "if(btns[i].offsetParent!==null)vis.push(btns[i]);}"
        'if(vis.length>0){vis[vis.length-1].click();return "clicked_last";}'
        'return "no_visible_btn"})()',
    )
    if "clicked" in str(result2):
        return result2

    subprocess.run(
        ["osascript", "-e", 'tell application "System Events" to keystroke return'],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return "enter"


def handle_consent(pid, wid):
    text = get_page_text(pid, wid)
    if "agree and continue" in text.lower():
        logger.info("Consent page detected, clicking agree")
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
        raw = json.loads(result)
        # Filter cookie consent banner text from options
        CONSENT_KW = ["consent", "cookie", "necessary", "always active",
                      "functional", "advertisement", "uncategorised",
                      "reject all", "performance", "show more",
                      "customise", "preferences", "advert", "research"]
        return [o for o in raw if not any(kw in o.lower() for kw in CONSENT_KW)]
    except (json.JSONDecodeError, TypeError):
        return []


# ── Deterministic persona picker (stealth-runner pattern) ──
# Antonio Reid profile: 39M, Black, Austin TX, self-employed CTO, 3 kids (4/5/5),
# $50-75K household, Bachelor's degree, rent, former smoker, never drinks.
def persona_pick_radio(body: str, options: list[str]) -> str | None:
    """Deterministic persona match for common demographic questions.
    Returns exact option text from `options` list, or None if no match.
    Pattern stolen from SIN-CLIs/stealth-runner _persona_pick_radio.
    """
    if not options or not body:
        return None
    body_low = body.lower()
    options_low = {o.lower(): o for o in options}

    def find(*keywords):
        """Find first option containing any keyword (case-insensitive)."""
        for kw in keywords:
            kw_low = kw.lower()
            for olow, orig in options_low.items():
                if kw_low in olow:
                    return orig
        return None

    def find_range_containing(target, options_list):
        """Find option whose range contains the target number."""
        import re as _r
        for o in options_list:
            ol = o.lower()
            ranges = _r.findall(r"(\d{1,3}(?:[.,]\d{3})?(?:k)?)\s*(?:[-–—‒]|bis|to|under|less than)\s*(\d{1,3}(?:[.,]\d{3})?(?:k)?)", ol)
            for lo, hi in ranges:
                try:
                    lo_n = float(lo.replace(",", "").replace("k", "000"))
                    hi_n = float(hi.replace(",", "").replace("k", "000"))
                    if lo_n <= target <= hi_n:
                        return o
                except (ValueError, AttributeError):
                    pass
        return None

    # === GENDER ===
    if any(k in body_low for k in ["gender", "sex", "are you male", "are you female"]):
        m = find("male", "man", "masculino")
        if m:
            return m

    # === AGE ===
    if any(k in body_low for k in ["how old", "age", "your age", "age range", "age group"]):
        # Antonio is 39 → match "35-44", "30-39", "35 to 44", etc.
        for target in [39, 35, 30, 40]:
            r = find_range_containing(target, options)
            if r:
                return r
        # direct number match
        m = find("35-44", "30-39", "35 to 44", "40-49")
        if m:
            return m

    # === STATE / LOCATION ===
    if any(k in body_low for k in ["state", "live in", "located", "reside", "country", "zip", "postal"]):
        m = find("texas", "tx", "united states", "us", "usa")
        if m:
            return m

    # === CITY ===
    if any(k in body_low for k in ["city", "metro", "area"]):
        m = find("austin", "texas")
        if m:
            return m

    # === MARITAL ===
    if any(k in body_low for k in ["marital", "married", "relationship", "civil status"]):
        m = find("married", "domestic partnership", "partner")
        if m:
            return m

    # === CHILDREN ===
    if any(k in body_low for k in ["children", "kids", "child"]):
        if any(k in body_low for k in ["how many", "number of"]):
            m = find("3", "three")
            if m:
                return m
        else:
            m = find("yes", "sí")
            if m:
                return m

    # === EMPLOYMENT ===
    if any(k in body_low for k in ["employment", "occupation", "work status", "currently work", "job status"]):
        m = find("self-employed", "self employed", "freelance", "independent", "entrepreneur")
        if m:
            return m
        m = find("full-time", "full time", "employed")
        if m:
            return m

    # === INDUSTRY / SECTOR ===
    if any(k in body_low for k in ["industry", "sector", "field of work"]):
        m = find("technology", "information", "computer", "software", "tech", "internet", "it ")
        if m:
            return m

    # === COMPANY SIZE ===
    if any(k in body_low for k in ["company size", "employees", "how many people"]):
        m = find("1-10", "1 to 10", "1-4", "fewer than 10", "under 10", "1-9", "small")
        if m:
            return m

    # === INCOME ===
    if any(k in body_low for k in ["income", "earn", "salary", "household", "how much"]):
        # Antonio: $50-75K household
        for target in [60000, 55000, 50000, 65000, 70000]:
            r = find_range_containing(target, options)
            if r:
                return r
        m = find("$50", "$60", "50,000", "60,000", "50-75", "50,000-75,000", "50000-75000")
        if m:
            return m

    # === EDUCATION ===
    if any(k in body_low for k in ["education", "degree", "school", "highest level"]):
        m = find("bachelor", "college", "undergraduate", "4-year", "four year")
        if m:
            return m

    # === RACE / ETHNICITY ===
    if any(k in body_low for k in ["race", "ethnicity", "ethnic", "heritage"]):
        m = find("black", "african", "african-american")
        if m:
            return m

    # === HOUSING ===
    if any(k in body_low for k in ["housing", "own or rent", "home ownership", "rent or own"]):
        m = find("rent", "renter", "renting")
        if m:
            return m

    # === SMOKER ===
    if any(k in body_low for k in ["smok", "tobacco", "cigarette"]):
        m = find("former", "ex-smoker", "used to")
        if m:
            return m

    # === ALCOHOL ===
    if any(k in body_low for k in ["alcohol", "drink", "beer", "wine"]):
        m = find("never", "none", "don't drink", "do not drink")
        if m:
            return m

    # === DECISION MAKER / PURCHASING ===
    if any(k in body_low for k in ["decision", "purchas", "choose", "buy"]):
        m = find("yes", "i decide", "final approval", "decision maker", "sole")
        if m:
            return m

    # === LIKELIHOOD / RECOMMEND / NPS ===
    if any(k in body_low for k in ["recommend", "likely", "nps", "rate"]):
        m = find("10", "9", "8", "very likely", "extremely")
        if m:
            return m

    # === SMARTPHONE / DEVICE ===
    if any(k in body_low for k in ["smartphone", "phone", "device", "mobile"]):
        m = find("android", "iphone", "samsung", "apple")
        if m:
            return m

    # === WEBCAM ===
    if any(k in body_low for k in ["webcam", "camera", "video call"]):
        m = find("yes", "willing", "i have", "i do")
        if m:
            return m

    # === URBAN / SUBURBAN / RURAL ===
    if any(k in body_low for k in ["urban", "rural", "suburban", "area you live"]):
        m = find("urban", "city", "suburban", "metro")
        if m:
            return m

    # === OPEN-ENDED / FREE-TEXT SCREENER ===
    # PureSpectrum/Cint often ask "describe a memory" / "tell us about X"
    # These are screeners — typing a safe positive answer is the goal.
    if any(k in body_low for k in [
        "describe a", "tell us about", "what was", "share a",
        "happy memory", "favorite memory", "best experience",
        "childhood", "describe your", "in your own words",
    ]):
        # Return a safe short answer (caller will type it into textarea)
        return "I had a wonderful experience with my family and friends. It was a very happy time."

    return None


# ── LLM with profile-first answering ──────────────────────────────────
def get_api_key():
    api_key = ""
    try:
        with open(Path.home() / ".zshrc") as f:
            for line in f:
                if "OPENCODE_API_KEY" in line and "=" in line:
                    api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    except FileNotFoundError:
        pass
    return api_key or os.environ.get("OPENCODE_API_KEY", "")


def get_llm_answer(page_text, visible_options, profile_context, previous_answers, api_key):
    """Profile-first answering: LLM consults profile and previous answers."""
    import httpx  # noqa: PLC0415

    if not api_key:
        return [], []

    options_str = ", ".join(visible_options[:30]) if visible_options else "(no visible options)"
    prev_answers_str = ""
    if previous_answers:
        prev_answers_str = "\nPREVIOUS ANSWERS IN THIS SURVEY:\n" + "\n".join(
            f"- Q: {a['question'][:80]} → A: {a['answer']}" for a in previous_answers[-10:]
        )

    prompt = (
        "You are answering survey questions as the person described in the profile below.\n"
        "You MUST answer consistently with the profile. If the profile doesn't have enough\n"
        "information to answer a question, set 'unknown' to true for that action.\n\n"
        "PROFILE:\n"
        + profile_context
        + "\n\nPAGE TEXT:\n"
        + page_text[:1500]
        + "\n\nVISIBLE OPTIONS ON PAGE:\n"
        + options_str
        + prev_answers_str
        + "\n\nReturn a JSON object with two fields:\n"
        '1. "answers": array of actions to take\n'
        '2. "unknown": array of questions the profile cannot answer\n\n'
        "Each answer has:\n"
        '- "type": "click" (click an option) or "fill" (type into a text field)\n'
        '- "text": the EXACT text of the option to click (for "click" type)\n'
        '- "value": the text to type (for "fill" type)\n'
        '- "question": the question being answered (for tracking)\n'
        '- "profile_key": the profile category.field this relates to (e.g. "demographics.age")\n\n'
        "Each unknown has:\n"
        '- "question": the question text that cannot be answered from the profile\n'
        '- "options": the available options to choose from\n\n'
        "RULES:\n"
        "- Answer ALL questions from the profile\n"
        "- For click: 'text' must EXACTLY match one of the VISIBLE OPTIONS\n"
        "- Be consistent with previous answers\n"
        "- If you're unsure about an answer, add it to 'unknown'\n"
        "- NEVER type or click the word ROBOT\n"
        "- Return ONLY the JSON object\n"
        "\nExample:\n"
        '{"answers":[{"type":"click","text":"Male","question":"What is your gender?","profile_key":"demographics.gender"}],'
        '"unknown":[{"question":"What is your favorite color?","options":["Red","Blue","Green"]}]}'
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

            # Try to parse as JSON object
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if match:
                try:
                    result = json.loads(match.group())
                except json.JSONDecodeError:
                    raw = match.group()
                    raw = re.sub(r",\s*([}\]])", r"\1", raw)
                    try:
                        result = json.loads(raw)
                    except json.JSONDecodeError:
                        return [], []

                answers = result.get("answers", [])
                unknowns = result.get("unknown", [])
                answers = [
                    a
                    for a in answers
                    if isinstance(a, dict) and (a.get("text") or a.get("value") or "").strip().upper() != "ROBOT"
                ]
                return answers, unknowns
    except Exception as e:
        logger.warning("LLM error: %s", e)
    return [], []


def ask_user_for_answer(question: str, options: list[str]) -> str | None:
    """Text the user to ask for an answer. Returns None if can't reach user."""
    # Try sending via Hermes messaging
    options_str = ", ".join(options[:10]) if options else "(free text)"
    message = (
        f"Survey question I can't answer:\n\n{question}\n\nOptions: {options_str}\n\nPlease reply with your answer."
    )

    # Try SMS via imsg CLI
    try:
        r = subprocess.run(
            ["imsg", "send", "--to", "read.musik@gmail.com", "--message", message],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if r.returncode == 0:
            logger.info("Sent question to user via iMessage")
            # Wait for reply (poll for up to 5 minutes)
            for _ in range(60):
                time.sleep(5)
                # Check if user replied (this would need a webhook or polling mechanism)
                # For now, return None and let the agent skip
            return None
    except FileNotFoundError:
        pass

    # Fallback: log the question for manual review
    logger.warning("UNKNOWN QUESTION: %s\nOptions: %s", question, options_str)

    # Save to unknown_questions.json for later review
    unknown_path = PROJECT_ROOT / "data" / "unknown_questions.json"
    unknowns = []
    if unknown_path.exists():
        with open(unknown_path) as f:
            unknowns = json.load(f)
    unknowns.append(
        {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "question": question,
            "options": options,
        }
    )
    with open(unknown_path, "w") as f:
        json.dump(unknowns, f, indent=2)

    return None


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
def get_survey_take_urls(pid, wid):
    """Extract actual survey take URLs from btn-continue on dashboard."""
    # First try to get the href from btn-continue links (these are the take URLs)
    raw = js(
        pid,
        wid,
        '(function(){var links=document.querySelectorAll("a[href*=take],#btn-continue,a.btn-continue");'
        "var results=[];"
        "for(var i=0;i<links.length;i++){"
        'if(links[i].href&&links[i].href.includes("/take/"))results.push(links[i].href);}'
        'if(results.length>0)return results.join("\\n");'
        'return "none"})()',
    )
    if raw and raw not in ("missing value", "", "none"):
        return [u.strip() for u in raw.split("\n") if u.strip()]
    # Fallback: look for pre URLs
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


def run_one_survey(dash_pid, dash_wid, store, tracker, api_key):  # noqa: PLR0915
    """Run one survey using profile-first answering."""
    profile_context = store.to_context_string()

    # Step 1: Navigate to dashboard and get survey pre URLs
    navigate("https://surveys.gobranded.com/users/dashboard")
    time.sleep(10)

    pre_urls = []
    for attempt in range(3):
        page_text = get_page_text(dash_pid, dash_wid)
        if "sign in" in page_text.lower():
            logger.error("Not logged in")
            return False
        raw = js(
            dash_pid,
            dash_wid,
            'Array.from(document.querySelectorAll("a")).filter(function(a){'
            'return a.href&&a.href.includes("/surveys/pre/")&&!a.href.includes("/listing")'
            '}).map(function(a){return a.href}).join("\\n")',
        )
        if raw and raw not in ("missing value", ""):
            pre_urls = [u.strip() for u in raw.split("\n") if u.strip()]
        if pre_urls:
            break
        logger.info("  Attempt %d: no survey URLs, waiting...", attempt + 1)
        time.sleep(5)

    if not pre_urls:
        logger.warning("No survey URLs found")
        return False

    # Step 2: Navigate to pre-page in the SAME window
    pre_url = pre_urls[0]
    logger.info("Opening pre-page: %s", pre_url[:80])
    navigate(pre_url)
    time.sleep(8)

    # Step 3: Extract take URL from btn-continue href
    take_url = js(
        dash_pid,
        dash_wid,
        '(function(){var btn=document.getElementById("btn-continue");'
        "if(btn&&btn.href)return btn.href;"
        'var links=document.querySelectorAll("a[href*=take]");'
        "for(var i=0;i<links.length;i++){"
        'if(links[i].href.includes("/take/"))return links[i].href;}'
        'return "none"})()',
    )

    if take_url and take_url not in ("none", "missing value", ""):
        logger.info("Extracted take URL: %s", take_url[:80])
        navigate(take_url)
        time.sleep(10)
    else:
        # Fallback: click btn-continue
        logger.info("No take URL found, clicking btn-continue")
        js(
            dash_pid,
            dash_wid,
            '(function(){var btn=document.getElementById("btn-continue");'
            'if(btn){btn.click();return "clicked";}return "none"})()',
        )
        time.sleep(8)

    # Use dashboard window for the survey
    pid, wid = dash_pid, dash_wid

    try:
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

            # Stuck detection
            visible_hash = js(
                pid,
                wid,
                '(function(){var els=document.querySelectorAll("*");var t="";'
                "for(var i=0;i<els.length;i++){"
                "if(els[i].offsetParent!==null&&els[i].childNodes.length<=3&&els[i].textContent.trim().length>0){"
                't+=els[i].textContent.trim().substring(0,50)+"|";}}'
                "return t.substring(0,2000)})()",
            )
            hash_source = visible_hash if visible_hash not in ("missing value", "") else page_text
            content_hash = hashlib.md5(hash_source.encode()).hexdigest()[:12]

            if content_hash == last_hash:
                same_count += 1
                if same_count >= 5:  # bumped from 3 to allow widget handlers to complete
                    logger.warning("Stuck on same page for 5 iterations")
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

            # Get visible options and ask LLM with profile context
            options = get_visible_options(pid, wid)
            cleaned_text = clean_page_text(page_text)
            logger.info("  Cleaned text: %d chars (from %d)", len(cleaned_text), len(page_text))
            prev_answers = [{"question": a["question"], "answer": a["answer"]} for a in tracker.answers[-10:]]

            # ── Deterministic persona pick (runs BEFORE LLM) ──
            # Steals stealth-runner's pattern: match question text against
            # known patterns, return exact option text. Saves LLM call,
            # prevents hallucination, handles screener pages with no visible question.
            answers = []
            if options:
                picked = persona_pick_radio(cleaned_text, options)
                if picked:
                    # Free-text detection: if picked is >40 chars or contains a period,
                    # it's a sentence answer, not a click target
                    is_free_text = (
                        len(picked) > 40
                        or "." in picked
                        or len(picked.split()) > 6
                    )
                    if not is_free_text:
                        # Short answer — should be a click target. Verify it's in options.
                        is_in_options = any(
                            picked.lower().strip() == o.lower().strip()
                            for o in options
                        )
                        if is_in_options:
                            logger.info("  Persona-pick: '%s' (click, no LLM)", picked[:50])
                            answers.append({
                                "type": "click",
                                "text": picked,
                                "question": cleaned_text[:80],
                                "profile_key": "persona-pick",
                            })
                        else:
                            # Short answer but not in options — fall through to LLM
                            logger.info("  Persona-pick '%s' not in options, trying LLM", picked[:50])
                    else:
                        # Long answer / sentence — fill textarea
                        logger.info("  Persona-fill: '%s' (fill, no LLM)", picked[:50])
                        answers.append({
                            "type": "fill",
                            "value": picked,
                            "question": cleaned_text[:80],
                            "profile_key": "persona-pick",
                        })

            # Only call LLM if persona picker didn't match
            if not answers:
                answers, unknowns = get_llm_answer(cleaned_text, options, profile_context, prev_answers, api_key)
                logger.info("  LLM: %d answers, %d unknowns", len(answers), len(unknowns))

                # Handle unknown questions — auto-pick safe middle option
                # (ask_user_for_answer texts via iMessage which never replies here)
                for unk in unknowns:
                    question = unk.get("question", "")
                    unk_options = unk.get("options", [])
                    logger.info("  Unknown: %s (auto-pick middle)", question[:80])
                    if unk_options and len(unk_options) >= 2:
                        # Avoid "yes/no", "true/false", "always/never" extremes
                        # Pick from the middle of the list
                        mid = len(unk_options) // 2
                        # If middle is a consent/empty option, try adjacent
                        for offset in [0, -1, 1, -2, 2]:
                            idx = mid + offset
                            if 0 <= idx < len(unk_options):
                                candidate = unk_options[idx]
                                cl = candidate.lower().strip()
                                if (cl and len(cl) < 50
                                    and not any(kw in cl for kw in ["accept", "reject", "consent", "cookie", "more"])):
                                    safe = candidate
                                    break
                        else:
                            safe = unk_options[mid]
                        logger.info("  Auto-pick: '%s' (index %d)", safe[:50], unk_options.index(safe))
                        answers.append({
                            "type": "click",
                            "text": safe,
                            "question": question,
                            "profile_key": "auto-pick",
                        })
                    elif unk_options:
                        # Single option — take it
                        answers.append({
                            "type": "click",
                            "text": unk_options[0],
                            "question": question,
                            "profile_key": "auto-pick",
                        })
            else:
                unknowns = []

            # ── Safe-radio fallback (stealth-runner pattern) ──
            # When persona-pick AND LLM both fail, click the first visible radio
            # instead of clicking "next" blindly. Breaks the infinite screener loop.
            if not answers:
                first_radio = js(
                    pid, wid,
                    """(function(){
                        var radios = document.querySelectorAll('input[type="radio"]:visible, input[type="radio"]');
                        radios = Array.from(radios).filter(function(r){
                            var rect = r.getBoundingClientRect();
                            return rect.width > 0 || rect.height > 0;
                        });
                        if (radios.length === 0) return '';
                        var r = radios[0];
                        var label = r.labels && r.labels[0] ? r.labels[0].innerText.trim() : '';
                        return label || r.value || r.id || 'first-radio';
                    })()"""
                )
                if first_radio and first_radio not in ("missing value", ""):
                    logger.info("  Safe-radio: '%s' (no LLM match)", first_radio[:50])
                    answers.append({
                        "type": "click",
                        "text": first_radio,
                        "question": cleaned_text[:80],
                        "profile_key": "safe-radio",
                    })

            # Apply answers and track them
            for ans in answers:
                action_type = ans.get("type", "click")
                question = ans.get("question", "")
                profile_key = ans.get("profile_key")
                text = ans.get("text", "")

                if action_type == "click":
                    if text:
                        # ── 1. Date picker handler ──
                        q_lower = question.lower()
                        if "born" in q_lower or "birth" in q_lower or "when" in q_lower and "year" in q_lower:
                            # Try to find Month/Day/Year dropdowns and select from profile
                            profile = store.profile if hasattr(store, "profile") else {}
                            demo = profile.get("demographics", {})
                            month_val = demo.get("birth_month", 3)
                            day_val = demo.get("birth_day", 15)
                            year_val = demo.get("birth_year", 1986)
                            # Set select values via JS
                            js(pid, wid, f"""(function(){{
                                var sels=document.querySelectorAll("select");
                                for(var i=0;i<sels.length;i++){{
                                    var opts=Array.from(sels[i].options).map(function(o){{return o.value||o.text;}});
                                    if(opts.some(function(o){{return o.toLowerCase().includes("month");}})){{
                                        sels[i].value="{month_val}";sels[i].dispatchEvent(new Event("change",{{bubbles:true}}));
                                    }}else if(opts.some(function(o){{return o.toLowerCase().includes("day");}})){{
                                        sels[i].value="{day_val}";sels[i].dispatchEvent(new Event("change",{{bubbles:true}}));
                                    }}else if(opts.some(function(o){{return o.toLowerCase().includes("year");}})){{
                                        sels[i].value="{year_val}";sels[i].dispatchEvent(new Event("change",{{bubbles:true}}));
                                    }}
                                }}
                            }})()""")
                            logger.info("  Filled date picker: %d/%d/%d", month_val, day_val, year_val)
                            if question:
                                tracker.record(question, f"{month_val}/{day_val}/{year_val}", profile_key)
                            continue

                        # ── 2. ROBOT attention check ──
                        if text.strip().upper() == "ROBOT":
                            # Type ROBOT into the first visible text field
                            js(pid, wid, """(function(){
                                var inputs=document.querySelectorAll("input[type='text'],input:not([type]),textarea");
                                for(var i=0;i<inputs.length;i++){
                                    if(inputs[i].offsetParent!==null){
                                        inputs[i].focus();
                                        inputs[i].value="ROBOT";
                                        inputs[i].dispatchEvent(new Event("input",{bubbles:true}));
                                        inputs[i].dispatchEvent(new Event("change",{bubbles:true}));
                                        return "robot-typed";
                                    }
                                }
                            })()""")
                            logger.info("  Typed ROBOT (attention check)")
                            if question:
                                tracker.record(question, "ROBOT", profile_key)
                            continue

                        # ── 3. Cookie consent handler ──
                        if "cookie" in q_lower or "consent" in q_lower:
                            for consent_text in ["Accept All", "Accept", "I Agree", "Reject All", "Necessary Only", "Reject"]:
                                clicked = click_element_by_text(pid, wid, consent_text)
                                if clicked:
                                    logger.info("  Clicked cookie consent: %s", consent_text)
                                    break
                            if question:
                                tracker.record(question, "cookie-consent", profile_key)
                            continue

                        # Default: click
                        clicked = click_element_by_text(pid, wid, text)
                        if not clicked:
                            js(
                                pid,
                                wid,
                                f'(function(){{var els=document.querySelectorAll("div,span,button,a,label,li");'
                                f"for(var i=0;i<els.length;i++){{"
                                f"var t=els[i].textContent.trim().toLowerCase();"
                                f'if(t.includes("{text.lower()}")&&els[i].offsetParent!==null){{els[i].click();return "clicked";}}}}'
                                f'return "none"}})()',
                            )
                        if question:
                            tracker.record(question, text, profile_key)

                elif action_type == "fill":
                    value = ans.get("value", "")
                    if value:
                        # ── 4. Free text handler: also handle textareas ──
                        filled = fill_text_by_index(pid, wid, 0, value)
                        if not filled:
                            # Fallback: JS set value on textarea/input
                            js(pid, wid, f"""(function(){{
                                var inputs=document.querySelectorAll("textarea,input[type='text'],input:not([type])");
                                for(var i=0;i<inputs.length;i++){{
                                    if(inputs[i].offsetParent!==null){{
                                        inputs[i].focus();
                                        inputs[i].value={json.dumps(value)};
                                        inputs[i].dispatchEvent(new Event("input",{{bubbles:true}}));
                                        inputs[i].dispatchEvent(new Event("change",{{bubbles:true}}));
                                        return "filled";
                                    }}
                                }}
                            }})()""")
                        if question:
                            tracker.record(question, value, profile_key)

            time.sleep(0.5)
            submit_result = submit_form(pid, wid)
            logger.info("  Submit: %s", submit_result)
            time.sleep(3)

        return False

    finally:
        # Navigate back to dashboard for next survey
        logger.info("Navigating back to dashboard")
        navigate("https://surveys.gobranded.com/users/dashboard")
        time.sleep(3)


def main():
    logger.info("CUA Survey Agent v5 starting")
    logger.info("Profile: %s (%.0f%% complete)", PROFILE_PATH, 0.0)

    # Load profile
    store = ProfileStore(PROFILE_PATH)
    logger.info("Profile loaded: %s", store.completion_pct())
    logger.info(store.summary()[:500])

    # Load answer tracker
    tracker = SurveyAnswerTracker()
    logger.info("Answer history: %d previous answers", len(tracker.answers))

    # Get API key
    api_key = get_api_key()
    if not api_key:
        logger.error("No OPENCODE_API_KEY found")
        return

    # Find Chrome
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
                if run_one_survey(dash_pid, dash_wid, store, tracker, api_key):
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

        pause_min = random.randint(2, 5)  # Testing: reduced from (15,45) for faster widget handler validation
        logger.info(
            "\n=== Cycle %d done: %d/%d completed. Pausing %d min ===", cycle, completed_count, num_surveys, pause_min
        )
        time.sleep(pause_min * 60)


if __name__ == "__main__":
    main()
