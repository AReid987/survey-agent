"""Auto-answer survey questions via cua-driver."""

import json
import subprocess
import sys
import time

sys.path.insert(0, "/Users/antonioreid/survey-agent")


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


def get_page_text():
    return cua_page(PID, WID, "document.body.innerText.substring(0, 1000)")


def answer_question():
    """Try to answer the current question and submit."""
    # Count inputs
    radios = cua_page(PID, WID, 'document.querySelectorAll("input[type=radio]").length')
    checks = cua_page(PID, WID, 'document.querySelectorAll("input[type=checkbox]").length')

    try:
        radio_count = int(radios) if radios.isdigit() else 0
    except (ValueError, AttributeError):
        radio_count = 0
    try:
        check_count = int(checks) if checks.isdigit() else 0
    except (ValueError, AttributeError):
        check_count = 0

    if radio_count > 0:
        # Select first radio button (usually "Same" or first option)
        result = cua_page(
            PID,
            WID,
            'var r=document.querySelectorAll("input[type=radio]");r[0].checked=true;r[0].dispatchEvent(new Event("change",{bubbles:true}));"radio:0"',
        )
        print(f"  Selected radio: {result}")
    elif check_count > 0:
        # Select first checkbox
        result = cua_page(
            PID,
            WID,
            'var c=document.querySelectorAll("input[type=checkbox]");c[0].checked=true;c[0].dispatchEvent(new Event("change",{bubbles:true}));"check:0"',
        )
        print(f"  Selected checkbox: {result}")
    else:
        print("  No inputs found")

    time.sleep(0.5)

    # Submit
    cua_page(PID, WID, 'document.querySelector("input[type=submit], button[type=submit], button").click()')
    time.sleep(2)


PID = 26306
WID = 17370

print("Starting survey auto-answer loop...")
for i in range(50):
    text = get_page_text()
    first_line = text.split("\n")[0][:100] if text else "empty"
    print(f"\n[Q{i + 1}] {first_line}")

    # Check for completion
    if "thank" in text.lower() or "complete" in text.lower() or "finished" in text.lower():
        print("SURVEY COMPLETED!")
        break

    if "not qualified" in text.lower() or "disqualified" in text.lower():
        print("DISQUALIFIED")
        break

    answer_question()

print("\nDone!")
