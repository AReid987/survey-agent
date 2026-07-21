"""Read text from image using FreeModel vision — neutral prompt."""

import base64
import json
import os
import subprocess
import tempfile


def get_key():
    key = ""
    try:
        with open(os.path.expanduser("~/.zshrc")) as f:
            for line in f:
                if "FREEMODEL_API_KEY" in line and "=" in line:
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    except FileNotFoundError:
        pass
    return key


def read_image(
    image_path,
    prompt="Please read and transcribe all visible text characters in this image. Preserve exact casing. Output only the characters.",
):
    key = get_key()
    if not key:
        return "NO_KEY"

    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    payload = json.dumps(
        {
            "model": "claude-haiku-4-5-20251001",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                    ],
                }
            ],
            "max_tokens": 50,
        }
    )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write(payload)
        p = f.name

    r = subprocess.run(
        [
            "curl",
            "-s",
            "https://api.freemodel.dev/v1/chat/completions",
            "-H",
            "Authorization: Bearer " + key,
            "-H",
            "Content-Type: application/json",
            "-d",
            f"@{p}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    os.unlink(p)

    if r.stdout:
        try:
            resp = json.loads(r.stdout)
            return resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        except (json.JSONDecodeError, IndexError, KeyError):
            return "PARSE_ERROR"
    return "CURL_ERROR"


if __name__ == "__main__":
    import sys

    image = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ps_captcha_tight.png"
    prompt = (
        sys.argv[2]
        if len(sys.argv) > 2
        else "Please read and transcribe all visible text characters in this image. Preserve exact casing. Output only the characters."
    )
    print(f"Image: {image}")
    result = read_image(image, prompt)
    print(f"Text: '{result}'")
