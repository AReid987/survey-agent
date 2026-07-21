"""Click TAKE SURVEY via cua-driver — simpler JS."""

import json
import subprocess
import time


def cua_page(pid, wid, js):
    payload = json.dumps(
        {
            "action": "execute_javascript",
            "pid": pid,
            "window_id": wid,
            "bundle_id": "com.google.Chrome",
            "javascript": js,
        }
    )
    r = subprocess.run(["cua-driver", "call", "page", payload], capture_output=True, text=True, timeout=15, check=False)
    return r.stdout.strip()


PID = 26306
WID = 17370

# Test simple JS
print(f"Title: {cua_page(PID, WID, 'document.title')}")
print(f"URL: {cua_page(PID, WID, 'document.location.href')}")

# Find TAKE SURVEY using querySelectorAll with Array.from
result = cua_page(
    PID,
    WID,
    "Array.from(document.querySelectorAll('a,button,div,span')).filter(el => el.textContent.trim() === 'TAKE SURVEY').map(el => el.tagName + '.' + el.className.substring(0,30)).join(', ')",
)
print(f"Elements: {result}")

# Click using querySelector
result2 = cua_page(
    PID,
    WID,
    "Array.from(document.querySelectorAll('a,button,div,span')).filter(el => el.textContent.trim() === 'TAKE SURVEY')[0]?.click() || 'no_click'",
)
print(f"Click: {result2}")

time.sleep(5)
print(f"Title after: {cua_page(PID, WID, 'document.title')}")
