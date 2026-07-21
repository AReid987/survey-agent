"""Interact with survey — single expressions only."""

import json
import subprocess


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

# Get all checkbox IDs and labels as a single expression
result = cua_page(
    PID,
    WID,
    "Array.from(document.querySelectorAll('input[type=checkbox]')).map(function(cb,i){var l=document.querySelector(\"label[for='\"+cb.id+\"']\");return i+':'+cb.id.substring(0,15)+':'+(l?l.textContent.trim().substring(0,25):'NL')}).join('|')",
)
print(f"Checkboxes: {result[:600]}")

# Click checkbox 17 by clicking its label
result2 = cua_page(
    PID,
    WID,
    "var cb=document.querySelectorAll('input[type=checkbox]')[17];var l=document.querySelector(\"label[for='\"+cb.id+\"']\");l?function(){l.click();return 'clicked:'+cb.id}():'no_label'",
)
print(f"Click 17: {result2}")
