"""Browser-harness backend — controls a dedicated Chrome instance via CDP.

Launches a SEPARATE Chrome process with its own profile directory.
The user's main Chrome is never touched. Login sessions persist across
runs because the profile directory is reused.

Set env var ``CHROME_BIN`` to override the Chrome binary path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from ..answering import llm_answers as llm_ans
from ..checkpoint import save as cp_save, load as cp_load, clear as cp_clear, increment_retry as cp_retry
from ..cookie_sync import sync_google_cookies
from .backend import AgentResult, SurveyAgentBackend

logger = logging.getLogger(__name__)

CHROME_BINARIES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
]

AGENT_CHROME_PROFILE = Path(os.environ.get("CHROME_PROFILE", str(Path(__file__).resolve().parent.parent.parent / "data" / "chrome_agent")))
CDP_PORT = int(os.environ.get("CDP_PORT", "19333"))  # Override via env for parallel instances

# JS code for reading interactive elements from a page — reused by page reader
_PAGE_ELEMENTS_JS = (
    "(() => {"
    "  const els = [];"
    "  document.querySelectorAll('input, select, textarea, button, [role=button], [role=radio], [role=option], [role=checkbox]').forEach(el => {"
    "    if (el.offsetParent === null && el.type !== 'radio' && el.type !== 'checkbox') return;"
    "    const tag = el.tagName.toLowerCase();"
    "    let text = (el.textContent || el.value || '').trim().substring(0, 100);"
    "    if ((el.type === 'radio' || el.type === 'checkbox') && !text) {"
    "      text = el.getAttribute('aria-label') || '';"
    "      if (!text && el.id) { const lbl = document.querySelector('label[for=\"' + el.id + '\"]'); if (lbl) text = lbl.textContent.trim(); }"
    "      if (!text) { const p = el.closest('label') || el.parentElement; if (p) text = (p.textContent || '').trim().replace(/\\s+/g, ' ').substring(0, 100); }"
    "    }"
    "    const type = el.type || el.getAttribute('role') || '';"
    "    const name = el.name || '';"
    "    const id = el.id || '';"
    "    const cls = (el.className || '').substring(0, 60);"
    "    const checked = el.checked || false;"
    "    const placeholder = el.placeholder || '';"
    "    const opts = tag === 'select' ? Array.from(el.options).map(o => ({v: o.value, t: o.text.trim()})).slice(0, 20) : [];"
    "    els.push({tag, type, name, id, text, cls, checked, placeholder, opts});"
    "  });"
    "  return JSON.stringify(els.slice(0, 100));"
    "})()"
)


def _find_chrome() -> str:
    for path in CHROME_BINARIES:
        if Path(path).exists():
            return path
        result = subprocess.run(["which", path], capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return result.stdout.strip()
    raise FileNotFoundError("Chrome not found. Install Google Chrome or set CHROME_BIN env var.")


def _is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


# ── LLM code fixup ──────────────────────────────────────────────────────

_HELPER_FIXES: list[tuple[str, str]] = [
    (r"\bcurrent_url\s*\(\s*\)", "current_tab()['url']"),
    (r"\bnavigate\s*\(", "goto_url("),
]


def _fix_bad_helpers(code: str) -> str:
    for pattern, replacement in _HELPER_FIXES:
        code = re.sub(pattern, replacement, code)
    if "def main(" in code:
        after_main_def = code.split("def main(")[1]
        if "main()" not in after_main_def:
            code = code.rstrip() + "\n\nmain()\n"
    return code


def _wrap_with_safety_net(code: str) -> str:
    if "def main(" in code:
        code = re.sub(r"\nmain\(\)\s*$", "", code)
        code = code.rstrip() + "\n\n"
        code += "import traceback as _tb\n"
        code += "try:\n"
        code += "    main()\n"
        code += "except Exception as _e:\n"
        code += '    print(f"RESULT: error in main: {_e}")\n'
        code += "    _tb.print_exc()\n"
        return code
    indented = "\n".join("    " + line if line.strip() else "" for line in code.split("\n"))
    return (
        "import sys, traceback as _tb\n"
        "try:\n" + indented + "\nexcept Exception as _e:\n"
        '    print(f"RESULT: error {_e}")\n'
        "    _tb.print_exc()\n"
        "    sys.exit(1)\n"
    )


def _read_zshrc_api_keys() -> dict[str, str]:
    """Read API keys from .zshrc (env vars aren't always exported to Python)."""
    keys: dict[str, str] = {}
    zshrc = Path(os.path.expanduser("~/.zshrc"))
    if not zshrc.exists():
        return keys
    try:
        text = zshrc.read_text()
        for var_name in ["OPENCODE_API_KEY", "FREEMODEL_API_KEY"]:
            for line in text.splitlines():
                if var_name in line and "=" in line:
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        keys[var_name] = val
                    break
    except OSError:
        pass
    return keys


# ── Browser-harness runner ───────────────────────────────────────────────


async def _run_harness(script: str, timeout: float = 120, cdp_url: str | None = None) -> tuple[str, int]:
    env = os.environ.copy()
    if cdp_url:
        env["BU_CDP_URL"] = cdp_url
    try:
        proc = await asyncio.create_subprocess_exec(
            "browser-harness",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(input=script.encode("utf-8")), timeout=timeout)
        return stdout.decode("utf-8", errors="replace"), proc.returncode or 0
    except TimeoutError:
        proc.kill()
        return "TIMEOUT", 1


# ── Pre-built scripts ────────────────────────────────────────────────────


def _build_login_script(site_url: str, email: str, password: str) -> str:
    """Build harness script for login. Uses temp file to avoid escaping hell."""
    import tempfile  # noqa: PLC0415

    email_esc = email.replace("\\", "\\\\").replace("'", "\\'")
    pw_esc = password.replace("\\", "\\\\").replace("'", "\\'")

    # Build the harness script as a plain string
    script = f"""import time

ensure_real_tab()
goto_url("{site_url}")
wait_for_load()
time.sleep(2)

url = current_tab()["url"]
text = js("document.body ? document.body.innerText.substring(0, 500) : ''")
if "dashboard" in url or "/offers" in url or "my points" in text.lower():
    print("RESULT: already_logged_in")
else:
    js("document.querySelectorAll('a').forEach(function(el){{if(el.textContent.trim()==='Log In') el.click()}})")
    wait_for_load()
    time.sleep(2)
    js("document.querySelectorAll('a,button,span,div,p').forEach(function(el){{var t=(el.textContent||'').trim().toLowerCase(); if(t==='sign in with email') el.click()}})")
    time.sleep(2)

    email_filled = False
    for sel in ['#UserEmail', "input[name='email']", "input[type='email']"]:
        try:
            js("var el=document.querySelector('" + sel + "'); if(el){{el.focus(); el.value='{email_esc}'; el.dispatchEvent(new Event('input',{{bubbles:true}})); el.dispatchEvent(new Event('change',{{bubbles:true}}));}}")
            email_filled = True
            print("EMAIL: filled via " + sel)
            break
        except Exception:
            continue
    if not email_filled:
        print("ERROR: email_field_not_found")

    time.sleep(0.5)

    pw_filled = False
    for sel in ['#UserPassword', "input[name='password']", "input[type='password']"]:
        try:
            js("var el=document.querySelector('" + sel + "'); if(el){{el.focus(); el.value='{pw_esc}'; el.dispatchEvent(new Event('input',{{bubbles:true}})); el.dispatchEvent(new Event('change',{{bubbles:true}}));}}")
            pw_filled = True
            print("PASSWORD: filled")
            break
        except Exception:
            continue
    if not pw_filled:
        print("ERROR: password_field_not_found")

    time.sleep(0.5)
    js("document.querySelector('input[type=submit]')?.click()")
    wait_for_load()
    time.sleep(3)

    final_url = current_tab()["url"]
    print("FINAL_URL:", final_url)

    if "dashboard" in final_url or "survey" in final_url.lower():
        print("RESULT: login_success")
    elif "sign_in" in final_url or "login" in final_url:
        print("RESULT: login_failed_still_on_signin")
    else:
        print("RESULT: login_unknown_check_page")
"""

    # Write to temp file and return the harness load command
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, prefix="login_") as f:
        f.write(script)
        script_path = f.name

    return f'exec(open("{script_path}").read())\n'


def _build_discover_script(dashboard_url: str) -> str:
    js_find_surveys = (
        "(() => { "
        "var s=[],l=document.querySelectorAll('a.take-survey-redirect, a[href*=\"/surveys/pre/\"], a[href*=\"/surveys/take/\"]'); "
        "for(var i=0;i<l.length;i++){ "
        "var a=l[i],c=a.closest('[class*=survey-card]')||a.parentElement; "
        "var d=parseInt((c?c.getAttribute('data-award'):'0')||'0'); "
        "if(d>0){s.push({href:a.href,pts:d});continue} "
        "var x=(c?c.textContent:a.textContent).trim().toUpperCase(); "
        "var p=x.indexOf('PTS');if(p<0)p=x.indexOf('POINTS');if(p<0)continue; "
        "var b=x.substring(0,p).trim().split(/[\\s,]+/); "
        "var n=parseInt(b[b.length-1]); "
        "if(!isNaN(n)&&n>0)s.push({href:a.href,pts:n}); "
        "} "
        "s.sort(function(a,b){return b.pts-a.pts}); "
        "return JSON.stringify(s); })()"
    )

    # Embed JS directly using JSON encoding to avoid escaping issues
    import json as _json  # noqa: PLC0415

    lines = [
        "import time, json",
        "ensure_real_tab()",
        # Navigate to /members first (triggers SPA survey AJAX on redirect/load)
        'goto_url(' + json.dumps(dashboard_url) + ')',
        "wait_for_load()",
        "time.sleep(12)",  # Wait for AJAX survey cards to load fully
        "info = page_info()",
        'print("PAGE_INFO:", str(info)[:500])',
        # Read JS from embedded JSON string, then run it
        '_js_code = ' + json.dumps(js_find_surveys),
        "surveys = js(_js_code)",
        'print("SURVEYS_RAW:", surveys)',
        'print("RESULT: discover_complete")',
    ]
    return "\n".join(lines) + "\n"


def _build_balance_script(dashboard_url: str) -> str:
    js_find_balance = (
        "(() => { "
        "const sels = ['.balance','.points','.earnings','[class*=\\\"balance\\\"]','[class*=\\\"point\\\"]']; "
        "for (const sel of sels) { "
        "const el = document.querySelector(sel); "
        "if (el) return el.textContent?.trim(); } "
        "const body = document.body?.textContent || ''; "
        "const m = body.match(/[\\\\$]?\\\\d+[.,]?\\\\d*/); "
        "return m ? m[0] : 'balance_not_found'; })()"
    )

    return f"""
import time

ensure_real_tab()
goto_url("{dashboard_url}")
wait_for_load()
time.sleep(2)

balance = js("{js_find_balance}")
print("BALANCE:", balance)
print("RESULT: balance_check_complete")
"""


# ── Main Backend ─────────────────────────────────────────────────────────


def _css_escape(s: str) -> str:
    """Escape a string for use in CSS selectors."""
    if not s:
        return s
    # First escape special CSS characters
    for ch in [".", ":", "[", "]", "(", ")", "#", ",", "+", "~", ">", " ", "'", '"']:
        s = s.replace(ch, "\\" + ch)
    # Then handle leading digit (CSS escape: \3<digit> + space)
    if s and s[0].isdigit():
        s = "\\3" + s[0] + " " + s[1:]
    return s


# ── Question-aware answer selection ─────────────────────────────────────

_PROFILE = {
    # Demographics (from profile.json)
    "gender": "male",
    "age": "39",
    "marital": "domestic partnership",
    "income": "50000-74999",
    "education": "trade/vocational",
    "employment": "self-employed",
    "race": "black",
    "ethnicity": "african american",
    "household": "5",
    "children": "yes",
    "homeowner": "rent",
    "smoker": "former",
    "alcohol": "never",
    "hispanic": "no",
    "country": "united states",
    "state": "texas",
    "zip": "78736",
    "city": "austin",
    "language": "english",
    "device": "laptop",
    "laptop": "yes",
    "desktop": "yes",
    "tablet": "no",
    "smartphone": "yes",
    "internet": "broadband",
    "social media": "yes",
    "online shopping": "yes",
    "grocery": "yes",
    "restaurant": "no",
    "fast food": "no",
    "exercise": "yes",
    "health": "very good",
    "diet": "no",
    "vegetarian": "no",
    "vegan": "no",
    "pet": "no",
    "dog": "no",
    "cat": "no",
    "car": "yes",
    "vehicle": "yes",
    "insurance": "no",
    "health insurance": "no",
    "bank": "yes",
    "credit card": "yes",
    "investment": "no",
    "retirement": "no",
    "travel": "yes",
    "airline": "yes",
    "hotel": "yes",
    "streaming": "yes",
    "netflix": "yes",
    "amazon": "yes",
    "youtube": "yes",
    "gaming": "yes",
    "beer": "no",
    "wine": "no",
    "spirit": "no",
    "coffee": "yes",
    "tea": "no",
    "soda": "no",
    "energy drink": "no",
    "organic": "no",
    "cereal": "no",
    "snack": "yes",
    "frozen": "yes",
    "dairy": "yes",
    "meat": "yes",
    "breakfast": "yes",
    "cook": "yes",
    "bake": "no",
    "decision": "primary",
    "purchase": "yes",
    "brand": "yes",
    "influence": "yes",
    "opinion": "yes",
    "survey": "yes",
    "feedback": "yes",
    "recommend": "yes",
    "satisfied": "satisfied",
    "experience": "positive",
    "frequency": "monthly",
    "often": "monthly",
    "how often": "monthly",
    "how many": "2-3",
    "how much": "moderate",
    "agree": "agree",
    "disagree": "disagree",
    "strongly agree": "agree",
    "strongly disagree": "disagree",
    "neither": "neither agree nor disagree",
    "scale": "4",
    "rate": "4",
    "satisfaction": "satisfied",
    "quality": "good",
    "value": "good value",
    "price": "fair",
    "expensive": "no",
    "cheap": "no",
    "afford": "yes",
    "pay": "yes",
    "spend": "moderate",
    "budget": "yes",
    "debt": "no",
    "loan": "no",
    "mortgage": "no",
    "rent": "yes",
    "student loan": "no",
    "medical": "no",
    "prescription": "no",
    "doctor": "yes",
    "dentist": "yes",
    "therapy": "no",
    "mental health": "good",
    "stress": "low",
    "sleep": "good",
    "anxiety": "no",
    "depression": "no",
    "pain": "no",
    "allergy": "no",
    "chronic": "no",
    "disability": "no",
    "pregnant": "no",
    "nursing": "no",
    "veteran": "no",
    "military": "no",
    "immigrant": "no",
    "citizen": "yes",
    "legal": "yes",
    "tax": "yes",
    "vote": "yes",
    "volunteer": "no",
    "charity": "no",
    "donate": "no",
    "church": "no",
    "religious": "no",
    "spiritual": "no",
    "political": "independent",
    "liberal": "no",
    "conservative": "no",
    "democrat": "no",
    "republican": "no",
    "work from home": "yes",
    "remote": "yes",
    "freelance": "no",
    "self-employed": "yes",
    "business owner": "yes",
    "manager": "yes",
    "supervisor": "no",
    "executive": "no",
    "student": "no",
    "retired": "no",
    "homemaker": "no",
    "unemployed": "no",
    "part-time": "no",
    "full-time": "yes",
    "contract": "no",
    "temporary": "no",
    "seasonal": "no",
    "intern": "no",
}


def _pick_radio_answer(page_text: str, options: list[dict]) -> dict:
    """Pick the best radio button answer based on the question text and profile."""
    for key, value in _PROFILE.items():
        if key in page_text:
            for opt in options:
                label = (opt.get("label") or "").lower()
                if value in label:
                    return opt

    if "gender" in page_text or "sex" in page_text:
        for opt in options:
            if "male" in (opt.get("label") or "").lower() and "female" not in (opt.get("label") or "").lower():
                return opt

    if "age" in page_text:
        for opt in options:
            label = (opt.get("label") or "").lower()
            if "35" in label or "39" in label or "25-44" in label or "30-44" in label:
                return opt

    sensitive = ["income", "salary", "religion", "political", "health", "medical", "sexual", "mental"]
    if any(kw in page_text for kw in sensitive):
        for opt in options:
            if "prefer not" in (opt.get("label") or "").lower():
                return opt

    # If question is about ownership/purchase/frequency, prefer "yes"/positive
    positive_kws = ["own", "have", "use", "purchase", "buy", "subscribe", "member", "participate", "involve"]
    if any(kw in page_text for kw in positive_kws):
        for opt in options:
            label = (opt.get("label") or "").lower()
            if label.strip() in ("yes", "yeah", "yep", "true", "correct"):
                return opt

    for opt in options:
        if "prefer not" in (opt.get("label") or "").lower():
            return opt

    # Default: pick middle option (often "neutral" in Likert scales)
    return options[len(options) // 2] if options else options[0]


class BrowserHarnessBackend(SurveyAgentBackend):
    """Browser backend powered by browser-harness + dedicated Chrome instance."""

    def __init__(
        self,
        llm_provider: str = "gemini",
        llm_model: str | None = None,
        llm_api_key: str | None = None,
        llm_base_url: str | None = None,
        headless: bool = False,
        session_dir: str | Path | None = None,
        google_email: str = "",
        paypal_email: str = "",
        login_email: str = "",
        login_password: str = "",
    ):
        self._google_email = google_email
        self._paypal_email = paypal_email
        self._login_email = login_email or google_email
        self._login_password = login_password
        self._llm_provider = llm_provider
        self._llm_model = llm_model
        self._llm_api_key = llm_api_key
        self._llm_base_url = llm_base_url
        self._headless = headless
        self._chrome_proc: subprocess.Popen | None = None
        self._cdp_url = f"http://127.0.0.1:{CDP_PORT}"

    async def start(self) -> None:
        chrome_bin = os.environ.get("CHROME_BIN") or _find_chrome()
        AGENT_CHROME_PROFILE.mkdir(parents=True, exist_ok=True)

        # If Chrome is already running with CDP on our port, reuse it
        if _is_port_open(CDP_PORT):
            logger.info("Chrome already running on port %d — reusing existing instance (preserves survey session)", CDP_PORT)
            # Still sync cookies to keep session fresh
            try:
                n = sync_google_cookies()
                logger.info("Cookie sync: %d cookies refreshed", n)
            except Exception as e:
                logger.warning("Cookie sync failed (non-fatal): %s", e)
            logger.info("Chrome window kept visible for screenshot access")
            result, rc = await _run_harness(
                "ensure_real_tab()\nprint('Browser ready')",
                cdp_url=self._cdp_url,
            )
            return

        # Port is not open — need to launch a fresh Chrome
        logger.warning("Port %d not in use — launching fresh Chrome instance", CDP_PORT)

        # Kill any stale Chrome processes using our profile (gracefully)
        try:
            import psutil as _ps  # noqa: PLC0415
            for proc in _ps.process_iter(["pid", "name", "cmdline"]):
                try:
                    cmdline = " ".join(str(s) for s in (proc.info.get("cmdline") or []))
                    if "Google Chrome" in proc.info.get("name", "") and str(AGENT_CHROME_PROFILE) in cmdline:
                        logger.info("Stopping stale Chrome pid=%d gracefully...", proc.info["pid"])
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except _ps.TimeoutExpired:
                            logger.warning("Chrome pid=%d didn't exit gracefully, killing", proc.info["pid"])
                            proc.kill()
                        logger.info("Stopped stale Chrome pid=%d", proc.info["pid"])
                except (_ps.NoSuchProcess, _ps.AccessDenied):
                    pass
        except ImportError:
            subprocess.run(f"pkill -f '{AGENT_CHROME_PROFILE}' 2>/dev/null", shell=True, timeout=5)
        await asyncio.sleep(2)

        # Sync Google OAuth cookies from user's real Chrome profile
        try:
            synced = sync_google_cookies()
            if synced > 0:
                logger.info("Synced %d Google/Prime Opinion cookies to agent Chrome profile", synced)
        except Exception as e:
            logger.warning("Cookie sync failed (non-fatal): %s", e)

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
            # Iframe access: allow contentDocument to work cross-origin
            "--disable-web-security",
            "--disable-site-isolation-trials",
            "--disable-features=IsolateOrigins,site-per-process",
        ]
        if self._headless:
            args.append("--headless=new")

        logger.info("Launching dedicated Chrome: %s", " ".join(args[:4]))
        self._chrome_proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(20):
            if _is_port_open(CDP_PORT):
                break
            await asyncio.sleep(0.5)
        else:
            logger.warning("Chrome may not have started on port %d", CDP_PORT)

        # Keep Chrome visible (needed for cua-driver CAPTCHA screenshots)
        logger.info("Chrome window kept visible for screenshot access")

        # Patch navigator.webdriver to hide automation from survey providers
        try:
            await _run_harness(
                # Run on the blank tab we just opened
                'js("Object.defineProperty(navigator, \'webdriver\', {get: () => undefined})")',
                cdp_url=self._cdp_url,
            )
        except Exception:
            pass

        output, rc = await _run_harness("print(page_info())", timeout=15, cdp_url=self._cdp_url)
        if rc != 0:
            logger.warning("browser-harness connection check failed: %s", output[:200])
        else:
            logger.info("Agent Chrome connected: %s", output.strip()[:80])

        # NOTE: Do NOT close tabs here — on macOS Chrome shares a process,
        # so closing tabs would kill the user's tabs too.

        # Suppress popups/alerts that block the agent
        await _run_harness(
            "js('window.alert = function(){}; window.confirm = function(){return true}; window.prompt = function(){return null}')"
            "\nprint('RESULT: popups_suppressed')",
            timeout=10,
            cdp_url=self._cdp_url,
        )

    async def stop(self) -> None:
        if self._chrome_proc:
            logger.info("Leaving Chrome running (pid=%d) for session persistence", self._chrome_proc.pid)
            self._chrome_proc = None
        else:
            logger.info("Chrome already running (reused), leaving it alive")

    async def login(self, site_url: str, site_name: str) -> AgentResult:
        # Handle region selector pages FIRST (YouGov, EurekaSurveys, etc.)
        # This must happen before any other check since region pages don't
        # have "dashboard" or "Sign in with Google" patterns
        region_handled = await self._handle_region_selector(site_url, site_name)
        if region_handled:
            return await self.login(site_url, site_name)  # Retry after region selection

        # Navigate to the target site first so we can check its actual login state
        # (don't trust whatever tab Chrome happens to have open)
        nav_check, _ = await _run_harness(
            f"""goto_url("{site_url}")
wait_for_load()
import time
time.sleep(3)
url = current_tab()["url"]
text = js("document.body ? document.body.innerText.substring(0, 500) : ''")
has_dashboard = ("dashboard" in url.lower() or "my points" in text.lower() or "take survey" in text.lower())
is_login_page = ("login" in url.lower() or "sign_in" in url.lower() or "sign-in" in url.lower())
print("URL:", url)
print("HAS_DASHBOARD:", "yes" if has_dashboard else "no")
print("IS_LOGIN_PAGE:", "yes" if is_login_page else "no")
""",
            timeout=20,
            cdp_url=self._cdp_url,
        )
        if "HAS_DASHBOARD: yes" in nav_check and "IS_LOGIN_PAGE: no" in nav_check:
            logger.info("Already logged in to %s (session persisted)", site_name)
            return AgentResult(success=True, output="already_logged_in")

        # Determine login method: Google OAuth vs email/password
        # Sites like Prime Opinion use "Sign in with Google" — needs google_email only
        use_google_oauth = (
            bool(self._google_email)
            and not self._login_password
        )
        # Also check if login page contains "Continue with Google" / "Sign in with Google"
        oauth_check = await _run_harness(
            f"""goto_url("{site_url}")
wait_for_load()
import time
time.sleep(2)
text = js("document.body ? document.body.innerText.substring(0, 2000) : ''").lower()
has_google = ('sign in with google' in text or 'continue with google' in text
              or 'log in with google' in text)
print('OAUTH_CHECK:', 'yes' if has_google else 'no')""",
            timeout=20,
            cdp_url=self._cdp_url,
        )
        if "OAUTH_CHECK: yes" in oauth_check:
            use_google_oauth = True

        if use_google_oauth:
            logger.info("Using Google OAuth login for %s", site_name)
            # First try navigating directly to the dashboard (cookies should auto-login)
            if "primeopinion" in site_url:
                dashboard_url = site_url.rstrip("/") + "/surveys"
            elif "gobranded" in site_url:
                dashboard_url = site_url.rstrip("/") + "/members"
            else:
                dashboard_url = site_url.rstrip("/") + "/users/dashboard"
            nav_check, _ = await _run_harness(
                f"""goto_url("{dashboard_url}")
wait_for_load()
import time
time.sleep(3)
url = current_tab()["url"]
text = js("document.body ? document.body.innerText.substring(0, 500) : ''")
print("DIRECT_URL:", url)
has_dashboard = ("dashboard" in url.lower() or "survey" in url.lower() or "my points" in text.lower()
                 or "log out" in text.lower() or "my account" in text.lower())
print("HAS_DASHBOARD:", "yes" if has_dashboard else "no")""",
                timeout=20,
                cdp_url=self._cdp_url,
            )
            if "HAS_DASHBOARD: yes" in str(nav_check):
                logger.info("Already logged in to %s via direct dashboard access", site_name)
                return AgentResult(success=True, output="already_logged_in")

            # Fall through to Google OAuth button click flow
            return await self._login_google_oauth(site_url, site_name)

        # Fall through to email/password login
        if not self._login_email or not self._login_password:
            return AgentResult(success=False, error="No login credentials. Run: survey-agent credentials")

        # Fallback: return error if no login method matched
        return AgentResult(
            success=False,
            error=f"No working login method for {site_name}. "
                  f"Use: survey-agent credentials or survey-agent login -s {site_name.split()[0].lower()}"
        )

    async def _handle_region_selector(self, site_url: str, site_name: str) -> bool:
        """Handle region selection pages common to international survey sites.

        Detects pages with region buttons (US, UK, etc.) and clicks the US option
        (or the first available region). Returns True if a region was selected.
        """
        logger.info("Checking for region selector on %s", site_name)
        # First navigate to the site and wait for any redirects to complete
        await _run_harness(
            f"""goto_url("{site_url}")
wait_for_load()
import time
time.sleep(3)""",
            timeout=15,
            cdp_url=self._cdp_url,
        )
        region_script = """() => {
            const text = document.body ? document.body.innerText.toLowerCase() : '';
            const isRegionPage = (
                text.includes('select your country') ||
                text.includes('choose your region') ||
                text.includes('select region') ||
                text.includes('country selection') ||
                text.includes('where are you from') ||
                text.includes('select your location') ||
                text.includes('change country') ||
                text.includes('change your country') ||
                text.includes('select your language') ||
                text.includes('pick your country')
            );
            if (!isRegionPage) return { isRegion: false };

            // Look for US/United States first
            const regionPatterns = [
                /united\\s*states/i, /\\bus\\b/i, /\\busa\\b/i, /america/i
            ];
            const allLinks = document.querySelectorAll('a, button, [role=link], [role=button], li, div');
            for (const pattern of regionPatterns) {
                for (const el of allLinks) {
                    const elText = (el.textContent || '').trim();
                    if (pattern.test(elText) && elText.length < 50) {
                        return { isRegion: true, selector: getSelector(el), text: elText };
                    }
                }
            }
            // If no US match, find any region-like link
            for (const el of allLinks) {
                const elText = (el.textContent || '').trim();
                if (elText.length > 0 && elText.length < 50 && /^[A-Z][a-z]+(\\s[A-Z][a-z]+)?$/.test(elText)) {
                    return { isRegion: true, selector: getSelector(el), text: elText };
                }
            }
            return { isRegion: true, selector: null };

            function getSelector(el) {
                if (el.id) return '#' + el.id;
                if (el.name) return '[name="' + el.name + '"]';
                if (el.className && typeof el.className === 'string') {
                    return '.' + el.className.split(' ')[0];
                }
                return null;
            }
        }"""
        region_output = await _run_harness(
            f"region = {region_script}\nprint('REGION:', region)",
            timeout=15,
            cdp_url=self._cdp_url,
        )
        if "REGION: {'isRegion': False" in region_output or "REGION:{'isRegion':False" in region_output:
            return False
        if "REGION: {'isRegion': True, 'selector': None" in region_output:
            logger.info("%s region page detected but no clickable region found", site_name)
            return False
        if "'isRegion': True" not in region_output:
            return False

        # Extract selector and text
        import re as _re
        sel_text = region_output[0] if isinstance(region_output, tuple) else str(region_output)
        sel_match = _re.search(r"'selector':\s*'([^']+)'", sel_text)
        text_match = _re.search(r"'text':\s*'([^']+)'", sel_text)
        if not sel_match:
            logger.info("%s region page detected but could not extract selector", site_name)
            return False
        selector = sel_match.group(1)
        region_text = text_match.group(1) if text_match else "US"
        logger.info("Selecting region %r for %s (selector: %s)", region_text, site_name, selector)

        # Click the region
        click_script = f"""() => {{
            const el = document.querySelector("{selector}");
            if (el) {{
                el.click();
                return true;
            }}
            return false;
        }}"""
        click_result = await _run_harness(
            f"clicked = {click_script}\nprint('CLICKED:', clicked)",
            timeout=10,
            cdp_url=self._cdp_url,
        )
        if "CLICKED: True" in click_result:
            await asyncio.sleep(3)  # Wait for redirect
            return True
        return False

    async def _fetch_email_verification_code(
        self,
        sender_pattern: str | None = None,
        subject_pattern: str | None = None,
        timeout: int = 60,
    ) -> str | None:
        """Fetch a verification code from Gmail via IMAP.

        Used when a site sends a verification code via email during login.
        Requires Gmail credentials to be configured via `survey-agent email setup`.
        """
        try:
            from ..email.verification import (  # noqa: PLC0415
                GmailVerificationReader, get_gmail_credentials,
            )
            creds = get_gmail_credentials()
            if not creds:
                logger.warning("No Gmail credentials configured — cannot fetch verification code")
                return None
            email_addr, app_password = creds
            with GmailVerificationReader(email_addr, app_password) as reader:
                return reader.find_verification_code(
                    sender_pattern=sender_pattern,
                    subject_pattern=subject_pattern,
                    timeout=timeout,
                    poll_interval=5,
                )
        except Exception as e:
            logger.error(f"Gmail verification fetch failed: {e}")
            return None

    async def _handle_email_code_entry(self, page_context: dict, timeout: int = 60) -> bool:
        """Detect an email code entry page and auto-fill using Gmail.

        Looks for input fields with names/placeholders like 'code', 'verification',
        'otp', 'pin'. Polls Gmail for a matching code, then types it.
        Returns True if a code was successfully entered.
        """
        # Detect email code input
        detect_script = """() => {
            const inputs = document.querySelectorAll('input');
            for (const el of inputs) {
                const name = (el.name || '').toLowerCase();
                const placeholder = (el.placeholder || '').toLowerCase();
                const id = (el.id || '').toLowerCase();
                const type = (el.type || '').toLowerCase();
                if (type === 'text' || type === 'tel' || type === 'number' || type === '') {
                    if (name.includes('code') || name.includes('otp') || name.includes('pin') ||
                        placeholder.includes('code') || placeholder.includes('otp') ||
                        id.includes('code') || id.includes('otp') || id.includes('pin') ||
                        id.includes('verification')) {
                        return {
                            found: true,
                            selector: el.id ? `#${el.id}` : `[name="${el.name}"]`,
                            id: el.id,
                            name: el.name,
                            placeholder: el.placeholder
                        };
                    }
                }
            }
            return { found: false };
        }"""
        detect_output = await _run_harness(
            f"detect = {detect_script}\nprint('DETECT:', detect)",
            timeout=10,
            cdp_url=self._cdp_url,
        )
        if "DETECT: {'found': True" not in detect_output and "DETECT: {'found':True" not in detect_output:
            return False

        logger.info("Email verification code field detected — fetching from Gmail")
        # Get the page's domain to filter Gmail sender
        url_result = await _run_harness("print('URL:', current_tab()['url'])", timeout=5, cdp_url=self._cdp_url)
        url_str = url_result[0] if isinstance(url_result, tuple) else str(url_result)
        # Extract domain for sender filter
        import re as _re
        m = _re.search(r"https?://(?:www\.)?([^/]+)", url_str.split("URL:")[-1] if "URL:" in url_str else "")
        domain = m.group(1) if m else None

        # Fetch code from Gmail
        code = await self._fetch_email_verification_code(
            sender_pattern=domain,
            timeout=timeout,
        )
        if not code:
            logger.error(f"No verification code received within {timeout}s")
            return False

        # Find the input field and fill it
        # Extract selector from detect output
        sel_match = _re.search(r"'selector':\s*'([^']+)'", detect_output)
        if not sel_match:
            logger.error("Could not extract input selector")
            return False
        selector = sel_match.group(1)
        escaped_code = code.replace("\\", "\\\\").replace("'", "\\'")

        # Type the code
        type_script = f"""() => {{
            const el = document.querySelector("{selector}");
            if (el) {{
                el.focus();
                el.value = '{escaped_code}';
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
                return true;
            }}
            return false;
        }}"""
        fill_result = await _run_harness(
            f"filled = {type_script}\nprint('FILLED:', filled)",
            timeout=10,
            cdp_url=self._cdp_url,
        )
        if "FILLED: True" in fill_result:
            logger.info(f"Verification code {code} entered")
            # Click submit button
            await asyncio.sleep(1)
            click_script = """() => {
                const buttons = document.querySelectorAll('button, input[type=submit]');
                for (const b of buttons) {
                    const text = (b.textContent || b.value || '').toLowerCase();
                    if (text.includes('verify') || text.includes('submit') || text.includes('continue') || text.includes('next')) {
                        b.click();
                        return true;
                    }
                }
                return false;
            }"""
            await _run_harness(
                f"clicked = {click_script}\nprint('CLICKED:', clicked)",
                timeout=10,
                cdp_url=self._cdp_url,
            )
            await asyncio.sleep(3)
            return True
        return False

        if self._login_password in ("YOUR_PASSWORD", "your_password", "password", "changeme"):
            return AgentResult(
                success=False,
                error=f"Password is a placeholder. Run: survey-agent credentials --email {self._login_email} --password YOUR_REAL_PASSWORD",
            )

        login_url = site_url.rstrip("/") + "/users/sign_in"
        script = _build_login_script(login_url, self._login_email, self._login_password)
        output, rc = await _run_harness(script, timeout=60, cdp_url=self._cdp_url)

        success = "login_success" in output or "already_logged_in" in output
        error = ""
        if not success:
            if "login_failed" in output:
                error = "Login failed — check credentials"
            elif "password_field_not_found" in output:
                error = "Could not find password field"
            elif rc != 0:
                error = f"browser-harness error (rc={rc}): {output[:200]}"
            else:
                error = f"Login unclear: {output[:200]}"

        return AgentResult(success=success, output=output, error=error)

    async def _login_google_oauth(self, site_url: str, site_name: str) -> AgentResult:
        """Log in via Google OAuth. Assumes user is already signed in to Google in this Chrome profile."""
        email_esc = self._google_email.replace("\\", "\\\\").replace("'", "\\'")
        script = f"""import time

ensure_real_tab()
goto_url("{site_url}")
wait_for_load()
time.sleep(2)

# Click the Google sign-in button
js(\"\"\"
var btns = document.querySelectorAll('a, button, div, span, [role=button]');
for (var i = 0; i < btns.length; i++) {{
    var t = (btns[i].textContent || '').trim().toLowerCase();
    var ariaLabel = (btns[i].getAttribute('aria-label') || '').toLowerCase();
    if (t.includes('sign in with google') || t.includes('continue with google') ||
        t.includes('log in with google') || t === 'google' ||
        ariaLabel.includes('google') || ariaLabel.includes('sign in')) {{
        btns[i].click();
        print('CLICKED: ' + t.substring(0, 50));
        break;
    }}
}}
\"\"\")
wait_for_load()
time.sleep(5)

# Check if Google account chooser appeared, or if we went straight to dashboard
url = current_tab()["url"]
text = js("document.body ? document.body.innerText.substring(0, 500) : ''")

if "accounts.google.com" in url or "sign in - google" in text.lower() or "accounts.google.com" in url:
    # Google account chooser OR sign-in page — try to find/click the right account
    js(\"\"\"
    // First try: look for existing account with our email
    var accounts = document.querySelectorAll('[data-identifier], [data-email], li, div[role=link]');
    var found = false;
    for (var i = 0; i < accounts.length; i++) {{
        var t = (accounts[i].textContent || '').toLowerCase();
        if (t.includes('{email_esc}')) {{
            accounts[i].click();
            print('CLICKED_ACCOUNT: ' + t.substring(0, 50));
            found = true;
            break;
        }}
    }}
    if (!found) {{
        // No existing account — try entering email in the input field
        var emailInput = document.querySelector('input[type="email"], input[name="email"], #identifierId, input[autocomplete="username"]');
        if (emailInput) {{
            emailInput.focus();
            emailInput.value = '{email_esc}';
            emailInput.dispatchEvent(new Event('input', {{bubbles: true}}));
            emailInput.dispatchEvent(new Event('change', {{bubbles: true}}));
            print('ENTERED_EMAIL: ' + emailInput.value);
            // Click Next button
            var nextBtn = document.querySelector('#identifierNext, button[type="submit"], [id*="Next"]');
            if (nextBtn) {{
                nextBtn.click();
                print('CLICKED_NEXT_AFTER_EMAIL');
            }} else {{
                // Try submitting form
                var form = emailInput.closest('form');
                if (form) form.submit();
                print('SUBMITTED_FORM');
            }}
        }} else {{
            print('NO_EMAIL_INPUT_FOUND');
        }}
    }}
    \"\"\")
    wait_for_load()
    time.sleep(5)

    # After clicking account or entering email, we may land on the password page
    url = current_tab()["url"]
    text = js("document.body ? document.body.innerText.substring(0, 500) : ''")
    if "challenges" in url or ("password" in text.lower() and "accounts.google.com" in url):
        # Google password challenge — try to skip via cookie or signal
        # The user has a saved session, so this should auto-redirect
        print("PASSWORD_CHALLENGE: waiting for auto-redirect...")
        time.sleep(8)

final_url = current_tab()["url"]
text = js("document.body ? document.body.innerText.substring(0, 500) : ''")
print("FINAL_URL:", final_url)

# Handle about:blank — OAuth popup may have closed, navigate to dashboard directly
if final_url == "about:blank" or not final_url:
    print("ABOUT_BLANK: navigating to dashboard to verify login...")
    goto_url("https://app.primeopinion.com/surveys")
    wait_for_load()
    time.sleep(3)
    final_url = current_tab()["url"]
    text = js("document.body ? document.body.innerText.substring(0, 500) : ''")
    print("POST_NAV_URL:", final_url)

if "dashboard" in final_url.lower() or "survey" in final_url.lower() or "my points" in text.lower():
    print("RESULT: login_success")
elif "sign" in final_url.lower() or "login" in final_url.lower():
    print("RESULT: login_failed_still_on_signin")
else:
    print("RESULT: login_unknown")
"""
        output, rc = await _run_harness(script, timeout=60, cdp_url=self._cdp_url)
        logger.info("Google OAuth login output: %s", output[:300])

        success = "login_success" in output or "already_logged_in" in output
        error = ""
        if not success:
            if "login_failed" in output:
                error = "Google OAuth login failed — check that read.musik@gmail.com is signed in"
            elif rc != 0:
                error = f"browser-harness error (rc={rc}): {output[:200]}"
            else:
                error = f"Google OAuth login unclear: {output[:200]}"

        return AgentResult(success=success, output=output, error=error)

    async def discover_surveys(self, dashboard_url: str) -> AgentResult:
        # Dismiss dialogs
        await _run_harness(
            'js("try { window.onbeforeunload = null; } catch(e) {} "); print("RESULT: dismiss_done")',
            timeout=5,
            cdp_url=self._cdp_url,
        )

        script = _build_discover_script(dashboard_url)
        output, rc = await _run_harness(script, timeout=30, cdp_url=self._cdp_url)
        logger.info("Discover output (rc=%d):\n%s", rc, output[:500])

        has_surveys = False
        if "SURVEYS_RAW:" in output:
            raw_line = output.split("SURVEYS_RAW:", maxsplit=1)[1].split("\n", maxsplit=1)[0].strip()
            has_surveys = raw_line != "[]" and len(raw_line) > 2
            logger.info("Discover has_surveys=%s, raw_line preview: %s", has_surveys, raw_line[:200])

        if has_surveys:
            return AgentResult(success=True, output=output)
        return AgentResult(success=True, output="no surveys available")

    async def complete_survey(self, survey_info: str, profile_context: str) -> AgentResult:  # noqa: PLR0915
        """Complete a survey. Single tab, universal page handling."""
        # Step 0: Extract survey URL
        survey_url = None
        if "SURVEYS_RAW:" in survey_info:
            raw = survey_info.split("SURVEYS_RAW:", maxsplit=1)[1].split("\n", maxsplit=1)[0].strip()
            try:
                surveys = json.loads(raw)
                if surveys and isinstance(surveys, list):
                    survey_url = surveys[0].get("href", "")
            except json.JSONDecodeError:
                pass

        if not survey_url:
            return AgentResult(success=False, error="No survey URL found")

        # Check for existing checkpoint for this survey
        ckpt = cp_load(survey_url)
        if ckpt:
            resume_page = ckpt.get("page", 0)
            resume_url = ckpt.get("current_url", "")
            logger.info("Resuming survey from checkpoint (page %d, URL: %s)", resume_page, resume_url[:60])
            # Clear the old checkpoint — we'll create a new one as we make progress
            cp_clear()

        # Step 1: Navigate to dashboard, find fresh surveys, and navigate to the best one
        logger.info("Starting survey from dashboard")

        # JS to find and navigate to the best survey link — written to temp file
        # Write navigation JS to temp file using raw string to avoid escaping hell
        click_survey_js = """(() => {
  // Wait for AJAX-loaded survey cards
  const deadline = Date.now() + 8000;
  while (Date.now() < deadline) {
    const links = document.querySelectorAll('a.take-survey-redirect, a[href*="/surveys/pre/"], a[href*="/surveys/take/"]');
    if (links.length > 0) break;
  }
  const links = document.querySelectorAll('a.take-survey-redirect, a[href*="/surveys/pre/"], a[href*="/surveys/take/"]');
  // Sort by parent card (highest points first)
  var results = [];
  for (var i = 0; i < links.length; i++) {
    var a = links[i];
    var card = a.closest('[class*=survey-card]') || a.parentElement;
    var pts = parseInt((card ? card.getAttribute('data-award') : '0') || '0');
    if (!pts || pts === 0) continue;
    results.push({el: a, pts: pts});
  }
  results.sort(function(a, b) { return b.pts - a.pts; });
  if (results.length > 0) {
    // Return full URL (convert relative to absolute if needed)
    var href = results[0].el.getAttribute('href') || '';
    if (href && !href.startsWith('http')) {
      href = 'https://surveys.gobranded.com' + href;
    }
    return href;
  }
  return 'no_links';
})()"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, prefix="bh_nav_") as jf:
            jf.write(click_survey_js)
            jf.flush()
            nav_js_path = jf.name

        nav_script = (
            "import time\n"
            "ensure_real_tab()\n"
            'js("window.alert = function(){}; window.confirm = function(){return true}; window.prompt = function(){return null}")\n'
            "goto_url('https://surveys.gobranded.com/members')\n"
            "time.sleep(5)\n"
            "wait_for_load()\n"
            "time.sleep(3)\n"
            "url = current_tab()['url']\n"
            "print('NAV_URL:', url)\n"
            "page_text = js('document.body ? document.body.innerText.substring(0, 200) : \"\"')\n"
            "print('NAV_TEXT:', page_text[:100])\n"
            f"with open('{nav_js_path}') as f:\n"
            "    _js = f.read()\n"
            "result = js(_js)\n"
            "print('NAV_RESULT:', result)\n"
            "if result != 'no_links' and result.startswith('http'):\n"
            "    goto_url(result)\n"
            "    time.sleep(8)\n"
            "    wait_for_load()\n"
            "url = current_tab()['url']\n"
            "print('URL:', url)\n"
            "if '/dashboard' in url or '/members' in url:\n"
            "    print('RESULT: still_on_dashboard')\n"
            "elif result == 'no_links':\n"
            "    print('RESULT: no_survey_links')\n"
            "else:\n"
            "    print('RESULT: navigated')\n"
        )
        nav_out, nav_rc = await _run_harness(nav_script, timeout=45, cdp_url=self._cdp_url)
        Path(nav_js_path).unlink(missing_ok=True)
        logger.info("Navigation output: %s", nav_out[:300])

        # If still on dashboard, the survey link redirected — survey unavailable
        if "still_on_dashboard" in nav_out or "no_survey_links" in nav_out:
            return AgentResult(success=False, error="Survey link redirected to dashboard — survey unavailable")

        # Step 2: Handle pre-survey pages (consent, begin, cookies) — loop up to 3x
        for _ in range(3):
            pre_script = (
                "import time\n"
                'js("window.alert = function(){}; window.confirm = function(){return true}; window.prompt = function(){return null}")\n'
                "text = js(\"document.body ? document.body.innerText.substring(0, 1500) : ''\")\n"
                "lower = text.lower()\n"
                "url = current_tab()['url']\n"
                "is_survey_page = '/surveys/' in url and '/dashboard' not in url\n"
                "# First: try common consent button selectors (SSI, OneTrust, CookieYes, etc.)\n"
                "js(\"\"\"\n"
                "var consentSelectors = [\n"
                "  '#_evh-button',  // SSI Surveys\n"
                "  '.cky-btn-accept',  // CookieYes\n"
                "  '#onetrust-accept-btn-handler',  // OneTrust\n"
                "  '.onetrust-accept-btn-handler',\n"
                "  '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',  // Cookiebot\n"
                "  '#accept-btn',  // Generic\n"
                "  '.cookie-accept',\n"
                "  '.cookie-consent-accept',\n"
                "  'button[id*=\"accept\" i]',\n"
                "  'button[class*=\"accept\" i]',\n"
                "  'a[id*=\"accept\" i]'\n"
                "];\n"
                "for (var i = 0; i < consentSelectors.length; i++) {\n"
                "  var el = document.querySelector(consentSelectors[i]);\n"
                "  if (el && el.offsetParent !== null) {\n"
                "    el.click();\n"
                "    console.log('Clicked consent: ' + consentSelectors[i]);\n"
                "    break;\n"
                "  }\n"
                "}\n"
                "\"\"\")\n"
                "time.sleep(3)\n"
                "# Refresh text and url after consent click\n"
                "text = js(\"document.body ? document.body.innerText.substring(0, 1500) : ''\")\n"
                "lower = text.lower()\n"
                "url = current_tab()['url']\n"
                "if 'take survey' in lower and is_survey_page:\n"
                "    # On a survey pre-page — click TAKE SURVEY to enter the actual survey\n"
                "    js(\"var els=document.querySelectorAll('a,button,input[type=submit]'); for(var i=0;i<els.length;i++){var t=(els[i].textContent||els[i].value||'').trim().toLowerCase(); if(t==='take survey'){els[i].click();break;}}\")\n"
                "    time.sleep(5); wait_for_load()\n"
                "    print('ACTION: clicked_take_survey')\n"
                "elif 'begin survey' in lower or 'start survey' in lower:\n"
                "    js(\"var els=document.querySelectorAll('a,button,div[role=button],input[type=submit]'); for(var i=0;i<els.length;i++){var t=(els[i].textContent||els[i].value||'').trim().toLowerCase(); if(t==='begin survey'||t==='start survey'||t==='begin'||t==='start'){els[i].click();break;}}\")\n"
                "    time.sleep(5); wait_for_load()\n"
                "    print('ACTION: clicked_begin_survey')\n"
                "elif 'cookie' in lower or 'accept all' in lower:\n"
                "    js(\"document.querySelector('.cky-btn-accept')?.click(); document.querySelectorAll('button').forEach(function(b){var t=(b.textContent||'').trim().toLowerCase(); if(t==='accept all'||t==='accept'){b.click()}})\")\n"
                "    time.sleep(2)\n"
                "    print('ACTION: accepted_cookies')\n"
                "elif 'consent' in lower or 'i agree' in lower or 'i accept' in lower:\n"
                "    js(\"var els=document.querySelectorAll('a,button,input[type=submit],[role=button]'); for(var i=0;i<els.length;i++){var t=(els[i].textContent||els[i].value||'').trim().toLowerCase(); if(t.includes('agree')||t.includes('accept')||t.includes('consent')||t==='i agree'){els[i].click();break;}}\")\n"
                "    time.sleep(3); wait_for_load()\n"
                "    print('ACTION: accepted_consent')\n"
                "else:\n"
                "    print('ACTION: ready')\n"
                "    print('RESULT: pre_survey_done')\n"
            )
            pre_out, _ = await _run_harness(pre_script, timeout=20, cdp_url=self._cdp_url)
            logger.info("Pre-survey: %s", pre_out[:150])
            if "RESULT: pre_survey_done" in pre_out:
                break

        # Step 3: Main survey loop
        max_pages = 40
        consecutive_empty = 0
        current_url = survey_url  # Initialize for checkpoint on first iteration
        last_url = ""
        last_content_hash = ""
        last_page_text = ""
        same_url_count = 0
        for page_num in range(max_pages):
            await _run_harness("wait_for_load(); import time; time.sleep(4)", timeout=15, cdp_url=self._cdp_url)

            # Save checkpoint for crash recovery
            cp_save(page_num, survey_url, current_url, profile_context)

            # Universal page reader — uses template file to avoid escaping issues
            # Write JS helper files to temp locations
            text_js = 'document.body ? document.body.innerText.substring(0, 5000) : ""'

            # The full iframe probe is loaded from the bundled file
            iframe_probe_path = Path(__file__).parent / "iframe_probe.js"
            iframe_js = iframe_probe_path.read_text()

            with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, prefix="bh_eljs_") as ejf:
                ejf.write(_PAGE_ELEMENTS_JS)
                ejf.flush()
                eljs_path = ejf.name
            with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, prefix="bh_txtjs_") as tjf:
                tjf.write(text_js)
                tjf.flush()
                txt_js_path = tjf.name

            # Copy template and substitute paths
            template_path = Path(__file__).parent / "_page_reader.py"
            template = template_path.read_text()
            page_script = template.replace("__ELEMENTS_JS_PATH__", eljs_path)
            # Copy JS files to fixed paths for the page reader template
            import shutil  # noqa: PLC0415

            shutil.copy2(txt_js_path, "/tmp/_bh_text_js.js")
            shutil.copy2(iframe_probe_path, "/tmp/_bh_iframe_probe.js")

            page_out, rc = await _run_harness(page_script, timeout=15, cdp_url=self._cdp_url)
            Path(eljs_path).unlink(missing_ok=True)
            Path(txt_js_path).unlink(missing_ok=True)
            if rc != 0 or "PAGE_TEXT:" not in page_out:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    # Try reconnecting to Chrome before giving up
                    logger.warning("Page read failed 3 times — attempting Chrome reconnect")
                    try:
                        reconnect_out, reconnect_rc = await _run_harness(
                            "print(page_info())", timeout=10, cdp_url=self._cdp_url
                        )
                        if reconnect_rc == 0:
                            logger.info("Chrome reconnected: %s", reconnect_out.strip()[:80])
                            consecutive_empty = 0
                            continue
                    except Exception:
                        pass
                    return AgentResult(success=False, error="Page read failed 3 times")
                continue

            page_text = ""
            elements_raw = "[]"
            if "PAGE_TEXT:" in page_out:
                page_text = page_out.split("PAGE_TEXT:", maxsplit=1)[1].split("ELEMENTS:", maxsplit=1)[0].strip()
            if "ELEMENTS:" in page_out:
                elements_raw = page_out.split("ELEMENTS:", maxsplit=1)[1].split("URL:", maxsplit=1)[0].strip()

            text_lower = page_text.lower()

            # CUA OCR fallback: if page text is sparse, take a screenshot and OCR it
            if len(page_text.strip()) < 300 and self._chrome_proc:
                try:
                    import base64  # noqa: PLC0415

                    chrome_pid = self._chrome_proc.pid
                    # Find agent Chrome window
                    result = subprocess.run(
                        ["cua-driver", "call", "list_windows", "--json"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    windows = json.loads(result.stdout.strip()).get("windows", [])
                    best_wid = None
                    best_area = 0
                    for w in windows:
                        if w.get("app_name") == "Google Chrome" and w.get("pid") == chrome_pid:
                            b = w.get("bounds", {})
                            area = b.get("width", 0) * b.get("height", 0)
                            if b.get("height", 0) > 100 and area > best_area:
                                best_wid = w["window_id"]
                                best_area = area

                    if best_wid:
                        r2 = subprocess.run(
                            [
                                "cua-driver",
                                "call",
                                "get_window_state",
                                json.dumps({"pid": chrome_pid, "window_id": best_wid, "max_elements": 200}),
                            ],
                            capture_output=True,
                            text=True,
                            timeout=15,
                            check=False,
                        )
                        data = json.loads(r2.stdout.strip())
                        screenshot_b64 = data.get("screenshot_png_b64", "")

                        if screenshot_b64:
                            import pytesseract  # noqa: PLC0415
                            from PIL import Image  # noqa: PLC0415

                            img_data = base64.b64decode(screenshot_b64)
                            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                                tf.write(img_data)
                                tmp_path = tf.name
                            img = Image.open(tmp_path)
                            ocr_text = pytesseract.image_to_string(img)
                            Path(tmp_path).unlink(missing_ok=True)

                            if len(ocr_text.strip()) > len(page_text.strip()):
                                logger.info(
                                    "CUA OCR fallback: %d chars (vs %d DOM chars)", len(ocr_text), len(page_text)
                                )
                                page_text = ocr_text
                                text_lower = page_text.lower()
                except Exception as e:
                    logger.debug("CUA OCR fallback failed: %s", e)
            current_url = ""
            if "URL:" in page_out:
                current_url = page_out.split("URL:", maxsplit=1)[1].split("\n", maxsplit=1)[0].strip()

            # Domain change detection at page read time (redirect may happen during load)
            if page_num >= 1 and last_url and current_url:
                from urllib.parse import urlparse  # noqa: PLC0415

                try:
                    old_domain = urlparse(last_url).netloc.split(":")[0]
                    new_domain = urlparse(current_url).netloc.split(":")[0]
                    if old_domain and new_domain and old_domain != new_domain:
                        logger.info("Domain changed (%s → %s) — survey complete", old_domain[:30], new_domain[:30])
                        return AgentResult(success=True, output="RESULT: survey_complete")
                except Exception:
                    pass

            # Track page content changes (not just URL — surveys use AJAX)
            import hashlib  # noqa: PLC0415

            content_hash = hashlib.md5(page_text.encode()).hexdigest()[:12]  # noqa: S324
            if current_url == last_url and content_hash == last_content_hash:
                same_url_count += 1
            elif current_url != last_url:
                same_url_count = 0
            elif content_hash != last_content_hash:
                # URL same but content changed — check similarity
                # If content is >80% similar, it's the same page (dynamic elements)
                if last_page_text and page_text:
                    common = len(set(page_text.split()) & set(last_page_text.split()))
                    total = max(len(set(page_text.split())), len(set(last_page_text.split())), 1)
                    similarity = common / total
                    if similarity > 0.8:
                        same_url_count += 1
                        logger.info(
                            "Content similar (%.0f%%) — incrementing stuck counter to %d",
                            similarity * 100,
                            same_url_count,
                        )
                    else:
                        same_url_count = 0
                else:
                    same_url_count = 0
            last_url = current_url
            last_content_hash = content_hash
            last_page_text = page_text

            # If we're back on the dashboard after being on a survey page, the survey is done
            if (
                ("/dashboard" in current_url or "/members/oq" in current_url)
                and page_num >= 5
                and "/members/" not in last_url
            ):
                logger.info("Back on dashboard after %d pages — survey complete", page_num)
                return AgentResult(success=True, output="Survey completed")

            if same_url_count >= 3:
                # After 2 identical pages, try refreshing the page
                if same_url_count == 3:
                    logger.info("Page stuck — refreshing page")
                    await _run_harness(
                        "press_key('F5'); import time; time.sleep(3); wait_for_load()",
                        timeout=15,
                        cdp_url=self._cdp_url,
                    )
                    continue
                logger.warning("Stuck on same page after refresh — skipping survey")
                return AgentResult(success=False, error="Stuck on same page, skipping")

            logger.info("Page %d: %d chars text, url=%s", page_num + 1, len(page_text), current_url[:80])

            # Skip sparse-page surveys (likely iframe-based, unreadable)
            if (
                page_num >= 1
                and len(page_text.strip()) < 200
                and any(s in current_url for s in ["purespectrum", "ssisurveys", "escalent"])
            ):
                logger.info("Sparse page (%d chars) on %s — skipping survey", len(page_text), current_url[:50])
                return AgentResult(success=False, error="Sparse iframe survey, skipping")

            # Disqualification check
            if any(
                kw in text_lower
                for kw in [
                    "not a match",
                    "don't qualify",
                    "disqualified",
                    "screened out",
                    "not eligible",
                    "we appreciate your interest",
                    "unfortunately, you",
                    "do not qualify",
                    "better luck next time",
                ]
            ):
                return AgentResult(success=False, error="Survey disqualified: not a match")

            # Completion check (redirected to non-survey URL)
            if page_num >= 5 and not any(
                s in current_url for s in ["gobranded", "survey", "screener", "qualify", "screening"]
            ):
                logger.info("Redirected to non-survey URL after %d pages — survey complete", page_num + 1)
                return AgentResult(success=True, output="RESULT: survey_complete")

            # Screenout check (redirected to non-survey URL after 1-4 pages)
            if page_num >= 1 and page_num < 5 and not any(
                s in current_url for s in ["gobranded", "survey", "screener", "qualify", "screening"]
            ):
                logger.info("Redirected to non-survey URL after only %d pages — screenout", page_num + 1)
                return AgentResult(success=False, error=f"Screenout after {page_num + 1} pages")

            # Completion check (no form elements + past page 1)
            no_elements = False
            try:
                els = json.loads(elements_raw) if elements_raw else []
                has_inputs = any(e.get("type") in ("radio", "checkbox", "text", "email", "number") for e in els)
                no_elements = not has_inputs
            except (json.JSONDecodeError, TypeError):
                no_elements = True

            if any(
                kw in text_lower
                for kw in [
                    "thank you",
                    "survey complete",
                    "successfully completed",
                    "congratulations",
                    "you've earned",
                ]
            ):
                if no_elements and page_num >= 1:
                    return AgentResult(success=True, output="RESULT: survey_complete")

            # Strong completion signal: no interactive elements on a page that previously had them
            # (e.g., "this survey is for the blind" assistive tech pages after submission)
            if no_elements and page_num >= 1 and last_page_text and len(last_page_text.strip()) > 200:
                # Previous page had content — this page is empty of inputs = survey done
                logger.info("No input elements on page %d (previous had %d chars) — survey complete",
                            page_num + 1, len(last_page_text))
                return AgentResult(success=True, output="RESULT: survey_complete")

            # CAPTCHA check — try to solve, not just skip
            captcha = await self._detect_captcha({"text": page_text, "forms": elements_raw})
            if captcha:
                logger.info("CAPTCHA on page %d: %s", page_num + 1, captcha[:80])
                solved = False
                if "text_image" in captcha or "text_challenge" in captcha:
                    solved = await self._try_solve_text_captcha()
                elif "drag_drop" in captcha:
                    solved = await self._try_solve_drag_captcha()
                if solved:
                    logger.info("CAPTCHA solved on page %d", page_num + 1)
                    await self._click_next()
                    time.sleep(2)
                    continue
                logger.warning("CAPTCHA not solved on page %d — skipping survey", page_num + 1)
                return AgentResult(success=False, error=f"CAPTCHA not solved: {captcha[:200]}")

            # Skip answer generation if we're on the dashboard (screenout or no survey)
            if "/dashboard" in current_url or "/members" in current_url:
                if page_num < 3:
                    logger.info("Dashboard page at page %d — survey screenout, not answering", page_num + 1)
                    return AgentResult(success=False, error="Survey screenout: redirected to dashboard")
                # Past page 3 on dashboard means it might be a real survey ending there
                logger.info("Dashboard after %d pages — real completion", page_num + 1)
                return AgentResult(success=True, output="Survey completed")

            # Use the structured two-stage answer pipeline
            answers = await self._generate_structured_answers(page_text, elements_raw, profile_context)

            if answers and answers.strip() != "[]":
                await self._apply_answers(answers)
                logger.info("Applied answers on page %d: %s", page_num + 1, answers[:150])

            # Click Next/Continue/Submit
            # First: check for unanswered required questions and fill them
            pre_submit_js = """(() => {
  let filled = 0;
  // Find unanswered radio groups
  const radios = document.querySelectorAll('input[type=radio]');
  const groups = {};
  radios.forEach(r => { if (r.name) { if (!groups[r.name]) groups[r.name] = []; groups[r.name].push(r); }});
  for (const [name, rs] of Object.entries(groups)) {
    if (!rs.some(r => r.checked)) { rs[0].click(); filled++; }
  }
  // Find empty required text inputs
  document.querySelectorAll('input[type=text][required], textarea[required]').forEach(el => {
    if (!el.value.trim()) { el.value = 'N/A'; el.dispatchEvent(new Event('input', {bubbles:true})); filled++; }
  });
  // Find empty required selects
  document.querySelectorAll('select[required]').forEach(el => {
    if (!el.value) { el.selectedIndex = 1; el.dispatchEvent(new Event('change', {bubbles:true})); filled++; }
  });
  return 'prefilled:' + filled;
})()"""
            with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, prefix="bh_presub_") as sf:
                sf.write(pre_submit_js)
                sf.flush()
                presub_path = sf.name
            presub_out, _ = await _run_harness(
                f"with open('{presub_path}') as f:\n    _js = f.read()\nresult = js(_js)\nprint('PRE_SUBMIT:', result)\nprint('RESULT: done')",
                timeout=10,
                cdp_url=self._cdp_url,
            )
            Path(presub_path).unlink(missing_ok=True)
            if "prefilled:" in presub_out:
                logger.info(
                    "Pre-submit: %s", presub_out.split("prefilled:", maxsplit=1)[1].split("\n", maxsplit=1)[0].strip()
                )

            pre_url = current_url
            advanced = await self._click_next()
            if not advanced:
                await _run_harness("press_key('Enter')", timeout=5, cdp_url=self._cdp_url)
            time.sleep(3)

            # Check if page actually advanced
            check_url_out, _ = await _run_harness(
                "print('URL:', current_tab()['url'])",
                timeout=10,
                cdp_url=self._cdp_url,
            )
            new_url = ""
            if "URL:" in check_url_out:
                new_url = check_url_out.split("URL:", maxsplit=1)[1].strip()

            # Domain change detection: if the URL domain changed significantly, previous survey is done
            if new_url and pre_url and page_num >= 2:
                from urllib.parse import urlparse  # noqa: PLC0415

                try:
                    old_domain = urlparse(pre_url).netloc.split(":")[0]
                    new_domain = urlparse(new_url).netloc.split(":")[0]
                    # If domain changed (not just path change), survey redirected — treat as complete
                    if old_domain and new_domain and old_domain != new_domain:
                        logger.info("Domain changed (%s → %s) — survey complete", old_domain[:30], new_domain[:30])
                        return AgentResult(success=True, output="RESULT: survey_complete")
                except Exception:
                    pass

            # If page didn't advance, try form submission via JS
            if new_url and new_url == pre_url:
                submit_js = """(() => {
  const forms = document.querySelectorAll('form');
  for (const form of forms) {
    if (form.offsetParent !== null) { form.submit(); return 'submitted'; }
  }
  const btns = document.querySelectorAll('input[type=submit], button[type=submit]');
  for (const btn of btns) {
    if (btn.offsetParent !== null) { btn.click(); return 'clicked:' + btn.value; }
  }
  return 'no_form';
})()"""
                with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, prefix="bh_submit_") as sf:
                    sf.write(submit_js)
                    sf.flush()
                    submit_path = sf.name
                submit_out, _ = await _run_harness(
                    f"with open('{submit_path}') as f:\n    _js = f.read()\nresult = js(_js)\nprint('SUBMIT:', result)\nprint('RESULT: done')",
                    timeout=10,
                    cdp_url=self._cdp_url,
                )
                Path(submit_path).unlink(missing_ok=True)
                if "SUBMIT:" in submit_out:
                    logger.info(
                        "Form submit fallback: %s",
                        submit_out.split("SUBMIT:", maxsplit=1)[1].split("\n", maxsplit=1)[0].strip(),
                    )
                time.sleep(3)

            if new_url and new_url == pre_url and page_num > 2:
                # Page didn't advance — try brute-force: click each unselected radio
                logger.info("Page didn't advance — trying brute-force radio selection")
                brute_js = (
                    "(() => {\n"
                    "    const radios = document.querySelectorAll('input[type=radio]');\n"
                    "    const groups = {};\n"
                    "    radios.forEach(r => {\n"
                    "        if (!r.name) return;\n"
                    "        if (!groups[r.name]) groups[r.name] = [];\n"
                    "        groups[r.name].push(r);\n"
                    "    });\n"
                    "    let clicked = 0;\n"
                    "    for (const [name, rs] of Object.entries(groups)) {\n"
                    "        const anySelected = rs.some(r => r.checked);\n"
                    "        if (!anySelected && rs.length > 0) {\n"
                    "            rs[rs.length - 1].click();\n"
                    "            clicked++;\n"
                    "        }\n"
                    "    }\n"
                    "    return 'clicked:' + clicked;\n"
                    "})()"
                )
                next_js = (
                    "(() => {\n"
                    "    const btns = document.querySelectorAll('button, input[type=submit], [role=button]');\n"
                    "    for (const btn of btns) {\n"
                    "        const text = (btn.textContent || btn.value || '').toLowerCase();\n"
                    "        if (['next', 'continue', 'submit', 'proceed'].some(k => text.includes(k)) && btn.offsetParent !== null) {\n"
                    "            btn.click();\n"
                    "            return 'clicked:' + text.substring(0, 20);\n"
                    "        }\n"
                    "    }\n"
                    "    return 'none';\n"
                    "})()"
                )
                with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, prefix="bh_brute_") as bf:
                    bf.write(brute_js)
                    bf.flush()
                    brute_path = bf.name
                with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, prefix="bh_next_") as nf:
                    nf.write(next_js)
                    nf.flush()
                    next_path = nf.name

                brute_script = (
                    "import time\n"
                    f"with open('{brute_path}') as f:\n"
                    "    _brute = f.read()\n"
                    "result = js(_brute)\n"
                    "print('BRUTE:', result)\n"
                    "time.sleep(1)\n"
                    f"with open('{next_path}') as f:\n"
                    "    _next = f.read()\n"
                    "result2 = js(_next)\n"
                    "print('NEXT2:', result2)\n"
                    "time.sleep(2)\n"
                    "print('RESULT: brute_done')\n"
                )
                await _run_harness(brute_script, timeout=20, cdp_url=self._cdp_url)
                Path(brute_path).unlink(missing_ok=True)
                Path(next_path).unlink(missing_ok=True)

            consecutive_empty = 0
            time.sleep(2)
        return AgentResult(success=False, error=f"Survey not complete after {max_pages} pages")

    async def _generate_page_action(self, page_text: str, elements_raw: str, profile_context: str) -> str | None:
        """Use LLM to generate a browser-harness script for the current page."""
        from browser_use.llm.messages import SystemMessage, UserMessage  # noqa: PLC0415

        from ..llm_provider import get_llm  # noqa: PLC0415

        llm = get_llm(
            provider=self._llm_provider,
            model=self._llm_model,
            api_key=self._llm_api_key,
            base_url=self._llm_base_url,
        )
        inner = getattr(llm, "_inner", llm)

        system = (
            "You generate browser-harness Python scripts to answer survey questions.\n\n"
            "Given page text and interactive elements, write a script that:\n"
            "1. Answers visible questions using the user profile\n"
            "2. Uses fill_input(sel, text) for text fields\n"
            '3. Uses js("document.querySelector(sel).click()") for radio/checkbox/button\n'
            "4. Uses js to select dropdowns\n"
            "5. Prints 'ACTION: answered' when done\n\n"
            "Available helpers: fill_input, js, click_at_xy, type_text, press_key, wait_for_load\n\n"
            "Rules:\n"
            "- Use element IDs/names from the ELEMENTS list as selectors\n"
            "- For radio buttons: js('document.querySelector(\"input[name=X][value=Y]\").click()')\n"
            "- For custom divs: js('document.querySelectorAll(\"[role=button]\")[N].click()')\n"
            "- NEVER use current_url() — use current_tab()['url']\n"
            "- Output ONLY Python code, no explanations"
        )

        user_msg = (
            f"PAGE TEXT (first 1500 chars):\n{page_text[:1500]}\n\n"
            f"INTERACTIVE ELEMENTS:\n{elements_raw[:2000]}\n\n"
            f"USER PROFILE:\n{profile_context[:500]}\n\n"
            "Write the script."
        )

        try:
            result = await inner.ainvoke(
                [
                    SystemMessage(content=system),
                    UserMessage(content=user_msg),
                ]
            )
            code = result.completion if hasattr(result, "completion") else str(result)
            code = re.sub(r"<think>.*?</think>\s*", "", code, flags=re.DOTALL).strip()
            code = re.sub(r"^```(?:python)?\s*", "", code.strip())
            code = re.sub(r"\s*```$", "", code.strip())
            code = _fix_bad_helpers(code)
            return code if code.strip() else None
        except Exception as e:
            logger.error("LLM action generation failed: %s", e)
            return None

    async def _generate_structured_answers(self, page_text: str, elements_raw: str, profile_context: str) -> str | None:
        """Generate structured survey answers using the two-stage pipeline.

        Stage 1: Parse page elements into question groups.
        Stage 2: LLM answers questions (not code), then resolve to element selectors.
        Stage 3: Deterministic fallback for any unanswered questions.
        """
        # Build the LLM call wrapper using the existing fallback chain
        async def _llm_call_wrapper(prompt: str) -> str | None:
            from browser_use.llm.messages import SystemMessage, UserMessage  # noqa: PLC0415
            from ..llm_provider import get_llm  # noqa: PLC0415

            # Build fallback chain: primary → config fallback → OpenCode → FreeModel
            providers_to_try = [
                {"provider": self._llm_provider, "model": self._llm_model,
                 "api_key": self._llm_api_key, "base_url": self._llm_base_url},
            ]
            from ..config import load_config  # noqa: PLC0415
            cfg = load_config()
            if cfg.llm_api_key and cfg.llm_provider != self._llm_provider:
                providers_to_try.append({"provider": cfg.llm_provider, "model": cfg.llm_model,
                                          "api_key": cfg.llm_api_key, "base_url": cfg.llm_base_url})

            # Try reading API keys from .zshrc (env vars aren't always exported to Python)
            _zshrc_keys = _read_zshrc_api_keys()

            opencode_key = self._llm_api_key if self._llm_provider == "opencode" else _zshrc_keys.get("OPENCODE_API_KEY", "")
            if opencode_key and not any(p.get("provider") == "opencode" for p in providers_to_try):
                providers_to_try.append({"provider": "opencode", "model": "deepseek-v4-flash-free",
                                          "api_key": opencode_key})

            freemodel_key = _zshrc_keys.get("FREEMODEL_API_KEY", "")
            if freemodel_key and not any(p.get("provider") == "freemodel" for p in providers_to_try):
                providers_to_try.append({"provider": "freemodel", "model": "claude-haiku-4-5-20251001",
                                          "api_key": freemodel_key})

            last_error = None
            for prov in providers_to_try:
                try:
                    llm = get_llm(**{k: v for k, v in prov.items() if v})
                    inner = getattr(llm, "_inner", llm)
                    result = await inner.ainvoke([UserMessage(content=prompt)])
                    raw = result.completion if hasattr(result, "completion") else str(result)
                    if raw and len(raw.strip()) > 10:
                        return raw
                except Exception as e:
                    last_error = e
                    logger.warning("LLM call failed for %s: %s", prov.get("provider", "?"), str(e)[:100])
                    continue  # Try next provider regardless of error type
            logger.warning("All LLM providers failed. Last: %s", last_error)
            return None

        try:
            actions_json = await llm_ans.generate_actions(
                page_text=page_text,
                elements_raw=elements_raw,
                profile_context=profile_context,
                profile_dict=_PROFILE,
                llm_call_fn=_llm_call_wrapper,
            )
            logger.info("Structured answers pipeline: %s", actions_json[:200])
            return actions_json
        except Exception as e:
            logger.error("Structured answers pipeline failed: %s", e)
            return None

    async def _get_answers_from_llm(self, page_data: dict, profile_context: str) -> str | None:
        """Ask the LLM to read the page and return structured answers as JSON."""
        from browser_use.llm.messages import SystemMessage, UserMessage  # noqa: PLC0415

        from ..llm_provider import get_llm  # noqa: PLC0415

        # Build fallback chain: primary → config fallback → OpenCode → FreeModel
        providers_to_try = [
            {
                "provider": self._llm_provider,
                "model": self._llm_model,
                "api_key": self._llm_api_key,
                "base_url": self._llm_base_url,
            },
        ]
        # Add config fallback if different from primary
        from ..config import load_config  # noqa: PLC0415

        cfg = load_config()
        if cfg.llm_api_key and cfg.llm_provider != self._llm_provider:
            providers_to_try.append(
                {
                    "provider": cfg.llm_provider,
                    "model": cfg.llm_model,
                    "api_key": cfg.llm_api_key,
                    "base_url": cfg.llm_base_url,
                }
            )
        # Add OpenCode free tier as fallback
        import os  # noqa: PLC0415

        opencode_key = os.environ.get("OPENCODE_API_KEY", "")
        if opencode_key:
            providers_to_try.append(
                {"provider": "opencode", "model": "deepseek-v4-flash-free", "api_key": opencode_key}
            )
        # Add FreeModel as fallback
        freemodel_key = os.environ.get("FREEMODEL_API_KEY", "")
        if freemodel_key:
            providers_to_try.append(
                {"provider": "freemodel", "model": "claude-haiku-4-5-20251001", "api_key": freemodel_key}
            )

        system = (
            "You are a REAL HUMAN taking a survey. Given a page's text and interactive elements, "
            "return a JSON array of actions to answer the questions.\n\n"
            'Each action: {"selector": "CSS", "value": "", "action": "click|fill|select"}\n\n'
            "CRITICAL RULES:\n"
            "- Radio/checkbox: action=click, selector=#id or input[name=X][value=Y]\n"
            "- Text/textarea: action=fill, selector=#id, value=answer\n"
            "- Select: action=select, selector=#id, value=option_value\n"
            "- Match ALL answers to this specific profile:\n"
            "  Age 39, Male, Black/African American, Domestic partnership, Austin TX 78736\n"
            "  Self-employed Founder/CTO in Technology, fully remote, 8 years experience\n"
            "  Trade/vocational degree in Software Engineering, household size 5, 3 children (ages 4,5,5)\n"
            "  Income $50-75K, rent, credit score Poor, Former smoker, exercises several times/week\n"
            "  Very good health, Uninsured, Never drinks alcohol\n"
            "  Hobbies: Reading, Gaming, Cooking, Travel, Music, Art, Photography\n"
            "  Shops monthly on Amazon/Target/Costco/Best Buy, Moderate politically, Expert with tech\n"
            "  Social media: Facebook, Instagram, X/Twitter, LinkedIn, Reddit, YouTube\n"
            "  Streaming: Netflix, Hulu, HBO Max, Amazon Prime, Spotify\n"
            "- For sensitive questions, prefer 'Prefer not to answer'\n"
            "- NEVER type the word 'ROBOT' — this is a bot detection trap. If the page says "
            "'enter the word ROBOT', the REAL answer is in an IMAGE on the page that you cannot see. "
            "Skip any text input field that asks for 'ROBOT', 'captcha', or 'verification code'.\n"
            "- NEVER fill fields with name/id containing 'robot', 'captcha', 'verify', 'security', 'confirm'\n"
            "- Answer like a HUMAN would: pick the most natural, relatable answer. For questions about "
            "personal experiences (like 'first day of school'), choose the answer that sounds like a real "
            "person's memory (e.g., 'a mix of nervousness and excitement'), NOT something robotic or clinical.\n"
            "- For 'select all that apply' questions, pick 2-4 relevant options, not all of them.\n"
            "- For MATRIX/RATING questions (rows × columns), select ONE option per row. "
            "Do NOT select multiple options in the same row. Use the row ID in the selector.\n"
            "- If a page has many questions, answer ALL of them before clicking Next.\n"
            "- Return ONLY the JSON array, no explanation"
        )

        user_msg = (
            f"PAGE TEXT:\n{page_data['text'][:2000]}\n\n"
            f"INTERACTIVE ELEMENTS:\n{page_data['forms'][:2000]}\n\n"
            f"USER PROFILE:\n{profile_context[:1000]}\n\n"
            "Return the JSON array of answers."
        )

        last_error = None
        for prov in providers_to_try:
            try:
                llm = get_llm(**{k: v for k, v in prov.items() if v})
                inner = getattr(llm, "_inner", llm)

                result = await inner.ainvoke([SystemMessage(content=system), UserMessage(content=user_msg)])
                raw = result.completion if hasattr(result, "completion") else str(result)
                logger.info("LLM raw response (first 500): %s", raw[:500])
                raw = re.sub(r"<think>.*?</think>\s*", "", raw, flags=re.DOTALL).strip()
                if "[" in raw:
                    raw = raw[raw.index("[") : raw.rindex("]") + 1]
                logger.info("LLM extracted JSON: %s", raw[:500])
                # Filter out CAPTCHA-related selectors and bot trap answers
                try:
                    answers = json.loads(raw)
                    captcha_keywords = ["robot", "captcha", "verify", "security", "confirm"]
                    filtered = []
                    for a in answers:
                        sel_val = (a.get("selector", "") + a.get("value", "")).lower()
                        # Skip if any CAPTCHA keyword found
                        if any(kw in sel_val for kw in captcha_keywords):
                            continue
                        # Skip if value is exactly "ROBOT" (bot trap)
                        if a.get("value", "").strip().upper() == "ROBOT":
                            continue
                        filtered.append(a)
                    if len(filtered) != len(answers):
                        logger.info("Filtered %d CAPTCHA-related answers", len(answers) - len(filtered))
                    raw = json.dumps(filtered)
                except (json.JSONDecodeError, TypeError):
                    pass
                return raw
            except Exception as e:
                last_error = e
                if "429" in str(e) or "rate" in str(e).lower() or "quota" in str(e).lower():
                    logger.warning("LLM rate limited (%s), trying fallback...", prov.get("provider", "?"))
                    continue
                logger.error("LLM answer generation failed: %s", e)
                return None

        logger.error("All LLM providers failed. Last error: %s", last_error)
        return None

    def _deterministic_answers(self, page_data: dict) -> str:  # noqa: PLR0915
        """Generate answers without LLM for common question types."""
        forms_raw = page_data.get("forms", "[]")
        try:
            forms = json.loads(forms_raw) if forms_raw else []
        except json.JSONDecodeError:
            return "[]"
        if not forms:
            return "[]"

        answers = []

        # Language selection
        for el in forms:
            if el.get("type") == "radio" and "language" in el.get("name", "").lower():
                label = (el.get("label") or "").lower()
                if "english" in label or el.get("value") == "en":
                    sel = (
                        f"#{_css_escape(el['id'])}"
                        if el.get("id")
                        else f"input[name='{el['name']}'][value='{el['value']}']"
                    )
                    answers.append({"selector": sel, "action": "click", "value": ""})
                    break

        # Radio groups — question-aware selection
        page_text = page_data.get("text", "").lower()
        radio_groups = {}
        for el in forms:
            if el.get("type") == "radio" and el.get("name"):
                radio_groups.setdefault(el["name"], []).append(el)

        for name, options in radio_groups.items():
            if any(name in a.get("selector", "") for a in answers):
                continue
            picked = _pick_radio_answer(page_text, options)
            sel = (
                f"#{_css_escape(picked['id'])}"
                if picked.get("id")
                else f"input[name='{name}'][value='{picked.get('value', '')}']"
            )
            answers.append({"selector": sel, "action": "click", "value": ""})

        # Checkboxes — for "select all that apply" questions
        checkbox_groups = {}
        for el in forms:
            if el.get("type") == "checkbox" and el.get("name"):
                checkbox_groups.setdefault(el["name"], []).append(el)

        for name, options in checkbox_groups.items():
            if any(name in a.get("selector", "") for a in answers):
                continue
            # Look for "none" or "none of the above" option
            none_opt = None
            for opt in options:
                label = (opt.get("label") or "").lower()
                if "none" in label or "none of the above" in label or "prefer not" in label:
                    none_opt = opt
                    break
            if none_opt:
                sel = (
                    f"#{_css_escape(none_opt['id'])}"
                    if none_opt.get("id")
                    else f"input[name='{name}'][value='{none_opt.get('value', '')}']"
                )
                answers.append({"selector": sel, "action": "click", "value": ""})
        # Text inputs
        for el in forms:
            if el.get("type") in ("text", "email", "number", "tel") or el.get("tag") == "textarea":
                label = (el.get("label") or el.get("placeholder") or "").lower()
                sel = f"#{_css_escape(el['id'])}" if el.get("id") else el.get("tag", "input")
                value = ""
                if "email" in label:
                    value = "read.musik@gmail.com"
                elif "age" in label:
                    value = "39"
                elif "name" in label:
                    value = "Antonio"
                elif "phone" in label:
                    value = "555-0123"
                elif "zip" in label or "postal" in label:
                    value = "90210"
                elif el.get("tag") == "textarea":
                    value = "I enjoy sharing my opinions and helping improve products and services."
                else:
                    value = "N/A"
                answers.append({"selector": sel, "action": "fill", "value": value})

        # Selects
        for el in forms:
            if el.get("tag") == "select" and el.get("opts"):
                sel = f"#{_css_escape(el['id'])}" if el.get("id") else f"select[name='{el.get('name', '')}']"
                opts = el["opts"]
                value = ""
                for opt in opts:
                    if opt.get("v") and opt.get("t") and opt["t"].lower() not in ("select", "choose", "pick one", ""):
                        value = opt["v"]
                        break
                if not value and opts:
                    value = opts[0].get("v", "")
                if value:
                    answers.append({"selector": sel, "action": "select", "value": value})

        return json.dumps(answers) if answers else "[]"

    @staticmethod
    def _escape_answers(answers_json: str) -> str:
        """Escape CSS selectors in LLM-generated answers."""
        try:
            answers = json.loads(answers_json)
        except json.JSONDecodeError:
            return answers_json
        for ans in answers:
            sel = ans.get("selector", "")
            if sel.startswith("#") and "\\" not in sel:
                raw_id = sel[1:]
                # Use CSS.escape()-equivalent: escape every non-identifier char
                escaped = ""
                for i, ch in enumerate(raw_id):
                    cp = ord(ch)
                    if cp == 0:
                        escaped += "\ufffd"
                    elif (0x01 <= cp <= 0x1F) or cp == 0x7F:
                        escaped += f"\\{cp:02X} "
                    elif ch in " \t\n\"#'(),./:;<=>?@[\\]`{|}~!$&*+;@^|":
                        escaped += f"\\{ch}"
                    elif i == 0 and ch.isdigit():
                        escaped += f"\\{cp:02X} "
                    else:
                        escaped += ch
                ans["selector"] = "#" + escaped
        return json.dumps(answers)

    async def _apply_answers(self, answers_json: str) -> bool:
        """Apply answers using JSON file + safe JS serialization (fixes quote escaping)."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, prefix="bh_ans_") as f:
            f.write(answers_json)
            f.flush()
            ans_path = f.name

        # Generate a JS file that the harness can load — avoids all string escaping issues.
        apply_answers_path = Path(__file__).parent / "apply_answers.js"
        js_template = apply_answers_path.read_text()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, prefix="bh_js_") as jf:
            jf.write(js_template)
            jf.flush()
            js_path = jf.name

        script = (
            f"import json\n"
            f"with open('{ans_path}') as f:\n"
            f"    _answers = json.load(f)\n"
            f"_ans_json = json.dumps(_answers)\n"
            f"with open('{js_path}') as f:\n"
            f"    _template = f.read()\n"
            f"_js = _template.replace('$_ANSWERS_JSON', _ans_json)\n"
            f"result = js(_js)\n"
            f"print(result)\n"
            f"print('RESULT: answers_applied')\n"
        )

        output, rc = await _run_harness(script, timeout=15, cdp_url=self._cdp_url)
        Path(ans_path).unlink(missing_ok=True)
        Path(js_path).unlink(missing_ok=True)
        logger.info("Apply answers (rc=%d): %s", rc, output[:200])
        return "APPLIED:" in output and rc == 0

    async def _click_next(self) -> bool:
        script = (
            "import time\n"
            'result = js("""\n'
            "(() => {\n"
            "    function findNextInFrames(doc) {\n"
            "        var selectors = [\n"
            "            'button[type=submit]', 'input[type=submit]',\n"
            "            'button.next', 'button.continue', 'a.next',\n"
            "            '[data-testid=next]', '[data-testid=submit]'\n"
            "        ];\n"
            "        for (var s = 0; s < selectors.length; s++) {\n"
            "            var el = doc.querySelector(selectors[s]);\n"
            "            if (el && el.offsetParent !== null) return el;\n"
            "        }\n"
            "        var keywords = ['next', 'continue', 'submit', 'proceed', 'done', 'finish', '>>'];\n"
            "        var btns = doc.querySelectorAll('button, input[type=submit], a, [role=button]');\n"
            "        for (var b = 0; b < btns.length; b++) {\n"
            "            var btn = btns[b];\n"
            "            var text = (btn.textContent || btn.value || '').toLowerCase().trim();\n"
            "            for (var k = 0; k < keywords.length; k++) {\n"
            "                if (text.indexOf(keywords[k]) >= 0 && btn.offsetParent !== null) {\n"
            "                    return btn;\n"
            "                }\n"
            "            }\n"
            "        }\n"
            "        var iframes = doc.querySelectorAll('iframe, frame');\n"
            "        for (var i = 0; i < iframes.length; i++) {\n"
            "            try {\n"
            "                var idoc = iframes[i].contentDocument || (iframes[i].contentWindow ? iframes[i].contentWindow.document : null);\n"
            "                if (idoc) {\n"
            "                    var found = findNextInFrames(idoc);\n"
            "                    if (found) return found;\n"
            "                }\n"
            "            } catch(e) {}\n"
            "        }\n"
            "        return null;\n"
            "    }\n"
            "    var btn = findNextInFrames(document);\n"
            "    if (btn) { btn.click(); return 'found'; }\n"
            "    return 'none';\n"
            "})()\n"
            '""")\n'
            "if result and result != 'none':\n"
            "    print(\"CLICKED:\", result)\n"
            "    time.sleep(1)\n"
            "    print(\"RESULT: next_clicked\")\n"
            "else:\n"
            "    print(\"RESULT: no_next_button\")\n"
        )
        output, rc = await _run_harness(script, timeout=15, cdp_url=self._cdp_url)
        logger.info("Click next (rc=%d): %s", rc, output[:200])
        return "next_clicked" in output or "CLICKED:" in output

    async def _get_page_text(self) -> str:
        output, _ = await _run_harness(
            "print(js('document.body ? document.body.innerText.substring(0, 2000) : \"\"'))",
            timeout=10,
            cdp_url=self._cdp_url,
        )
        return output.strip()

    async def _detect_captcha(self, page_data: dict) -> str | None:
        from ..captcha.solver import CaptchaDetector  # noqa: PLC0415

        page_text = page_data["text"]
        text_len = len(page_text.strip())

        # Only detect CAPTCHAs on sparse pages (< 400 chars).
        # Real survey pages have 500+ chars of content.
        # CAPTCHA pages typically have < 200 chars (just the instruction + image + input).
        # Pages with CAPTCHA text embedded in longer survey content are NOT CAPTCHA pages.
        if text_len > 400:
            return None

        # Check text patterns first
        challenge = CaptchaDetector.detect_from_page(page_text, page_data["forms"])
        if challenge:
            if challenge.captcha_type.value == "text_challenge":
                forms = page_data.get("forms", "")
                has_text_input = '"type": "text"' in forms or '"type":"text"' in forms or "textarea" in forms
                if not has_text_input:
                    return None
            logger.info("CAPTCHA detected: %s — %s", challenge.captcha_type.value, challenge.description)
            return f"{challenge.captcha_type.value}: {challenge.description}"

        # Check for draggable elements in forms (drag-and-drop CAPTCHA)
        forms_raw = page_data.get("forms", "[]")
        try:
            forms = json.loads(forms_raw) if forms_raw else []
            has_draggable = any(
                "drag" in str(el.get("cls", "")).lower() or el.get("tag", "").startswith("custom_") for el in forms
            )
            if has_draggable:
                return "drag_drop: draggable elements detected in page"
        except (json.JSONDecodeError, TypeError):
            pass

        return None

    def _read_captcha_with_vision(self, image_path: str) -> str:
        """Read CAPTCHA text using FreeModel Claude Haiku vision model."""
        try:
            import base64  # noqa: PLC0415
            import subprocess as sp  # noqa: PLC0415

            # Read API key from zshrc
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
                payload_path = f.name

            r = sp.run(
                [
                    "curl",
                    "-s",
                    "https://api.freemodel.dev/v1/chat/completions",
                    "-H",
                    "Authorization: Bearer " + key,
                    "-H",
                    "Content-Type: application/json",
                    "-d",
                    f"@{payload_path}",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            Path(payload_path).unlink(missing_ok=True)

            if r.stdout:
                resp = json.loads(r.stdout)
                content = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                # Clean up: remove non-alphanumeric chars
                content = re.sub(r"[^A-Za-z0-9]", "", content)
                if content and 2 <= len(content) <= 20:
                    return content
        except Exception as e:
            logger.debug("Vision CAPTCHA failed: %s", e)
        return ""

    async def _try_solve_text_captcha(self) -> bool:  # noqa: PLR0915
        """Solve text/image CAPTCHAs using cua-driver screenshots + OCR."""
        import base64  # noqa: PLC0415
        import subprocess as sp  # noqa: PLC0415

        # Step 1: Take a real screenshot via cua-driver (not browser-harness)
        try:
            chrome_pid = self._chrome_proc.pid if self._chrome_proc else None
            if not chrome_pid:
                # Find the harness Chrome by its debug port
                import psutil  # noqa: PLC0415

                for proc in psutil.process_iter(["pid", "cmdline"]):
                    try:
                        cmdline = " ".join(proc.info["cmdline"] or [])
                        if f"remote-debugging-port={CDP_PORT}" in cmdline:
                            chrome_pid = proc.info["pid"]
                            break
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue

            if not chrome_pid:
                logger.warning("Could not find Chrome PID for screenshot")
                return False

            # Find Chrome window ID
            result = sp.run(
                ["cua-driver", "call", "list_windows", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            windows = json.loads(result.stdout.strip()).get("windows", [])
            wid = None
            best_area = 0
            # Match by PID (harness Chrome) — pick the LARGEST content window
            for w in windows:
                if w.get("app_name") == "Google Chrome" and w.get("pid") == chrome_pid:
                    b = w.get("bounds", {})
                    w_area = b.get("width", 0) * b.get("height", 0)
                    # Content windows are >100px tall (toolbars are ~33px)
                    if w_area > 10000 and b.get("height", 0) > 100 and w_area > best_area:
                        wid = w["window_id"]
                        best_area = w_area
            # Fallback: any Chrome window with a title
            if not wid:
                for w in windows:
                    if w.get("app_name") == "Google Chrome" and w.get("title"):
                        b = w.get("bounds", {})
                        w_area = b.get("width", 0) * b.get("height", 0)
                        if w_area > 10000 and b.get("height", 0) > 100 and w_area > best_area:
                            wid = w["window_id"]
                            best_area = w_area
            if wid:
                logger.info("CAPTCHA screenshot window: wid=%s area=%d", wid, best_area)

            if not wid:
                logger.warning("Could not find Chrome window for screenshot (pid=%s)", chrome_pid)
                return False
            logger.info("CAPTCHA screenshot: pid=%s wid=%s", chrome_pid, wid)

            # Take screenshot
            result = sp.run(
                [
                    "cua-driver",
                    "call",
                    "get_window_state",
                    json.dumps({"pid": chrome_pid, "window_id": wid, "max_elements": 200}),
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            data = json.loads(result.stdout.strip())
            screenshot_b64 = data.get("screenshot_png_b64", "")

            if not screenshot_b64:
                logger.warning("cua-driver returned no screenshot")
                return False

            # Save screenshot
            with open("/tmp/captcha_full.png", "wb") as f:
                f.write(base64.b64decode(screenshot_b64))
            logger.info(
                "Screenshot saved via cua-driver (%d chars b64, pid=%s, wid=%s)", len(screenshot_b64), chrome_pid, wid
            )

        except Exception as e:
            logger.error("cua-driver screenshot failed: %s", e)
            return False

        # Step 2: Find CAPTCHA image in the screenshot using AX tree elements
        captcha_rect = None
        elements = data.get("elements", [])
        screenshot_w = data.get("screenshot_width", 1568)
        screenshot_h = data.get("screenshot_height", 983)

        # First try: find image with CAPTCHA-related label
        for el in elements:
            label = (el.get("label") or "").lower()
            role = el.get("role", "")
            if role == "AXImage" and any(kw in label for kw in ["captcha", "verify", "code", "security"]):
                frame = el.get("frame", {})
                captcha_rect = {
                    "x": frame.get("x", 0),
                    "y": frame.get("y", 0),
                    "w": frame.get("w", 0),
                    "h": frame.get("h", 0),
                }
                logger.info("Found CAPTCHA image by label: %s", captcha_rect)
                break

        # Second try: find image near a text input (proximity-based)
        if not captcha_rect:
            text_inputs = [e for e in elements if e.get("role") in ("AXTextArea", "AXTextField")]
            images = [e for e in elements if e.get("role") == "AXImage"]
            for img_el in images:
                iframe = img_el.get("frame", {})
                iw, ih = iframe.get("w", 0), iframe.get("h", 0)
                # CAPTCHA images are typically 80-500px wide, 20-150px tall
                if 80 < iw < 500 and 20 < ih < 150:
                    for inp in text_inputs:
                        inpframe = inp.get("frame", {})
                        # Image should be above or near the input (within 200px vertically)
                        if abs(iframe.get("y", 0) + ih - inpframe.get("y", 0)) < 200:
                            captcha_rect = {"x": iframe.get("x", 0), "y": iframe.get("y", 0), "w": iw, "h": ih}
                            logger.info("Found CAPTCHA image by proximity: %s", captcha_rect)
                            break
                if captcha_rect:
                    break

        # Step 3: OCR the CAPTCHA
        try:
            import pytesseract  # noqa: PLC0415
            from PIL import Image, ImageFilter, ImageOps  # noqa: PLC0415

            img = Image.open("/tmp/captcha_full.png")

            if captcha_rect and captcha_rect["w"] > 20:
                # Scale AX coordinates to screenshot pixels (handles Retina displays)
                ax_max_x = (
                    max(el.get("frame", {}).get("x", 0) + el.get("frame", {}).get("w", 0) for el in elements) or 1
                )
                ax_max_y = (
                    max(el.get("frame", {}).get("y", 0) + el.get("frame", {}).get("h", 0) for el in elements) or 1
                )
                scale_x = screenshot_w / ax_max_x if ax_max_x > 0 else 1
                scale_y = screenshot_h / ax_max_y if ax_max_y > 0 else 1
                x = max(0, int(captcha_rect["x"] * scale_x) - 10)
                y = max(0, int(captcha_rect["y"] * scale_y) - 10)
                w = int(captcha_rect["w"] * scale_x) + 20
                h = int(captcha_rect["h"] * scale_y) + 20
                captcha_img = img.crop((x, y, min(img.width, x + w), min(img.height, y + h)))
                logger.info("Cropped CAPTCHA: (%d,%d,%d,%d) scale=%.2f", x, y, w, h, scale_x)
            else:
                # No specific CAPTCHA image found — crop center area where CAPTCHAs typically appear
                # CAPTCHAs are usually in the center-upper area of the page
                cx, cy = img.width // 2, img.height // 3
                crop_w, crop_h = min(400, img.width // 2), min(150, img.height // 3)
                captcha_img = img.crop((cx - crop_w // 2, cy - crop_h // 2, cx + crop_w // 2, cy + crop_h // 2))
                logger.info(
                    "No CAPTCHA image found — cropping center area (%d,%d,%d,%d)",
                    cx - crop_w // 2,
                    cy - crop_h // 2,
                    crop_w,
                    crop_h,
                )

            captcha_img.save("/tmp/captcha_cropped.png")

            # Try vision model first (FreeModel Claude Haiku)
            ocr_text = self._read_captcha_with_vision("/tmp/captcha_cropped.png")
            if ocr_text:
                logger.info("Vision model CAPTCHA: '%s'", ocr_text)
            else:
                # Fallback to Tesseract OCR
                logger.info("Vision model failed, falling back to Tesseract")

            if not ocr_text:
                # OCR preprocessing — try multiple binarization thresholds
                configs = [
                    r"--oem 3 --psm 8 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
                    r"--oem 3 --psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
                    r"--oem 3 --psm 7",
                    r"--oem 3 --psm 13",
                ]
                results = []
                for threshold in [80, 128, 180]:
                    gray = captcha_img.convert("L")
                    gray = ImageOps.autocontrast(gray, cutoff=5)
                    gray = gray.resize((gray.width * 4, gray.height * 4), Image.LANCZOS)
                    gray = gray.filter(ImageFilter.SHARPEN)
                    bw = gray.point(lambda p, t=threshold: 255 if p > t else 0, mode="1")
                    for config in configs:
                        for source in [bw, gray]:
                            try:
                                text = pytesseract.image_to_string(source, config=config).strip()
                                text = re.sub(r"[^A-Za-z0-9]", "", text)
                                # Filter out garbage results (repeated chars, common UI text)
                                if text and 2 <= len(text) <= 20 and len(set(text)) > 1:
                                    results.append(text)
                            except Exception:
                                pass

                if not results:
                    logger.warning("OCR returned no results from cua-driver screenshot")
                    return False

                from collections import Counter  # noqa: PLC0415

                counts = Counter(results)
                ocr_text = counts.most_common(1)[0][0]
                logger.info("OCR results: %s -> picked: '%s'", results[:5], ocr_text)

            # Fill in the answer
            escaped = ocr_text.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"')
            fill_script = (
                f"fill_input('input[type=text], input[type=password], textarea', '{escaped}', clear_first=True)\n"
                "import time; time.sleep(1)\n"
                "js(\"document.querySelector('button[type=submit], input[type=submit], button')?.click()\")\n"
                "time.sleep(2)\n"
                "wait_for_load()\n"
                "print('RESULT: captcha_filled')\n"
            )
            fill_out, _ = await _run_harness(fill_script, timeout=15, cdp_url=self._cdp_url)
            return "captcha_filled" in fill_out

        except Exception as e:
            logger.error("OCR failed: %s", e, exc_info=True)
            return False

    async def _try_solve_drag_captcha(self) -> bool:
        """Solve drag-and-drop CAPTCHAs using mouse events (mousedown/mousemove/mouseup)."""
        # Write the JS to a file to avoid triple-quote escaping issues
        drag_js = (
            "(() => {\n"
            "    const sources = document.querySelectorAll('[draggable=true], [class*=drag], [class*=source], [class*=number-box]');\n"
            "    const targets = document.querySelectorAll('[class*=drop], [class*=target], [class*=zone], [class*=answer-box]');\n"
            "    let src = sources[0] || document.querySelector('[draggable=true]');\n"
            "    let tgt = targets[0] || null;\n"
            "    if (!src) return 'no_source';\n"
            "    // If no explicit target, try to find a drop zone near the source\n"
            "    if (!tgt) {\n"
            "        const allEls = document.querySelectorAll('[class*=box], [class*=area], [class*=container], [class*=slot]');\n"
            "        const sRect = src.getBoundingClientRect();\n"
            "        for (const el of allEls) {\n"
            "            const r = el.getBoundingClientRect();\n"
            "            if (r.width > 50 && r.height > 50 && el !== src && !el.contains(src)) {\n"
            "                tgt = el; break;\n"
            "            }\n"
            "        }\n"
            "    }\n"
            "    if (!tgt) return 'no_target';\n"
            "    const sRect = src.getBoundingClientRect();\n"
            "    const tRect = tgt.getBoundingClientRect();\n"
            "    const sx = sRect.x + sRect.width/2;\n"
            "    const sy = sRect.y + sRect.height/2;\n"
            "    const tx = tRect.x + tRect.width/2;\n"
            "    const ty = tRect.y + tRect.height/2;\n"
            "    // Use mousedown/mousemove/mouseup (works better than DragEvent for many CAPTCHAs)\n"
            "    const opts = (x, y) => ({bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0});\n"
            "    src.dispatchEvent(new MouseEvent('mousedown', opts(sx, sy)));\n"
            "    // Move in steps for realism\n"
            "    const steps = 10;\n"
            "    for (let i = 1; i <= steps; i++) {\n"
            "        const mx = sx + (tx - sx) * i / steps;\n"
            "        const my = sy + (ty - sy) * i / steps;\n"
            "        document.elementFromPoint(mx, my)?.dispatchEvent(new MouseEvent('mousemove', opts(mx, my)));\n"
            "    }\n"
            "    tgt.dispatchEvent(new MouseEvent('mouseup', opts(tx, ty)));\n"
            "    // Also try pointer events (some libraries use these)\n"
            "    src.dispatchEvent(new PointerEvent('pointerdown', opts(sx, sy)));\n"
            "    for (let i = 1; i <= steps; i++) {\n"
            "        const mx = sx + (tx - sx) * i / steps;\n"
            "        const my = sy + (ty - sy) * i / steps;\n"
            "        document.elementFromPoint(mx, my)?.dispatchEvent(new PointerEvent('pointermove', opts(mx, my)));\n"
            "    }\n"
            "    tgt.dispatchEvent(new PointerEvent('pointerup', opts(tx, ty)));\n"
            "    return 'dragged:' + Math.round(sx) + ',' + Math.round(sy) + '->' + Math.round(tx) + ',' + Math.round(ty);\n"
            "})()"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, prefix="bh_drag_") as jf:
            jf.write(drag_js)
            jf.flush()
            drag_js_path = jf.name

        script = (
            "import time\n"
            f"with open('{drag_js_path}') as f:\n"
            "    _drag_code = f.read()\n"
            "result = js(_drag_code)\n"
            "print('DRAG_RESULT:', result)\n"
            "if result and result.startswith('dragged:'):\n"
            "    time.sleep(2)\n"
            "    print('RESULT: drag_solved')\n"
            "else:\n"
            "    print('RESULT: drag_failed')\n"
        )
        output, rc = await _run_harness(script, timeout=20, cdp_url=self._cdp_url)
        Path(drag_js_path).unlink(missing_ok=True)
        logger.info("Drag CAPTCHA: %s", output[:200])
        return "drag_solved" in output

    async def check_balance(self, dashboard_url: str) -> AgentResult:
        script = _build_balance_script(dashboard_url)
        output, rc = await _run_harness(script, timeout=30, cdp_url=self._cdp_url)
        return AgentResult(success=rc == 0, output=output)

    async def withdraw(self, redeem_url: str, method: str = "paypal") -> AgentResult:
        return await self._llm_execute(
            task=(
                f"Navigate to {redeem_url} and withdraw earnings via {method}. "
                f"Use browser-harness helpers. Find the {method} option, select max amount, confirm. "
                "Print RESULT: withdraw_success or RESULT: withdraw_failed with details."
            ),
            timeout=120,
        )

    async def _llm_execute(self, task: str, timeout: float = 120, max_retries: int = 2) -> AgentResult:
        """Use LLM to generate and execute browser-harness code with self-healing retry."""
        from browser_use.llm.messages import SystemMessage, UserMessage  # noqa: PLC0415

        from ..llm_provider import get_llm  # noqa: PLC0415

        llm = get_llm(
            provider=self._llm_provider,
            model=self._llm_model,
            api_key=self._llm_api_key,
            base_url=self._llm_base_url,
        )
        inner = getattr(llm, "_inner", llm)

        system = (
            "You are a browser automation expert. Generate ONLY valid Python code "
            "for the browser-harness CLI. No explanations, no markdown fences.\n\n"
            "Available helpers: page_info(), current_tab(), new_tab(url), goto_url(url), "
            "wait_for_load(), click_at_xy(x,y), fill_input(sel,text), type_text(text), "
            "press_key(key), js(code), scroll(dx,dy), capture_screenshot(), "
            + "ensure_real_tab(), list_tabs(), close_tab(target_id)\n\n"
            "CRITICAL:\n"
            "- NEVER use current_url() — use current_tab()['url']\n"
            "- page_info() returns dict with {url, title, w, h} — NO text\n"
            "- To read page text: js('document.body.innerText.substring(0, 2000)')\n"
            "- Define main() AND call it at the end\n"
            "- Print RESULT: lines"
        )

        lc_messages = [SystemMessage(content=system), UserMessage(content=task)]
        output = ""

        for attempt in range(max_retries + 1):
            try:
                result = await inner.ainvoke(lc_messages)
                code = result.completion if hasattr(result, "completion") else str(result)
                code = re.sub(r"<think>.*?</think>\s*", "", code, flags=re.DOTALL).strip()
                code = re.sub(r"^```(?:python)?\s*", "", code.strip())
                code = re.sub(r"\s*```$", "", code.strip())
                code = _fix_bad_helpers(code)
                code = _wrap_with_safety_net(code)

                logger.info("Generated harness code (attempt %d, %d chars)", attempt + 1, len(code))
                output, rc = await _run_harness(code, timeout=timeout, cdp_url=self._cdp_url)
                logger.info("Script output (rc=%d):\n%s", rc, output[:500])

                success = any(
                    marker in output
                    for marker in [
                        "RESULT: success",
                        "RESULT: complete",
                        "RESULT: survey_complete",
                        "RESULT: already_logged_in",
                        "RESULT: login_success",
                    ]
                )
                if success:
                    return AgentResult(success=True, output=output)

                logger.warning("Script failed (attempt %d, rc=%d), retrying", attempt + 1, rc)
                error_snippet = output[-500:] if len(output) > 500 else output
                lc_messages.append(UserMessage(content=code))
                lc_messages.append(
                    UserMessage(
                        content=(
                            f"Your code failed with rc={rc}.\n\n"
                            f"Error:\n{error_snippet}\n\n"
                            "Fix the code. Common mistakes:\n"
                            "- current_url() does NOT exist → use current_tab()['url']\n"
                            "- click_element() does NOT exist → use js('document.querySelector(sel).click()')\n"
                            "- page_info() returns a dict with no text → use js('document.body.innerText')\n\n"
                            "Generate ONLY the fixed Python code."
                        )
                    )
                )

            except Exception as e:
                logger.error("LLM execute failed (attempt %d): %s", attempt + 1, e, exc_info=True)
                if attempt == max_retries:
                    return AgentResult(success=False, error=str(e))

        return AgentResult(success=False, error=f"Failed after {max_retries + 1} attempts", output=output)
