#!/usr/bin/env python3
"""Fully autonomous survey automation: screencapture + vision AI + PyAutoGUI.

No computer_use, no cua-driver, no Playwright login issues.
Uses ego-lite browser (already logged in) + PyAutoGUI for all interactions.
Vision AI analyzes screenshots to decide what to do.
"""

import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    import pyautogui
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0.15
except ImportError:
    print("ERROR: pyautogui not installed. Run: pdm add pyautogui")
    sys.exit(1)

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: pdm add httpx")
    sys.exit(1)

# ── Configuration ──────────────────────────────────────────────────────

CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
CEREBRAS_ENDPOINT = "https://api.cerebras.ai/v1/chat/completions"
CEREBRAS_MODEL = "gemma-4-31b"

# ego-lite window position (macOS)
EGO_WINDOW = {"x": 0, "y": 34, "w": 1028, "h": 948}
CHROME_HEIGHT = 95  # toolbar + address bar height
WEB_AREA_Y = EGO_WINDOW["y"] + CHROME_HEIGHT  # y offset to web content

# Survey profile
PROFILE = {
    "age": "39", "gender": "Male", "state": "Texas", "zip": "78701",
    "income_household": "$60,000 ~ $79,999",
    "income_individual": "$40,000 ~ $59,999",
    "race": "Black or African American",
    "employment": "Self-employed", "education": "Bachelor's degree",
    "children": "3", "marital": "Single",
    "smoker": "Former smoker", "drinker": "Never",
    "weight": "200", "height_ft": "5", "height_in": "10",
}

# ── Screenshot ─────────────────────────────────────────────────────────

def capture_ego_lite() -> str:
    """Take a screenshot of the ego-lite window, return path."""
    path = "/tmp/ego_survey.png"
    x, y, w, h = EGO_WINDOW["x"], EGO_WINDOW["y"], EGO_WINDOW["w"], EGO_WINDOW["h"]
    subprocess.run(
        ["screencapture", "-x", "-R", f"{x},{y},{w},{h}", path],
        timeout=5, check=True
    )
    return path


def image_to_base64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


# ── Vision AI ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a survey automation assistant. You analyze screenshots of survey pages and decide what action to take.

RESPOND WITH ONLY VALID JSON (no markdown, no explanation). Format:
{
  "action": "click_radio|click_checkbox|type_text|select_dropdown|drag_drop|click_button|completed|disqualified|unknown",
  "target": "description of what to interact with",
  "answer": "the answer to select or type (for radio/checkbox/text)",
  "coords": [x, y] or null,
  "reasoning": "brief explanation"
}

RULES:
- For radio buttons: action="click_radio", answer="the option text to select"
- For text inputs: action="type_text", answer="the text to type", target="field description"
- For dropdowns: action="select_dropdown", answer="the option to select"
- For drag-and-drop: action="drag_drop", coords=[[x1,y1],[x2,y2]] for each drag operation
- For Continue/Next buttons: action="click_button", target="Continue"
- For completion pages: action="completed"
- For disqualification: action="disqualified"
- Use the SURVEY PROFILE to select appropriate answers
- Always prefer answers that match the profile demographics
- For brand/product questions, usually answer "Yes" to show engagement
- For "how many times" questions, pick moderate options (2-3 times)
- For age: always 39
- For gender: always Male
- For state: always Texas
- For zip: always 78701
- For income household: always $60,000 ~ $79,999
- For income individual: always $40,000 ~ $59,999
- For race: always Black or African American
- For employment: always Self-employed
"""


def analyze_screenshot(image_path: str) -> dict:
    """Send screenshot to Cerebras vision API, return action dict."""
    b64 = image_to_base64(image_path)
    
    profile_text = "\n".join(f"- {k}: {v}" for k, v in PROFILE.items())
    user_prompt = f"""Analyze this survey page screenshot and decide what action to take.

SURVEY PROFILE:
{profile_text}

What should I do next? Respond with ONLY valid JSON."""

    payload = {
        "model": CEREBRAS_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": user_prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
            ]}
        ],
        "max_tokens": 500,
        "temperature": 0.1,
    }

    headers = {
        "Authorization": f"Bearer {CEREBRAS_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        resp = httpx.post(CEREBRAS_ENDPOINT, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()
        
        # Extract JSON from response (handle markdown code blocks)
        json_match = re.search(r'\{[\s\S]*\}', content)
        if json_match:
            return json.loads(json_match.group())
        return {"action": "unknown", "reasoning": f"Could not parse: {content[:200]}"}
    except Exception as e:
        return {"action": "error", "reasoning": str(e)[:200]}


# ── Action Execution ───────────────────────────────────────────────────

def get_current_url() -> str:
    """Get the current URL from ego-lite via osascript."""
    try:
        result = subprocess.run(
            ["osascript", "-e", 'tell application "ego lite" to get URL of active tab of front window'],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip()
    except Exception:
        return ""


def navigate_ego_lite(url: str):
    """Navigate ego-lite to a URL via osascript."""
    subprocess.run(
        ["osascript", "-e", f'tell application "ego lite" to open location "{url}"'],
        timeout=10
    )
    time.sleep(8)


def click_radio_by_text(option_text: str) -> bool:
    """Click a radio button by its label text using Tab+Space navigation.
    
    Strategy: Tab through radio options, Space to select when focused.
    For the first option (Yes/Male), just Tab+Space.
    For other options, count tabs needed.
    """
    # Common option orders
    yes_no_options = ["Yes", "No, but", "No"]
    gender_options = ["Male", "Female"]
    income_options = ["Less than", "$20,000", "$40,000", "$60,000", "$80,000", 
                      "$100,000", "$120,000", "$150,000", "$200,000", "Not sure"]
    
    text_lower = option_text.lower().strip()
    
    # Determine how many tabs to reach the option
    if text_lower in ["yes", "male", "less than"]:
        tabs = 1
    elif text_lower.startswith("no") and "but" in text_lower:
        tabs = 2
    elif text_lower in ["no", "female"]:
        tabs = 2
    elif "$40,000" in option_text:
        tabs = 3
    elif "$60,000" in option_text:
        tabs = 4
    elif "$80,000" in option_text:
        tabs = 5
    else:
        tabs = 1  # Default: try first option
    
    # Tab to the option
    for _ in range(tabs):
        pyautogui.press("tab")
        time.sleep(0.2)
    
    # Space to select
    pyautogui.press("space")
    time.sleep(0.5)
    return True


def click_continue():
    """Click the Continue button. Try multiple strategies."""
    # Strategy 1: Press Enter (works on many survey platforms)
    pyautogui.press("enter")
    time.sleep(2)
    
    # Strategy 2: If still on same page, try clicking the Continue area
    # Continue button is typically at bottom-right of the survey card
    # ego-lite window: (0, 34), web area starts at ~y=129
    # Continue button is usually around (900-1050, 400-900) depending on content
    # Let's try a few positions
    continue_positions = [
        (965, 500),  # Common position
        (965, 600),
        (965, 700),
        (965, 800),
        (965, 900),
    ]
    for x, y in continue_positions:
        # Check if we're still on the survey (not navigated away)
        # Just try clicking at these positions
        pyautogui.click(x, y)
        time.sleep(0.5)


def type_text(text: str):
    """Type text into a focused field."""
    pyautogui.typewrite(text, interval=0.05)
    time.sleep(0.3)


def perform_drag_drop(start: tuple, end: tuple):
    """Perform a foreground drag from start to end."""
    pyautogui.moveTo(start[0], start[1])
    time.sleep(0.2)
    pyautogui.mouseDown()
    time.sleep(0.3)
    # Move in steps for smooth drag
    steps = 15
    for i in range(1, steps + 1):
        x = int(start[0] + (end[0] - start[0]) * i / steps)
        y = int(start[1] + (end[1] - start[1]) * i / steps)
        pyautogui.moveTo(x, y)
        time.sleep(0.03)
    time.sleep(0.3)
    pyautogui.mouseUp()
    time.sleep(0.5)


def execute_action(action: dict) -> bool:
    """Execute the action returned by vision analysis."""
    act = action.get("action", "unknown")
    answer = action.get("answer", "")
    target = action.get("target", "")
    coords = action.get("coords")
    
    print(f"  Action: {act} | Answer: {answer} | Target: {target}")
    
    if act == "click_radio":
        return click_radio_by_text(answer)
    
    elif act == "click_checkbox":
        # Similar to radio but may need multiple selections
        return click_radio_by_text(answer)
    
    elif act == "type_text":
        # Click the text field area first, then type
        # Text fields are usually in the middle of the page
        pyautogui.click(400, 350)  # Common text field position
        time.sleep(0.3)
        type_text(answer)
        return True
    
    elif act == "select_dropdown":
        # For dropdowns, try pressing the first letter
        if answer:
            pyautogui.press(answer[0].lower())
            time.sleep(0.5)
        return True
    
    elif act == "drag_drop":
        if coords and len(coords) >= 2:
            start = tuple(coords[0])
            end = tuple(coords[1])
            perform_drag_drop(start, end)
            return True
        return False
    
    elif act == "click_button":
        if "continue" in target.lower() or "next" in target.lower():
            click_continue()
            return True
        elif "begin" in target.lower():
            # Click BEGIN SURVEY button area
            pyautogui.click(260, 280)
            time.sleep(3)
            return True
        return False
    
    elif act in ("completed", "disqualified"):
        return True
    
    elif act == "unknown":
        # Try clicking Continue as default action
        click_continue()
        return True
    
    return False


# ── Main Loop ──────────────────────────────────────────────────────────

def run_survey(url: str) -> str:
    """Run through a single survey. Returns 'completed', 'disqualified', or 'stuck'."""
    print(f"\n{'='*60}")
    print(f"Starting survey: {url}")
    print(f"{'='*60}")
    
    # Navigate to survey
    navigate_ego_lite(url)
    time.sleep(5)
    
    # Take initial screenshot and analyze
    for step in range(200):  # Max 200 steps per survey
        # Capture screenshot
        img_path = capture_ego_lite()
        
        # Analyze with vision AI
        action = analyze_screenshot(img_path)
        act = action.get("action", "unknown")
        reasoning = action.get("reasoning", "")
        
        print(f"Step {step+1}: {act} — {reasoning[:80]}")
        
        # Check for completion/disqualification
        if act == "completed":
            print("SURVEY COMPLETED!")
            return "completed"
        if act == "disqualified":
            print("DISQUALIFIED")
            return "disqualified"
        
        # Execute action
        success = execute_action(action)
        if not success:
            print(f"  Action failed, trying Continue as fallback")
            click_continue()
        
        # Wait for page to update
        time.sleep(3)
        
        # Check if URL changed (survey might have redirected)
        current_url = get_current_url()
        if "dashboard" in current_url.lower():
            print("Redirected to dashboard — survey ended")
            return "ended"
    
    print("Max steps reached")
    return "stuck"


def get_survey_urls_from_ego_lite() -> list:
    """Get survey URLs from ego-lite dashboard."""
    navigate_ego_lite("https://surveys.gobranded.com/users/dashboard")
    time.sleep(10)
    
    # Use osascript to get links (limited, but worth trying)
    # Since osascript JS doesn't work in ego-lite, we'll use a fixed set
    # or rely on the user to provide URLs
    return []


def main():
    """Main entry point."""
    print("=" * 60)
    print("AUTONOMOUS SURVEY AUTOMATION")
    print("screencapture + Cerebras vision + PyAutoGUI")
    print("=" * 60)
    
    if not CEREBRAS_API_KEY:
        print("ERROR: CEREBRAS_API_KEY not set")
        sys.exit(1)
    
    # Check if ego-lite is running
    result = subprocess.run(["pgrep", "-f", "ego lite"], capture_output=True)
    if result.returncode != 0:
        print("ERROR: ego-lite is not running. Start it first.")
        sys.exit(1)
    
    print(f"ego-lite window: {EGO_WINDOW}")
    print(f"Vision model: {CEREBRAS_MODEL}")
    print()
    
    # Get survey URLs from command line or discover them
    if len(sys.argv) > 1:
        urls = sys.argv[1:]
    else:
        print("Usage: python survey_autonomous.py <survey_url1> [survey_url2] ...")
        print("Or run with no args to discover from dashboard (not yet implemented)")
        sys.exit(1)
    
    results = []
    for url in urls:
        result = run_survey(url)
        results.append(result)
        print(f"Result: {result}")
        time.sleep(5)
    
    print(f"\n{'='*60}")
    print(f"RESULTS: {results}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
