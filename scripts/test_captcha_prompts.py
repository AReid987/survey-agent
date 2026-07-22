#!/usr/bin/env python3
"""Test CAPTCHA accuracy with different prompting strategies."""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time


def get_key(name):
    val = os.environ.get(name, "")
    if val:
        return val
    try:
        with open(os.path.expanduser("~/.zshrc")) as f:
            for line in f:
                if name in line and "=" in line:
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return ""


def call_api(base_url, model, api_key, prompt, image_b64, max_tokens=50):
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}}
        ]}],
        "max_tokens": max_tokens
    })
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write(payload)
        p = f.name
    try:
        r = subprocess.run(
            ["curl", "-s", f"{base_url}/chat/completions",
             "-H", "Authorization: Bearer " + api_key,
             "-H", "Content-Type: application/json",
             "-d", f"@{p}"],
            capture_output=True, text=True, timeout=30, check=False
        )
        if r.stdout:
            resp = json.loads(r.stdout)
            content = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            return content
        return f"ERROR: {r.stderr[:200]}"
    finally:
        os.unlink(p)


def clean_response(text):
    """Extract only alphanumeric characters from response."""
    import re
    return re.sub(r"[^A-Za-z0-9]", "", text)


def main():
    image_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/captcha.png"
    expected = sys.argv[2] if len(sys.argv) > 2 else "ani4Tmu"

    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    cerebras_key = get_key("CEREBRAS_API_KEY")
    freemodel_key = get_key("FREEMODEL_API_KEY")

    prompts = {
        "A_simple": "Read the CAPTCHA code. Reply only with characters.",
        "B_detailed": "This is a CAPTCHA image with distorted black text on white background with a horizontal line through the middle. Read each character carefully one at a time from left to right. Reply only with the characters.",
        "C_step_by_step": "Look at this CAPTCHA image. For each character position, identify the letter or number. The characters are uppercase letters and numbers. Reply only with the 5-7 characters shown.",
        "D_safety_rephrase": "What text is shown in this image? Read the characters exactly as they appear, preserving case. Return only the characters.",
    }

    print(f"CAPTCHA image: {image_path}")
    print(f"Expected text: {expected}")
    print(f"Image size: {os.path.getsize(image_path)} bytes")
    print("=" * 70)

    results = []

    # Test all 4 prompts with Cerebras gemma-4-31b
    print("\n🔵 CEREBRAS gemma-4-31b")
    print("-" * 50)
    for name, prompt in prompts.items():
        print(f"\n  Prompt [{name}]:")
        print(f"  \"{prompt}\"")
        time.sleep(1)  # Rate limit courtesy
        raw = call_api("https://api.cerebras.ai/v1", "gemma-4-31b", cerebras_key, prompt, img_b64)
        cleaned = clean_response(raw)
        match = cleaned == expected
        print(f"  Raw response: '{raw}'")
        print(f"  Cleaned:      '{cleaned}'")
        print(f"  Match: {'✅ YES' if match else '❌ NO'}")
        results.append(("Cerebras", name, raw, cleaned, match))

    # Test best prompt with FreeModel gpt-5.4-mini
    print("\n🟢 FREEMODEL gpt-5.4-mini")
    print("-" * 50)
    # Use the safety-rephrase prompt (historically best for Claude-based models)
    best_prompt_name = "D_safety_rephrase"
    best_prompt = prompts[best_prompt_name]
    print(f"\n  Prompt [{best_prompt_name}]:")
    print(f"  \"{best_prompt}\"")
    raw = call_api("https://api.freemodel.dev/v1", "gpt-5.4-mini", freemodel_key, best_prompt, img_b64)
    cleaned = clean_response(raw)
    match = cleaned == expected
    print(f"  Raw response: '{raw}'")
    print(f"  Cleaned:      '{cleaned}'")
    print(f"  Match: {'✅ YES' if match else '❌ NO'}")
    results.append(("FreeModel", best_prompt_name, raw, cleaned, match))

    # Also test simple prompt with FreeModel for comparison
    print(f"\n  Prompt [A_simple]:")
    print(f"  \"{prompts['A_simple']}\"")
    time.sleep(1)
    raw = call_api("https://api.freemodel.dev/v1", "gpt-5.4-mini", freemodel_key, prompts["A_simple"], img_b64)
    cleaned = clean_response(raw)
    match = cleaned == expected
    print(f"  Raw response: '{raw}'")
    print(f"  Cleaned:      '{cleaned}'")
    print(f"  Match: {'✅ YES' if match else '❌ NO'}")
    results.append(("FreeModel", "A_simple", raw, cleaned, match))

    # Summary
    print("\n" + "=" * 70)
    print("📊 SUMMARY")
    print("=" * 70)
    print(f"Expected: '{expected}'")
    print()
    print(f"{'Provider':<12} {'Prompt':<16} {'Raw Response':<20} {'Cleaned':<12} {'Match'}")
    print("-" * 70)
    for provider, prompt_name, raw, cleaned, match in results:
        raw_display = raw[:18] + ".." if len(raw) > 20 else raw
        print(f"{provider:<12} {prompt_name:<16} {raw_display:<20} {cleaned:<12} {'✅' if match else '❌'}")

    # Accuracy per provider
    print()
    for provider in ["Cerebras", "FreeModel"]:
        matches = sum(1 for p, _, _, _, m in results if p == provider and m)
        total = sum(1 for p, _, _, _, _ in results if p == provider)
        print(f"{provider}: {matches}/{total} correct")


if __name__ == "__main__":
    main()
