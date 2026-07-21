#!/usr/bin/env python3
"""Full survey automation using ego-lite + PyAutoGUI.

Uses screencapture for vision, PyAutoGUI for all interactions (click, type, drag).
No cua-driver dependency — uses real OS-level mouse events.

Flow:
1. osascript to navigate ego-lite
2. screencapture to see the page
3. vision_analyze to determine what to do
4. PyAutoGUI to click/type/drag
5. Loop until survey complete
"""
import subprocess
import sys
import time
import re
import os

# ego-lite window position
WINDOW_X = 22
WINDOW_Y = 56
WINDOW_W = 1200
WINDOW_H = 904

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
    "marital": "Single",
    "children": "3",
    "education": "Bachelor's degree",
}


def screenshot():
    """Take a screenshot of the ego-lite window."""
    path = f"/tmp/ego_auto_{int(time.time())}.png"
    subprocess.run(
        ["screencapture", "-x", "-R", f"{WINDOW_X},{WINDOW_Y},{WINDOW_W},{WINDOW_H}", path],
        timeout=5,
        capture_output=True,
    )
    return path


def navigate(url):
    """Navigate ego-lite to a URL via osascript."""
    script = f'tell application "ego lite" to open location "{url}"'
    subprocess.run(["osascript", "-e", script], timeout=10, capture_output=True)
    time.sleep(8)


def click_at(x, y):
    """Click at screen coordinates using PyAutoogui."""
    import pyautogui
    pyautogui.click(WINDOW_X + x, WINDOW_Y + y)
    time.sleep(0.5)


def type_text(text, delay=30):
    """Type text using PyAutoGUI keyboard."""
    import pyautogui
    pyautogui.typewrite(text, interval=delay / 1000) if text.isascii() else pyautogui.write(text)


def press_key(key):
    """Press a key."""
    import pyautogui
    pyautogui.press(key)


def drag(src_x, src_y, dst_x, dst_y, duration=0.5):
    """Perform a real OS-level mouse drag."""
    import pyautogui
    sx = WINDOW_X + src_x
    sy = WINDOW_Y + src_y
    dx = WINDOW_X + dst_x
    dy = WINDOW_Y + dst_y
    pyautogui.moveTo(sx, sy)
    time.sleep(0.2)
    pyautogui.mouseDown()
    time.sleep(0.1)
    pyautogui.moveTo(dx, dy, duration=duration)
    time.sleep(0.1)
    pyautogui.mouseUp()
    time.sleep(0.5)


def main():
    """Main automation loop."""
    # Get survey URL from command line or use default
    if len(sys.argv) < 2:
        print("Usage: python ego_automate_full.py <survey_url>")
        print("  or:  python ego_automate_full.py dashboard  (to navigate to dashboard)")
        sys.exit(1)

    target = sys.argv[1]

    if target == "dashboard":
        navigate("https://surveys.gobranded.com/users/dashboard")
        print("Navigated to dashboard. Take a screenshot to see available surveys.")
        return

    # Navigate to survey
    print(f"Navigating to: {target}")
    navigate(target)

    # Main automation loop
    step = 0
    max_steps = 200

    while step < max_steps:
        step += 1
        time.sleep(2)

        # Take screenshot
        img_path = screenshot()

        # Print current state for debugging
        print(f"\n--- Step {step} ---")
        print(f"Screenshot: {img_path}")

        # The caller (Hermes agent) will use vision_analyze on the screenshot
        # and then call the appropriate action. This script is the execution layer.
        # For autonomous operation, we'd need a local vision model.

        # For now, output the screenshot path and wait for instructions
        print(f"ACTION_NEEDED: Analyze {img_path} and determine next action")
        break

    print(f"Completed {step} steps")


if __name__ == "__main__":
    main()
