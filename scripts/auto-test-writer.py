#!/usr/bin/env python3
"""Auto-test writer — generates tests for uncovered code using free LLM APIs.

Provider chain (tries in order until one succeeds):
  1. Gemini    (GEMINI_API_KEY / GOOGLE_API_KEY) — 15 RPM, 1M tok/day
  2. Groq      (GROQ_API_KEY)                    — 30 RPM, 14.4K tok/day
  3. Mistral   (MISTRAL_API_KEY)                 — 2 RPS, generous free tier
  4. Cerebras  (CEREBRAS_API_KEY)                — 30 RPM, 1M tok/day
  5. OpenRouter (OPENROUTER_API_KEY)             — pay-per-use, free models

All use the OpenAI-compatible chat completions API via httpx (no extra deps).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
import xml.etree.ElementTree as ET
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
try:
    import httpx

    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

COVERAGE_XML = PROJECT_ROOT / "coverage.xml"
TESTS_DIR = PROJECT_ROOT / "tests"
PACKAGE_DIR = PROJECT_ROOT / "survey_agent"

MAX_FILES_PER_RUN = 5
CALL_DELAY_SECONDS = 2  # delay between API calls to avoid rate limits
PROVIDERS = [
    {
        "name": "Groq",
        "env_key": ["GROQ_API_KEY"],
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
    },
    {
        "name": "Cerebras",
        "env_key": ["CEREBRAS_API_KEY"],
        "base_url": "https://api.cerebras.ai/v1",
        "model": "gpt-oss-120b",
    },
    {
        "name": "Mistral",
        "env_key": ["MISTRAL_API_KEY"],
        "base_url": "https://api.mistral.ai/v1",
        "model": "mistral-large-latest",
    },
    {
        "name": "Gemini",
        "env_key": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.5-flash",
    },
    {
        "name": "OpenRouter",
        "env_key": ["OPENROUTER_API_KEY"],
        "base_url": "https://openrouter.ai/api/v1",
        "model": "meta-llama/llama-3.3-70b-instruct:free",
    },
]

def find_provider() -> tuple[str, str, str, str] | None:
    """Return (name, base_url, api_key, model) for the first available provider."""
    if not HAS_HTTPX:
        return None
    for p in PROVIDERS:
        for env in p["env_key"]:
            key = os.environ.get(env, "")
            if key:
                return p["name"], p["base_url"], key, p["model"]
    return None


def llm_generate(prompt: str, provider: tuple[str, str, str, str], retries: int = 2) -> str | None:
    """Call an OpenAI-compatible chat completions endpoint with retry on 429."""
    name, base_url, api_key, model = provider
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4096,
        "temperature": 0.2,
    }

    for attempt in range(retries + 1):
        try:
            with httpx.Client(timeout=90) as client:
                resp = client.post(url, headers=headers, json=payload)
                if resp.status_code == 429:
                    if attempt < retries:
                        wait = 2 ** (attempt + 1)
                        print(f"  ⏳ {name} rate limited, retry in {wait}s...", file=sys.stderr)
                        time.sleep(wait)
                        continue
                    print(f"  ⏳ {name} rate limited after {retries} retries", file=sys.stderr)
                    return None
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"  ⚠️  {name} error: {e}", file=sys.stderr)
            return None
    return None


def _resolve_source_path(filename: str) -> Path | None:
    """Resolve a coverage XML filename to an actual source file path."""
    for candidate in [PROJECT_ROOT / filename, PACKAGE_DIR / filename]:
        if candidate.exists():
            return candidate
    return None


def parse_coverage_gaps() -> list[dict]:
    """Parse coverage.xml and return files below threshold with uncovered lines."""
    if not COVERAGE_XML.exists():
        return []

    tree = ET.parse(COVERAGE_XML)
    root = tree.getroot()

    gaps = []
    for _pkg in root.iter("package"):
        for cls in _pkg.iter("class"):
            cls_rate = float(cls.attrib.get("line-rate", 0))
            filename = cls.attrib.get("filename", "")

            if cls_rate >= 0.8 or filename.endswith("__init__.py"):
                continue

            uncovered_lines: list[int] = []
            total_lines = 0
            for line in cls.iter("line"):
                total_lines += 1
                if int(line.attrib.get("hits", 0)) == 0:
                    uncovered_lines.append(int(line.attrib.get("number", 0)))

            if not uncovered_lines:
                continue

            source_path = _resolve_source_path(filename)
            if source_path:
                gaps.append(
                    {
                        "file": filename,
                        "source_path": str(source_path),
                        "pct": round(cls_rate * 100, 1),
                        "uncovered_lines": uncovered_lines,
                        "total_lines": total_lines,
                    }
                )

    return sorted(gaps, key=lambda x: x["pct"])


def _module_import_path(gap: dict) -> str:
    """Derive the Python import path for a gap's source file."""
    source_path = Path(gap["source_path"])
    rel = source_path.relative_to(PROJECT_ROOT)
    return ".".join(rel.with_suffix("").parts)


def read_source_context(gap: dict) -> str:
    """Read source file content with uncovered lines highlighted."""
    source_path = Path(gap["source_path"])
    try:
        lines = source_path.read_text().splitlines()
        annotated = []
        for i, line in enumerate(lines, 1):
            marker = ">>> " if i in gap["uncovered_lines"] else "    "
            annotated.append(f"{marker}{i:4d}: {line}")
        return "\n".join(annotated)
    except Exception:
        return ""


def generate_test_with_llm(gap: dict, provider: tuple[str, str, str, str]) -> str | None:
    """Generate test code using an LLM provider."""
    source_content = read_source_context(gap)
    module_import = _module_import_path(gap)

    prompt = f"""Generate pytest tests for this Python module to maximize line coverage.

## Source: {gap["file"]} ({gap["pct"]}% covered, {len(gap["uncovered_lines"])} lines uncovered)
## Uncovered lines: {gap["uncovered_lines"][:40]}{"..." if len(gap["uncovered_lines"]) > 40 else ""}

```python
{source_content}
```

## Rules
- Write ONLY valid Python test code, no markdown fences or explanation
- Use `from __future__ import annotations` at top
- Import the module at top level: `from {module_import} import ClassName, func_name`
- Mock external deps (keyring, httpx, playwright, browser_use, langchain) with unittest.mock
- Test uncovered lines marked with >>> — both happy path and error cases
- Use pytest fixtures from conftest.py where appropriate
- Every test must be runnable, no placeholders
- Keep tests focused — one behavior per test function
- Use `async def test_...` for async functions (asyncio_mode=auto)"""

    result = llm_generate(prompt, provider)
    if not result:
        return None

    # Strip markdown fences if present
    code = result
    if code.startswith("```"):
        lines = code.split("\n")
        start = 1
        end = len(lines)
        for i in range(len(lines) - 1, 0, -1):
            if lines[i].strip().startswith("```"):
                end = i
                break
        code = "\n".join(lines[start:end])
    return code.strip()


def generate_test_template(gap: dict) -> str:
    """Generate a basic test template when no LLM is available."""
    module_import = _module_import_path(gap)
    module_name = Path(gap["file"]).stem

    return textwrap.dedent(f'''\
        """Tests for {gap["file"]} — auto-generated skeleton.

        Current coverage: {gap["pct"]}%
        Uncovered lines: {len(gap["uncovered_lines"])}
        """
        from __future__ import annotations

        import pytest


        class Test{module_name.title().replace("_", "")}Coverage:
            """Coverage gap tests for {module_name}."""

            def test_module_imports(self):
                """Verify module imports without error."""
                import {module_import}
                assert {module_import} is not None
    ''')


def write_test_file(gap: dict, test_code: str) -> Path:
    """Write generated test code to the appropriate test directory."""
    source = Path(gap["file"])
    test_name = f"test_{source.stem}.py"

    parts = source.parts
    test_dir = TESTS_DIR / Path(*parts[:-1]) if len(parts) > 1 else TESTS_DIR

    test_dir.mkdir(parents=True, exist_ok=True)
    test_path = test_dir / test_name
    test_path.write_text(test_code + "\n")
    return test_path


def main() -> int:
    try:
        coverage_data = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        print("❌ Failed to parse coverage data from stdin", file=sys.stderr)
        return 1

    if coverage_data.get("ok"):
        print("✅ Coverage already meets threshold")
        return 0

    gaps = parse_coverage_gaps()
    if not gaps:
        print("⚠️  No coverage gaps found (but coverage is below threshold)")
        return 1

    priority_gaps = gaps[:MAX_FILES_PER_RUN]

    print(f"\n📝 Auto-test writer: {len(gaps)} file(s) below threshold")
    print(f"   Target: {coverage_data['threshold']}% | Current: {coverage_data['total_pct']}%")
    print(f"   Prioritizing top {len(priority_gaps)} lowest-coverage files\n")

    provider = find_provider()

    if provider:
        print(f"   Using {provider[0]} ({provider[3]})\n")
    else:
        print("   ⚠️  No API keys found — using template fallback")
        print("   Set one of: GEMINI_API_KEY, GROQ_API_KEY, MISTRAL_API_KEY,")
        print("   CEREBRAS_API_KEY, OPENROUTER_API_KEY\n")

    written = 0
    for i, gap in enumerate(priority_gaps):
        print(f"   📄 {gap['file']} ({gap['pct']}% — {len(gap['uncovered_lines'])} lines uncovered)")

        if provider:
            if i > 0:
                time.sleep(CALL_DELAY_SECONDS)
            test_code = generate_test_with_llm(gap, provider)
            if not test_code:
                test_code = generate_test_template(gap)
        else:
            test_code = generate_test_template(gap)

        test_path = write_test_file(gap, test_code)
        print(f"      → {test_path.relative_to(PROJECT_ROOT)}")
        written += 1

    print(f"\n   ✍️  Wrote {written} test file(s)")

    # Auto-fix lint/format issues in generated tests
    test_files = [str(p) for p in TESTS_DIR.rglob("test_*.py")]
    if test_files:
        print("   🔧 Auto-formatting generated tests...")
        subprocess.run(
            ["pdm", "run", "ruff", "format", *test_files],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=30,
            check=False,
        )
        subprocess.run(
            ["pdm", "run", "ruff", "check", "--fix", *test_files],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=30,
            check=False,
        )

    print("   🔍 Verifying test collection...")
    result = subprocess.run(
        ["pdm", "run", "pytest", "--co", "-q", "--no-header"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        print(f"   ⚠️  Some tests have collection errors:\n{result.stderr[:500]}")
    else:
        print("   ✅ All generated tests collect successfully")

    return 0


if __name__ == "__main__":
    sys.exit(main())
