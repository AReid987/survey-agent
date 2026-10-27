#!/usr/bin/env python3
"""
TopSurveys Agent — production survey automation for topsurveys.app
Connects to existing ego-lite CDP session. Answers surveys. Verifies balance.
"""

import asyncio
import json
import os
import random
from datetime import datetime

from playwright.async_api import async_playwright

CDP_URL = "http://127.0.0.1:9222"
LOG = os.path.expanduser("~/survey-agent/data/survey_run.log")
STATE_FILE = os.path.expanduser("~/survey-agent/data/agent_state.json")
os.makedirs(os.path.dirname(LOG), exist_ok=True)

# ============================================================
# User Profile — matched against question options
# ============================================================
PROFILE_MAP = {
    "employment": ["self-employed", "self employed", "freelance", "business owner"],
    "work_hours": ["30-40", "30_40", "30 to 40", "more than 40", "40+", "over 40"],
    "income": ["50,000", "50k", "50_75", "50k_75k", "$50,000", "$75,000", "50", "75", "100"],
    "gender": ["male", "man"],
    "age": ["35", "39", "30-39", "35-44", "35 to 44"],
    "hispanic": ["no", "not hispanic", "non-hispanic"],
    "race": ["black", "african american", "african"],
    "state": ["texas", "tx"],
    "education": ["bachelor", "college", "university", "bachelor's", "bachelors"],
    "children": ["yes", "2", "3", "4"],
    "marital": ["single", "never married"],
    "smoke": ["former", "formerly", "used to"],
    "drink": ["never", "don't drink", "do not drink"],
    "home": ["rent", "renting"],
}


def log(m):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {m}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {"total_completed": 0, "total_screened": 0, "total_failed": 0, "balance_history": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def match_answer(question_text, options):
    """Match a question to the user profile. Returns the best option value or None."""
    q = question_text.lower().strip()

    # Extract the question topic
    topic_keywords = []
    for cat, kws in PROFILE_MAP.items():
        if any(
            kw in q
            for kw in [
                # Employment
                "employment",
                "employ",
                "job",
                "work status",
                "occupation",
                "profession",
                # Hours
                "hour",
                "week",
                "time",
                # Income
                "income",
                "earn",
                "salary",
                "household",
                "$",
                # Gender
                "gender",
                "sex",
                "male",
                "female",
                # Age
                "age",
                "old",
                "birth",
                "born",
                # Hispanic
                "hispanic",
                "latino",
                "spanish",
                # Race
                "race",
                "ethnicity",
                "ethnic background",
                # State/Location
                "state",
                "live",
                "reside",
                "location",
                "where",
                # Education
                "education",
                "degree",
                "school",
                "college",
                "graduate",
                "study",
                # Children
                "children",
                "child",
                "kids",
                "dependant",
                "dependent",
                # Marital
                "marital",
                "married",
                "relationship",
                "single",
                # Smoke
                "smoke",
                "tobacco",
                "cigarette",
                "vape",
                # Drink
                "drink",
                "alcohol",
                "beer",
                "wine",
                "liquor",
                # Home
                "home",
                "rent",
                "own",
                "house",
                "apartment",
                "live",
            ]
        ):
            topic_keywords = kws
            break
        if topic_keywords:
            break

    # Try exact value match first
    for opt in options:
        ot = opt["text"].lower().strip()
        if not ot:
            continue
        if ot in ["select one", "please select", "---select---", "choose...", "—select—"]:
            continue

        # Direct value match
        if topic_keywords:
            if any(kw in ot for kw in topic_keywords):
                return opt["value"]

    # Fallback: first non-"prefer not to say" option
    for opt in options:
        ot = opt["text"].lower().strip()
        if (
            "prefer" not in ot
            and "not to say" not in ot
            and "rather not" not in ot
            and "skip" not in ot
            and "none" not in ot
        ):
            return opt["value"]

    # Last resort
    return options[0]["value"] if options else None


# ============================================================
# Page interaction helpers
# ============================================================


async def get_balance(page):
    """Read balance from the nav bar."""
    try:
        text = await page.evaluate("""() => {
            const el = document.querySelector("#balance-display");
            return el?.innerText?.trim() || null;
        }""")
        if text:
            return float(text.replace("$", "").replace(",", ""))
    except:
        pass
    return None


async def find_surveys(page):
    """Return list of {href, title, reward, minutes} from dashboard."""
    try:
        return await page.evaluate("""() => {
            const cards = document.querySelectorAll('a[href*="/survey/start/"]');
            return [...cards].map(a => {
                const text = a.innerText.trim();
                const lines = text.split('\\n').map(s => s.trim()).filter(Boolean);
                const rewardMatch = text.match(/\\$([0-9.]+)/);
                const timeMatch = text.match(/(\\d+)\\s*min/);
                return {
                    href: a.href,
                    title: lines[0] || 'Unknown',
                    reward: rewardMatch ? parseFloat(rewardMatch[1]) : 0,
                    minutes: timeMatch ? parseInt(timeMatch[1]) : 0,
                    text: text
                };
            });
        }""")
    except:
        return []


async def answer_question(page, q_num):
    """Answer one survey question. Returns True if page should reload, False if done/failed."""
    # Check for terminal states
    state = await page.evaluate("""() => {
        const text = document.body.innerText.toLowerCase();
        return {
            completed: text.includes('thank you') || text.includes('completed') ||
                       text.includes('finished') || text.includes('earned') ||
                       text.includes('congratulations'),
            dq: text.includes('not a good match') || text.includes('not qualify') ||
                text.includes('disqualif') || text.includes('screen out') ||
                text.includes('not eligible') || text.includes('unfortunately') ||
                text.includes('does not match'),
        };
    }""")

    if state["completed"]:
        return "completed"
    if state["dq"]:
        return "screened_out"

    # Get question form data
    q_data = await page.evaluate("""() => {
        const label = document.querySelector('label.block.text-lg.font-medium, label:has(+ div input[type="radio"]), h2, .question-text');
        const inputs = [...document.querySelectorAll('input[type="radio"]:not([type="hidden"])')].map(r => ({
            name: r.name,
            value: r.value,
            text: (r.closest('label')?.querySelector('span')?.innerText?.trim() ||
                   r.closest('label')?.innerText?.trim()?.replace(r.value, '')?.trim() ||
                   '')
        }));
        // Fallback to any radio buttons with labels
        if (inputs.length === 0) {
            const allRadios = document.querySelectorAll('input[type="radio"]');
            return {question: '', options: [...allRadios].map(r => ({
                name: r.name,
                value: r.value,
                text: (r.closest('label')?.innerText?.trim() || '')
            }))};
        }
        return {
            question: label?.innerText?.trim() || document.body.innerText.substring(0, 200),
            options: inputs
        };
    }""")

    if not q_data["options"] or len(q_data["options"]) == 0:
        # Check for select dropdowns
        select_data = await page.evaluate("""() => {
            const sel = document.querySelector('select');
            if (!sel) return null;
            return {
                question: document.querySelector('label[for="' + sel.id + '"]')?.innerText?.trim() || '',
                options: [...sel.options].slice(1).map(o => ({
                    name: sel.name,
                    value: o.value,
                    text: o.text.trim()
                }))
            };
        }""")
        if select_data and select_data["options"]:
            q_data = select_data

    if not q_data["options"] or len(q_data["options"]) == 0:
        log(f"  Q{q_num}: No interactive elements found")
        # Check for text boxes
        text_input = await page.evaluate("""() => {
            const ta = document.querySelector('textarea, input[type="text"], input[type="number"]');
            if (!ta) return null;
            const label = document.querySelector('label[for="' + ta.id + '"]')?.innerText?.trim() || '';
            return {question: label, element: ta.id || ta.name || 'input'};
        }""")
        if text_input:
            log(f"  Q{q_num}: Text input found - filling")
            is_zip = "postal" in str(text_input).lower() or "zip" in str(text_input).lower()
            is_age = "age" in str(text_input).lower() or "birth" in str(text_input).lower()
            value = "78701" if is_zip else ("39" if is_age else "I carefully consider my options.")
            await page.evaluate(
                """(v) => {
                const el = document.querySelector('textarea, input[type="text"], input[type="number"]');
                if (el) { el.value = v; el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); }
            }""",
                value,
            )
            await submit_form(page)
            return True

        log(f"  Q{q_num}: Stuck")
        return "stuck"

    # Match answer
    chosen = match_answer(q_data["question"], q_data["options"])
    if not chosen:
        chosen = q_data["options"][0]["value"]

    chosen_text = next((o["text"][:40] for o in q_data["options"] if o["value"] == chosen), chosen)
    log(f"  Q{q_num}: {q_data['question'][:60]} -> {chosen_text}")

    # Submit via fetch + replace (the site returns full HTML on each POST)
    try:
        result = await page.evaluate(
            """(payload) => {
            const form = document.querySelector('form');
            const action = form?.action || window.location.href;
            return fetch(action, {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: payload,
                credentials: 'same-origin'
            }).then(r => r.text());
        }""",
            f"{q_data['options'][0]['name']}={chosen}",
        )
    except Exception as e:
        log(f"  Q{q_num}: Fetch error: {e}")
        return "stuck"

    await page.evaluate("(html) => { document.open(); document.write(html); document.close(); }", result)
    await asyncio.sleep(random.uniform(0.8, 1.8))
    return True


async def submit_form(page):
    """Click submit/next button on current form."""
    await page.evaluate("""() => {
        const btn = document.querySelector('button[type="submit"], button:has-text("Next"), button:has-text("Continue")');
        if (btn) btn.click();
    }""")
    await asyncio.sleep(1)


# ============================================================
# Survey lifecycle
# ============================================================


async def do_survey(page, survey, initial_balance):
    """
    Complete one survey. Returns (result_str, final_balance).
    result: 'completed' (balance increased), 'screened_out', 'failed_*'
    """
    log(f"→ Starting: {survey['title']} (${survey['reward']}, {survey['minutes']}min)")

    try:
        await page.goto(survey["href"], wait_until="networkidle", timeout=15000)
    except:
        await page.goto(survey["href"], wait_until="load", timeout=15000)
    await asyncio.sleep(random.uniform(1, 2.5))

    for q_num in range(1, 16):  # max 15 questions per survey
        result = await answer_question(page, q_num)
        if result == "completed":
            log("  ✓ Survey completed (thank you page)")
            break
        if result == "screened_out":
            log("  ○ Screened out (accepted success)")
            return "screened_out", initial_balance
        if result == "stuck":
            log(f"  ✗ Stuck at Q{q_num}")
            return "failed_stuck", initial_balance

    # Navigate back to dashboard and check balance
    await asyncio.sleep(random.uniform(1, 3))
    try:
        await page.goto("https://app.topsurveys.app/", wait_until="networkidle", timeout=15000)
    except:
        await page.goto("https://app.topsurveys.app/", wait_until="load", timeout=15000)
    await asyncio.sleep(random.uniform(1, 2))

    final_balance = await get_balance(page)
    if final_balance is None:
        log("  ✗ Could not read balance")
        return "failed_balance_read", initial_balance

    delta = final_balance - initial_balance
    if delta > 0:
        log(f"  ✓✓ BALANCE INCREASED: ${initial_balance:.2f} → ${final_balance:.2f} (+${delta:.2f})")
        return "completed", final_balance
    else:
        log(f"  ∼ No balance change (${initial_balance:.2f} → ${final_balance:.2f})")
        return "failed_no_change", final_balance


async def run_session(page, task_count):
    """Run one session of task_count surveys."""
    initial_balance = await get_balance(page)
    if initial_balance is None:
        log("! Could not read initial balance")
        initial_balance = 0.0

    log(f"=== SESSION: {task_count} surveys (balance: ${initial_balance:.2f}) ===")

    # Refresh survey list
    try:
        await page.goto("https://app.topsurveys.app/", wait_until="networkidle", timeout=15000)
    except:
        pass
    await asyncio.sleep(1)

    completed = 0
    screened = 0
    failed = 0
    current_balance = initial_balance

    for task_num in range(1, task_count + 1):
        log(f"--- Task {task_num}/{task_count} ---")
        surveys = await find_surveys(page)
        if not surveys:
            log("  No surveys available — waiting 30s")
            await asyncio.sleep(30)
            failed += 1
            continue

        # Pick highest-paying survey
        survey = max(surveys, key=lambda s: s["reward"])

        result, new_balance = await do_survey(page, survey, current_balance)

        if result == "completed":
            completed += 1
            current_balance = new_balance if new_balance and new_balance > current_balance else current_balance
        elif result == "screened_out":
            screened += 1
        else:
            failed += 1

        # Brief pause between surveys
        await asyncio.sleep(random.uniform(3, 8))

    log(f"=== SESSION RESULTS: completed={completed}, screened={screened}, failed={failed} ===")
    return completed, screened, failed


# ============================================================
# Main orchestrator — indefinite loop
# ============================================================


async def main():
    log("=" * 60)
    log("TOPSURVEYS AGENT v1.0 — Autonomous Survey Automation")
    log("=" * 60)

    state = load_state()
    log(
        f"Loaded state: {state['total_completed']} completed, {state['total_screened']} screened, {state['total_failed']} failed"
    )

    async with async_playwright() as pw:
        log("Connecting to ego-lite CDP...")
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        ctx = browser.contexts[0]

        # Find or create topsurveys page
        page = None
        for pg in ctx.pages:
            if "topsurveys.app" in pg.url:
                page = pg
                break

        if not page:
            log("No topsurveys.app tab found — opening one")
            page = await ctx.new_page()
            await page.goto("https://app.topsurveys.app/", wait_until="networkidle", timeout=30000)

        # Verify we can interact
        await page.bring_to_front()
        await page.goto("https://app.topsurveys.app/", wait_until="networkidle", timeout=30000)
        balance = await get_balance(page)
        log(f"Connected. Balance: ${balance:.2f}" if balance else "Connected. Balance: unknown")

        session_num = 0
        while True:
            session_num += 1
            task_count = random.randint(8, 12)
            log(f"\n{'=' * 60}")
            log(f"SESSION {session_num} — {task_count} tasks")
            log(f"{'=' * 60}")

            try:
                c, s, f = await run_session(page, task_count)
                state["total_completed"] += c
                state["total_screened"] += s
                state["total_failed"] += f
                save_state(state)
            except Exception as e:
                log(f"SESSION ERROR: {e}")
                import traceback

                traceback.print_exc()

            # Random pause
            pause_min = random.randint(15, 45)
            log(f"\nPausing {pause_min} minutes...")
            log(
                f"Total to date: {state['total_completed']} completed, {state['total_screened']} screened, {state['total_failed']} failed"
            )
            await asyncio.sleep(pause_min * 60)

            # Heartbeat — keep page alive during long pause
            try:
                await page.evaluate("document.title")
            except:
                log("Page disconnected — reconnecting...")
                for pg in ctx.pages:
                    if "topsurveys.app" in pg.url:
                        page = pg
                        break
                if not page:
                    page = await ctx.new_page()
                    await page.goto("https://app.topsurveys.app/")


if __name__ == "__main__":
    asyncio.run(main())
