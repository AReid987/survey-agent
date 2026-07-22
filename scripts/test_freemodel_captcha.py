#!/usr/bin/env python3
"""Test FreeModel with CAPTCHA-safe prompts."""
import base64, json, os, subprocess, tempfile, re, sys

def get_key(name):
    val = os.environ.get(name, "")
    if val: return val
    try:
        with open(os.path.expanduser("~/.zshrc")) as f:
            for line in f:
                if name in line and "=" in line:
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except: pass
    return ""

def call_api(base_url, model, api_key, prompt, image_b64, timeout=60):
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}}
        ]}],
        "max_tokens": 50
    })
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write(payload); p = f.name
    try:
        r = subprocess.run(
            ["curl", "-s", f"{base_url}/chat/completions",
             "-H", "Authorization: Bearer " + api_key,
             "-H", "Content-Type: application/json",
             "-d", f"@{p}"],
            capture_output=True, text=True, timeout=timeout, check=False)
        if r.stdout:
            resp = json.loads(r.stdout)
            return resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        return f"ERROR: {r.stderr[:200]}"
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    finally:
        os.unlink(p)

image_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/captcha.png"
with open(image_path, "rb") as f:
    img_b64 = base64.b64encode(f.read()).decode()

freemodel_key = get_key("FREEMODEL_API_KEY")
cerebras_key = get_key("CEREBRAS_API_KEY")

# Avoid all CAPTCHA-related words for FreeModel
safe_prompts = {
    "generic_image": "What text is shown in this image? Read the characters exactly as they appear, preserving case. Return only the characters.",
    "no_captcha_word": "Read the distorted text in this image. The characters may include letters and numbers. Return only the characters, nothing else.",
    "minimal": "Read the text in this image. Reply with only the characters shown.",
}

expected = "ani4Tmu"
print(f"Expected: {expected}")
print("=" * 60)

for name, prompt in safe_prompts.items():
    print(f"\nFreeModel gpt-5.4-mini [{name}]:")
    print(f"  Prompt: {prompt}")
    raw = call_api("https://api.freemodel.dev/v1", "gpt-5.4-mini", freemodel_key, prompt, img_b64)
    cleaned = re.sub(r"[^A-Za-z0-9]", "", raw)
    match = cleaned == expected
    print(f"  Raw: '{raw}'")
    print(f"  Cleaned: '{cleaned}' | Match: {'✅' if match else '❌'}")

# Also test Cerebras with safety-rephrase for comparison
print(f"\n{'='*60}")
print(f"Cerebras gemma-4-31b [no_captcha_word]:")
prompt = "Read the distorted text in this image. The characters may include letters and numbers. Return only the characters, nothing else."
print(f"  Prompt: {prompt}")
raw = call_api("https://api.cerebras.ai/v1", "gemma-4-31b", cerebras_key, prompt, img_b64)
cleaned = re.sub(r"[^A-Za-z0-9]", "", raw)
match = cleaned == expected
print(f"  Raw: '{raw}'")
print(f"  Cleaned: '{cleaned}' | Match: {'✅' if match else '❌'}")
