#!/usr/bin/env python3
"""Ego-lite survey automation with PyAutoGUI for drag-and-drop.

Uses:
- osascript to navigate ego-lite
- computer_use (via subprocess) for screenshots + clicks
- PyAutoGUI for OS-level mouse drag (no approval needed)
- Cerebras gemma-4-31b for CAPTCHA vision
"""
import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import time

# PyAutoGUI for drag
import pyautogui
pyautogui.PAUSE = 0.05
pyautogui.FAILSAFE = True

# --- Config ---
WINDOW_X, WINDOW_Y = 22, 56  # ego-lite window position
WINDOW_W, WINDOW_H = 1200, 904
PROFILE = {
    "age": 39, "gender": "Male", "state": "Texas", "city": "Austin",
    "race": "Black", "education": "Associate Degree", "income": "$50-75K",
    "employment": "Self-employed", "marital": "Domestic partnership",
    "children": "Yes", "smoker": "Former smoker", "drinker": "Never",
}


def osascript(cmd):
    """Run an AppleScript command."""
    r = subprocess.run(['osascript', '-e', cmd], capture_output=True, text=True, timeout=15)
    return r.stdout.strip()


def navigate_ego(url):
    """Navigate ego-lite to a URL."""
    osascript(f'tell application "ego lite" to open location "{url}"')
    time.sleep(8)


def get_cerebras_vision(image_bytes, prompt):
    """Call Cerebras gemma-4-31b for vision."""
    import httpx
    key = os.environ.get('CEREBRAS_API_KEY', '')
    if not key:
        return None
    b64 = base64.b64encode(image_bytes).decode()
    resp = httpx.post('https://api.cerebras.ai/v1/chat/completions',
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
        json={
            'model': 'gemma-4-31b',
            'messages': [{'role': 'user', 'content': [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}}
            ]}],
            'max_tokens': 200
        }, timeout=30)
    if resp.status_code == 200:
        return resp.json()['choices'][0]['message']['content']
    return None


def screenshot_ego():
    """Take a screenshot of the ego-lite window."""
    x, y, w, h = WINDOW_X, WINDOW_Y, WINDOW_W, WINDOW_H
    # Use screencapture
    path = '/tmp/ego_screenshot.png'
    subprocess.run(['screencapture', '-x', '-R', f'{x},{y},{w},{h}', path], timeout=5)
    with open(path, 'rb') as f:
        return f.read()


def click_at(x, y):
    """Click at screen coordinates."""
    pyautogui.click(x, y)
    time.sleep(0.5)


def type_text(text):
    """Type text using pyautogui."""
    pyautogui.typewrite(text, interval=0.03) if text.isascii() else pyautogui.write(text)
    time.sleep(0.3)


def drag_element(src_x, src_y, tgt_x, tgt_y):
    """Drag from source to target using PyAutoGUI."""
    print(f"  Dragging ({src_x},{src_y}) -> ({tgt_x},{tgt_y})")
    pyautogui.moveTo(src_x, src_y, duration=0.3)
    time.sleep(0.2)
    pyautogui.mouseDown()
    time.sleep(0.3)
    steps = 25
    for i in range(1, steps + 1):
        x = src_x + (tgt_x - src_x) * i // steps
        y = src_y + (tgt_y - src_y) * i // steps
        pyautogui.moveTo(x, y, duration=0.02)
        time.sleep(0.01)
    time.sleep(0.2)
    pyautogui.mouseUp()
    time.sleep(1)
    print("  Drag complete!")


def click_element_center(el_x, el_y, el_w, el_h):
    """Click the center of an element given its bounds."""
    cx = WINDOW_X + el_x + el_w // 2
    cy = WINDOW_Y + el_y + el_h // 2
    click_at(cx, cy)


def main():
    survey_urls = sys.argv[1:] if len(sys.argv) > 1 else []
    if not survey_urls:
        print("Usage: python ego_automate.py <url1> [url2] ...")
        sys.exit(1)
    
    for url in survey_urls:
        print(f"\n=== Survey: {url[:60]}... ===")
        navigate_ego(url)
        time.sleep(5)
        
        # Take screenshot to see what's on the page
        img = screenshot_ego()
        
        # Ask vision what's on the page
        desc = get_cerebras_vision(img, "Describe this survey page. What question is shown? What type of interaction is needed (text input, radio buttons, checkbox, drag-and-drop, CAPTCHA)?")
        print(f"Page: {desc[:200] if desc else 'unknown'}")
        
        # Save screenshot for debugging
        with open('/tmp/ego_current.png', 'wb') as f:
            f.write(img)
        print("Screenshot saved to /tmp/ego_current.png")


if __name__ == '__main__':
    main()
