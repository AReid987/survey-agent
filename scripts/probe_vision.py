"""Probe which local vision runtimes exist and whether they can read a CAPTCHA.

Answers the {#escalate-runtime} question with evidence instead of assumption:
per the 2026-08-26 inventory this machine has NO multimodal model installed, so
before wiring vision into the escalation path we need to know what is actually
reachable and how well it reads the CAPTCHA images already captured in data/.

This script only *reports*. It installs nothing and changes no config.

    python scripts/probe_vision.py                    # capability report only
    python scripts/probe_vision.py --image data/ab-captcha.png
    python scripts/probe_vision.py --expect ABC123    # score the read

Runtimes probed:
  * LM Studio  — `lms` CLI + its OpenAI-compatible server on :1234
  * Ollama     — `ollama list` for locally-pulled vision models
  * llama.cpp  — `llama-server` binary presence
  * Tesseract  — pytesseract, the OCR baseline the harness already uses
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

LMSTUDIO_URL = "http://127.0.0.1:1234"
OLLAMA_URL = "http://127.0.0.1:11434"
PROBE_TIMEOUT = 20

# Substrings that mark a model as multimodal. Names vary by publisher, so this is
# a heuristic — a hit still has to survive the live read test below.
VISION_MARKERS = (
    "vl",  # qwen2-vl, qwen3-vl
    "vision",
    "llava",
    "moondream",
    "smolvlm",
    "gemma-3",  # gemma 3 ships vision variants
    "pixtral",
    "minicpm-v",
    "internvl",
)

CAPTCHA_PROMPT = (
    "Read the characters in this image exactly as shown. "
    "Preserve capitalisation. Reply with only the characters, nothing else."
)


@dataclass
class RuntimeReport:
    name: str
    installed: bool = False
    server_up: bool = False
    models: list[str] = field(default_factory=list)
    vision_models: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def usable_for_vision(self) -> bool:
        return self.installed and bool(self.vision_models)


def _run(cmd: list[str], timeout: int = PROBE_TIMEOUT) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return proc.returncode, proc.stdout, proc.stderr
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, "", str(exc)


def _looks_multimodal(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in VISION_MARKERS)


def _http_json(url: str, payload: dict | None = None) -> dict | None:
    """Minimal HTTP helper. httpx is already a project dependency."""
    try:
        import httpx
    except ImportError:
        return None
    try:
        if payload is None:
            resp = httpx.get(url, timeout=PROBE_TIMEOUT)
        else:
            resp = httpx.post(url, json=payload, timeout=PROBE_TIMEOUT * 3)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


def probe_lmstudio() -> RuntimeReport:
    report = RuntimeReport("LM Studio")
    if shutil.which("lms") is None:
        report.notes.append("lms CLI not on PATH")
        return report
    report.installed = True

    code, out, _ = _run(["lms", "ls"])
    if code == 0:
        for raw_line in out.splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith(("You have", "LLM", "EMBEDDING")):
                continue
            model = stripped.split()[0]
            if model:
                report.models.append(model)
    report.vision_models = [m for m in report.models if _looks_multimodal(m)]

    if (models := _http_json(f"{LMSTUDIO_URL}/v1/models")) is not None:
        report.server_up = True
        served = [m.get("id", "") for m in models.get("data", [])]
        report.notes.append(f"server up, serving: {', '.join(served) or 'nothing'}")
    else:
        report.notes.append(f"server not reachable at {LMSTUDIO_URL} (start with: lms server start)")

    if not report.vision_models:
        report.notes.append("no installed model name looks multimodal")
    return report


def probe_ollama() -> RuntimeReport:
    report = RuntimeReport("Ollama")
    if shutil.which("ollama") is None:
        report.notes.append("ollama not on PATH")
        return report
    report.installed = True

    code, out, _ = _run(["ollama", "list"])
    if code == 0:
        for line in out.splitlines()[1:]:
            if not line.strip():
                continue
            name = line.split()[0]
            report.models.append(name)
            if name.endswith(":cloud"):
                report.notes.append(f"{name} is cloud-hosted, not local inference")

    local = [m for m in report.models if not m.endswith(":cloud")]
    report.vision_models = [m for m in local if _looks_multimodal(m)]

    if _http_json(f"{OLLAMA_URL}/api/tags") is not None:
        report.server_up = True

    if not report.vision_models:
        report.notes.append("no local vision model pulled (try: ollama pull qwen3-vl:8b)")
    return report


def probe_llamacpp() -> RuntimeReport:
    report = RuntimeReport("llama.cpp")
    binary = shutil.which("llama-server")
    if binary is None:
        report.notes.append("llama-server not on PATH")
        return report
    report.installed = True
    report.notes.append(f"binary at {binary}; needs a GGUF + mmproj pair to serve vision")
    return report


def probe_tesseract() -> RuntimeReport:
    report = RuntimeReport("Tesseract (OCR baseline)")
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        report.notes.append("pytesseract not importable")
        return report
    if shutil.which("tesseract") is None:
        report.notes.append("pytesseract installed but tesseract binary missing")
        return report
    report.installed = True
    report.vision_models = ["tesseract"]  # it reads images, which is the bar here
    report.notes.append("already used as the harness OCR fallback")
    return report


def try_read_image(image: Path, model: str, expected: str = "") -> None:
    """Ask a served LM Studio model to read an image, and score it if we can."""
    if not image.exists():
        print(f"  image not found: {image}")
        return

    b64 = base64.b64encode(image.read_bytes()).decode("ascii")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": CAPTCHA_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }
        ],
        "max_tokens": 32,
        "temperature": 0,
    }
    result = _http_json(f"{LMSTUDIO_URL}/v1/chat/completions", payload)
    if result is None:
        print(f"  {model}: request failed (is the model loaded and vision-capable?)")
        return

    try:
        answer = result["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError):
        print(f"  {model}: unexpected response shape: {json.dumps(result)[:200]}")
        return

    print(f"  {model} read: {answer!r}")
    if expected:
        verdict = "EXACT MATCH" if answer == expected else "MISMATCH"
        print(f"    expected {expected!r} -> {verdict}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe local vision runtimes")
    parser.add_argument("--image", type=Path, help="CAPTCHA image to attempt a read on")
    parser.add_argument("--model", help="Model id to test (default: first vision-looking model)")
    parser.add_argument("--expect", default="", help="Ground-truth string for scoring")
    args = parser.parse_args()

    reports = [probe_lmstudio(), probe_ollama(), probe_llamacpp(), probe_tesseract()]

    print("=" * 68)
    print("LOCAL VISION RUNTIME PROBE")
    print("=" * 68)
    for r in reports:
        status = "OK" if r.usable_for_vision else ("installed" if r.installed else "absent")
        print(f"\n[{status:>9}] {r.name}")
        if r.models:
            print(f"            models: {', '.join(r.models)}")
        if r.vision_models:
            print(f"            vision-capable: {', '.join(r.vision_models)}")
        for note in r.notes:
            print(f"            - {note}")

    # The SDK view is authoritative for what survey_agent.vision will actually use.
    print("\n" + "-" * 68)
    try:
        from survey_agent import vision as vision_mod

        sdk_models = vision_mod.list_downloaded_vision_models()
        print(f"survey_agent.vision sees: {sdk_models or 'no vision model'}")
        print(f"vision_ready(): {vision_mod.vision_ready()}")
    except Exception as exc:
        print(f"survey_agent.vision unavailable: {exc}")

    vision_ready = [r for r in reports if r.usable_for_vision and r.name != "Tesseract (OCR baseline)"]
    print("-" * 68)
    if vision_ready:
        print(f"VERDICT: {len(vision_ready)} runtime(s) can serve a vision model.")
    else:
        print("VERDICT: NO multimodal model installed. OCR (tesseract) is the only")
        print("         image reader available. Install one with:")
        print("           lms get qwen/qwen3-vl-8b --mlx --yes")
    print("-" * 68)

    if args.image:
        print(f"\nAttempting read of {args.image} via survey_agent.vision:")
        try:
            from survey_agent import vision as vision_mod

            result = vision_mod.read_captcha(args.image)
        except Exception as exc:
            print(f"  read failed: {exc}")
            return 1

        if result is None:
            print("  read returned None (no model, unreadable, or error) — would escalate")
            return 1
        print(f"  read: {result!r}")
        if args.expect:
            verdict = "EXACT MATCH" if result == args.expect else "MISMATCH"
            print(f"    expected {args.expect!r} -> {verdict}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
