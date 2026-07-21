#!/usr/bin/env python3
"""CUA live test — drive Chrome through a real survey via cua-driver."""
import json
import subprocess
import sys
import time

PID = 657  # Chrome main process
WID = 56   # User's visible Chrome window


def cua(tool, args=None, timeout=30):
    cmd = ["cua-driver", "call", tool]
    if args:
        cmd.append(json.dumps(args))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    try:
        return json.loads(r.stdout.strip())
    except json.JSONDecodeError:
        return {"raw": r.stdout.strip()}


def get_elements(max_els=150):
    r = cua("get_window_state", {"pid": PID, "window_id": WID, "max_elements": max_els})
    return r.get("elements", [])


def get_texts(elements):
    return [e.get("label", "") for e in elements if e.get("role") == "AXStaticText" and e.get("label")]


def click(idx):
    r = cua("click", {"pid": PID, "window_id": WID, "element_index": idx})
    return "error" not in str(r).lower() or "Performed" in str(r)


def type_text(text):
    cua("type_text", {"pid": PID, "window_id": WID, "text": text})


def press_key(key):
    cua("press_key", {"pid": PID, "window_id": WID, "key": key})


def scroll_down(n=5):
    cua("scroll", {"pid": PID, "window_id": WID, "direction": "down", "amount": n})


def navigate(url):
    # Get fresh element indices
    els = get_elements(20)
    addr = [e for e in els if "address" in e.get("label", "").lower()]
    if addr:
        click(addr[0]["element_index"])
        time.sleep(0.3)
        press_key("cmd+a")
        time.sleep(0.1)
        type_text(url)
        time.sleep(0.2)
        press_key("return")
        time.sleep(6)
        return True
    return False


def find_element(elements, search_terms, role_filter=None):
    for el in elements:
        label = el.get("label", "").lower()
        role = el.get("role", "")
        if role_filter and role != role_filter:
            continue
        for term in search_terms:
            if term.lower() in label:
                return el
    return None


def main():
    print("=" * 60)
    print("CUA Live Test — Branded Surveys")
    print("=" * 60)

    # Step 1: Navigate to dashboard
    print("\n[1] Navigating to dashboard...")
    navigate("https://surveys.gobranded.com/users/dashboard")
    time.sleep(3)

    els = get_elements(200)
    texts = get_texts(els)
    page_text = " ".join(texts).lower()
    title_el = [e for e in els if e.get("role") == "AXWindow"]
    title = title_el[0].get("label", "") if title_el else ""
    print(f"  Page: {title}")
    print(f"  Points: {[t for t in texts if 'point' in t.lower() or t.strip().isdigit()][:5]}")

    if "log in" in page_text or "sign in" in page_text:
        print("  ERROR: Not logged in!")
        sys.exit(1)

    # Step 2: Scroll to find surveys
    print("\n[2] Finding surveys...")
    scroll_down(8)
    time.sleep(1)
    els = get_elements(300)
    texts = get_texts(els)
    page_text = " ".join(texts).lower()

    # Look for survey links
    survey_el = find_element(els, ["take survey", "start survey"], "AXLink")
    if not survey_el:
        survey_el = find_element(els, ["take survey", "start survey"], "AXButton")
    if not survey_el:
        # Try finding any link with survey-related text
        for el in els:
            if el.get("role") in ("AXLink", "AXButton") and "survey" in el.get("label", "").lower():
                survey_el = el
                break

    if survey_el:
        print(f"  Found survey: [{survey_el['element_index']}] {survey_el['label'][:60]}")
        print(f"  Clicking...")
        click(survey_el["element_index"])
        time.sleep(8)

        # Step 3: Handle pre-survey page
        print("\n[3] Pre-survey page...")
        els = get_elements(200)
        texts = get_texts(els)
        page_text = " ".join(texts).lower()
        title_el = [e for e in els if e.get("role") == "AXWindow"]
        title = title_el[0].get("label", "") if title_el else ""
        print(f"  Page: {title}")

        # Click "Take Survey" or "Begin" if on pre-page
        begin_el = find_element(els, ["take survey", "begin survey", "start survey", "begin"], "AXLink")
        if not begin_el:
            begin_el = find_element(els, ["take survey", "begin survey", "start survey", "begin"], "AXButton")
        if begin_el:
            print(f"  Clicking: [{begin_el['element_index']}] {begin_el['label'][:40]}")
            click(begin_el["element_index"])
            time.sleep(6)

        # Step 4: Answer survey questions
        print("\n[4] Answering survey...")
        for page_num in range(30):
            time.sleep(3)
            els = get_elements(200)
            texts = get_texts(els)
            page_text = " ".join(texts).lower()
            title_el = [e for e in els if e.get("role") == "AXWindow"]
            title = title_el[0].get("label", "") if title_el else ""

            print(f"\n  Page {page_num + 1}: {title[:60]}")
            print(f"  Text: {page_text[:200]}")

            # Check completion
            if any(kw in page_text for kw in ["thank you", "survey complete", "congratulations", "you've earned"]):
                print("  >>> SURVEY COMPLETED! <<<")
                return {"status": "completed", "pages": page_num + 1}

            # Check disqualification
            if any(kw in page_text for kw in ["not a match", "disqualified", "screened out", "don't qualify"]):
                print("  >>> DISQUALIFIED <<<")
                return {"status": "disqualified", "pages": page_num + 1}

            # Check for CAPTCHA
            if any(kw in page_text for kw in ["captcha", "robot", "verification code"]):
                print("  >>> CAPTCHA DETECTED <<<")
                # Try to find and screenshot the CAPTCHA image
                cap_el = find_element(els, ["captcha", "robot", "verify"], "AXImage")
                if cap_el:
                    print(f"  CAPTCHA image at element {cap_el['element_index']}")
                return {"status": "captcha", "pages": page_num + 1}

            # Find radio buttons
            radios = [e for e in els if e.get("role") == "AXRadioButton"]
            checkboxes = [e for e in els if e.get("role") == "AXCheckBox"]
            text_fields = [e for e in els if e.get("role") in ("AXTextField", "AXTextArea")]
            buttons = [e for e in els if e.get("role") == "AXButton"]
            links = [e for e in els if e.get("role") == "AXLink"]

            answered = False

            # Click radio buttons based on profile
            if radios:
                for r in radios:
                    label = r.get("label", "").lower()
                    if any(kw in label for kw in ["male", "39", "35-44", "25-44", "bachelor", "full-time", "black", "hispanic"]):
                        print(f"  Clicking radio: {r['label'][:40]}")
                        click(r["element_index"])
                        answered = True
                        break
                if not answered and radios:
                    print(f"  Clicking first radio: {radios[0]['label'][:40]}")
                    click(radios[0]["element_index"])
                    answered = True

            # Click checkboxes
            if not answered and checkboxes:
                print(f"  Clicking checkbox: {checkboxes[0]['label'][:40]}")
                click(checkboxes[0]["element_index"])
                answered = True

            # Fill text fields
            if not answered and text_fields:
                print(f"  Filling text field: {text_fields[0]['label'][:40]}")
                click(text_fields[0]["element_index"])
                time.sleep(0.3)
                type_text("I enjoy sharing my opinions about products and services.")
                answered = True

            if answered:
                print(f"  Answered on page {page_num + 1}")

            # Click Next/Continue
            time.sleep(1)
            next_el = find_element(els, ["next", "continue", "submit", "proceed", "done"], "AXButton")
            if not next_el:
                next_el = find_element(els, ["next", "continue", "submit"], "AXLink")
            if next_el:
                print(f"  Clicking: {next_el['label'][:30]}")
                click(next_el["element_index"])
            else:
                print("  Pressing Enter (no next button)")
                press_key("return")
            time.sleep(2)

        return {"status": "max_pages", "pages": 30}
    else:
        print("  No survey links found on dashboard")
        # Show what we see
        for t in texts[:20]:
            if t.strip():
                print(f"  - {t[:80]}")
        return {"status": "no_surveys"}


if __name__ == "__main__":
    result = main()
    print(f"\nResult: {result}")
