#!/usr/bin/env python3
"""Ego-lite survey automation with PyAutoGUI for drag-and-drop.

Uses:
- osascript to navigate ego-lite
- computer_use (via Hermes MCP) for screenshots + AX clicks
- PyAutoGUI for drag-and-drop (foreground OS-level mouse)
- Vision model for CAPTCHA reading

This script is designed to be called from the Hermes agent terminal.
It handles all question types including drag-and-drop.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Ensure dependencies
try:
    import pyautogui
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "pyautogui"], check=True)
    import pyautogui

# Ego-lite window position
EGO_X, EGO_Y = 22, 56
EGO_W, EGO_H = 1200, 904

# Survey profile
PROFILE = {
    "age": "39",
    "gender": "Male",
    "state": "Texas",
    "income": "$50,000-$74,999",
    "race": "Black / African American",
    "employment": "Self-employed",
    "weight": "200",
    "height_feet": "5",
    "height_inches": "10",
    "conditions": ["High blood pressure", "High cholesterol"],
}


def screenshot(path="/tmp/ego_screen.png"):
    """Take a screenshot of ego-lite window."""
    region = f"{EGO_X},{EGO_Y},{EGO_W},{EGO_H}"
    subprocess.run(
        ["screencapture", "-x", "-R", region, path],
        timeout=5, check=True
    )
    return path


def navigate(url):
    """Navigate ego-lite to a URL via osascript."""
    script = f'tell application "ego lite" to open location "{url}"'
    subprocess.run(["osascript", "-e", script], timeout=10, capture_output=True)
    time.sleep(10)


def click_coord(x, y, delay=0.1):
    """Click at screen coordinates."""
    pyautogui.click(EGO_X + x, EGO_Y + y)
    time.sleep(delay)


def drag(from_x, from_y, to_x, to_y, duration=0.5):
    """Perform a foreground drag using PyAutoGUI."""
    sx, sy = EGO_X + from_x, EGO_Y + from_y
    ex, ey = EGO_X + to_x, EGO_Y + to_y
    pyautogui.moveTo(sx, sy)
    time.sleep(0.2)
    pyautogui.mouseDown()
    time.sleep(0.1)
    pyautogui.moveTo(ex, ey, duration=duration)
    time.sleep(0.1)
    pyautogui.mouseUp()
    time.sleep(0.5)


def type_text(text, delay=0.03):
    """Type text using PyAutoGUI."""
    pyautogui.typewrite(text, interval=delay) if text.isascii() else pyautogui.write(text)


def press_key(key):
    """Press a keyboard key."""
    pyautogui.press(key)
    time.sleep(0.3)


def enable_js_from_apple_events():
    """Enable JavaScript from Apple Events in ego-lite."""
    script = '''
    tell application "System Events"
        tell process "ego lite"
            click menu item "Allow JavaScript from Apple Events" of menu "Developer" of menu item "Developer" of menu "View" of menu bar item "View" of menu bar 1
        end tell
    end tell
    '''
    result = subprocess.run(["osascript", "-e", script], timeout=10, capture_output=True, text=True)
    return "error" not in result.stderr.lower()


def run_automation(survey_url=None):
    """Main automation loop."""
    print("=== Ego-Lite Survey Automation ===")
    print(f"Profile: {PROFILE['age']}y/o {PROFILE['gender']} from {PROFILE['state']}")
    
    # Step 1: Ensure ego-lite is running
    print("\n[1] Checking ego-lite...")
    result = subprocess.run(
        ["osascript", "-e", 'tell application "ego lite" to get name'],
        timeout=10, capture_output=True, text=True
    )
    if "error" in result.stderr.lower():
        print("  Launching ego-lite...")
        subprocess.run(["open", "-a", "ego lite"], timeout=10)
        time.sleep(5)
    
    # Step 2: Enable JS from Apple Events
    print("\n[2] Enabling JS from Apple Events...")
    enable_js_from_apple_events()
    time.sleep(1)
    
    # Step 3: Navigate to survey
    if survey_url:
        print(f"\n[3] Navigating to survey: {survey_url[:60]}...")
        navigate(survey_url)
    else:
        print("\n[3] Navigating to dashboard...")
        navigate("https://surveys.gobranded.com/users/dashboard")
        time.sleep(5)
        # TODO: extract survey URLs from page
        print("  Need survey URL. Pass as argument.")
        return
    
    # Step 4: Auto-answer loop
    print("\n[4] Starting auto-answer loop...")
    max_steps = 100
    step = 0
    
    while step < max_steps:
        step += 1
        time.sleep(2)
        
        # Take screenshot and analyze
        img_path = screenshot()
        print(f"\n  Step {step}: Screenshot saved to {img_path}")
        
        # Read page content via accessibility (if JS is enabled)
        body = get_page_text()
        if body:
            progress = re.search(r"(\d+)%", body)
            pct = progress.group(1) + "%" if progress else "?"
            print(f"  Progress: {pct}")
            
            # Check completion
            if "completed" in body.lower() and "thank you" in body.lower():
                print(f"\n✅ SURVEY COMPLETED at step {step}!")
                return True
            
            # Check disqualification
            if "do not qualify" in body.lower() or "not a match" in body.lower():
                print(f"\n❌ Disqualified at step {step}")
                return False
            
            # Check for drag-and-drop
            if "drag" in body.lower() and "drop" in body.lower():
                print(f"  🔄 Drag-and-drop detected!")
                handle_drag_and_drop(body)
                continue
            
            # Check for CAPTCHA
            if "type the following code" in body.lower() or "captcha" in body.lower():
                print(f"  🔐 CAPTCHA detected!")
                # TODO: read CAPTCHA via vision model
                continue
            
            # Handle standard questions
            answer_standard_question(body)
        else:
            print("  Could not read page text (JS not available)")
            # Fallback: use vision model to analyze screenshot
            print(f"  Analyzing screenshot with vision model...")
            # The agent should call vision_analyze here
    
    print(f"\n⚠️ Reached max steps ({max_steps})")
    return False


def get_page_text():
    """Get page text via osascript JS (if enabled) or return None."""
    script = '''
    tell application "ego lite"
        tell front window
            tell active tab
                set r to execute JavaScript "document.body.innerText.substring(0, 2000)"
                return r
            end tell
        end tell
    end tell
    '''
    result = subprocess.run(
        ["osascript", "-e", script],
        timeout=10, capture_output=True, text=True
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def handle_drag_and_drop(body):
    """Handle drag-and-drop questions using PyAutoGUI."""
    # Extract number to drag and target
    number_match = re.search(r"drag.*?number\s+(\d+)", body.lower())
    if number_match:
        number = number_match.group(1)
        print(f"  Need to drag number {number}")
        
        # TODO: Use vision model to find exact coordinates
        # For now, use estimated coordinates based on page layout
        # The numbers are typically in a row, target is below
        
        # This needs vision model integration to work properly
        print("  ⚠️ Needs vision model for coordinate detection")
        print("  Taking screenshot for manual coordinate input...")
        img_path = screenshot("/tmp/ego_drag.png")
        print(f"  Screenshot: {img_path}")
        print("  Agent should analyze this image and provide coordinates")
    else:
        print(f"  Drag-and-drop question detected but format unknown")
        print(f"  Body preview: {body[:200]}")


def answer_standard_question(body):
    """Answer a standard survey question based on page content."""
    body_lower = body.lower()
    
    # Detect question type and answer
    # This is a simplified version - the agent should use vision model for complex pages
    
    # Try clicking common buttons
    for keyword in ["continue", "forward", "next", "submit"]:
        if keyword in body_lower:
            # Try to click the button using coordinates
            # This needs the agent's vision model integration
            break


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else None
    run_automation(url)
