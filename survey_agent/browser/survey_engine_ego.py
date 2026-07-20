"""Survey completion engine for ego-lite backend.

Uses ego-browser's snapshotText() to read page content,
LLM to generate answers, and click('@N') to interact.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def _build_snapshot_and_interact_script(task_name: str = "survey answering") -> str:
    """Build a script that takes a snapshot and returns structured data."""
    return f"""
const task = await useOrCreateTaskSpace('{task_name}');
await waitForLoad();
await new Promise(r => setTimeout(r, 2000));
const pageSnap = await snapshotText();
const pageUrl = (await currentTab()).url;
cliLog(JSON.stringify({{ url: pageUrl, snapshot: pageSnap }}));
"""


def _find_interactive_elements(snapshot: str) -> list[dict]:
    """Parse ego-browser snapshot to find interactive elements with their text."""
    elements = []
    lines = snapshot.split("\n")

    for line in lines:
        ref_match = re.search(r'\[ref=(\d+)', line)
        if not ref_match:
            continue

        ref = ref_match.group(1)
        # Extract text after the ref annotations
        text_part = re.sub(r'\[ref=\d+[^\]]*\]\s*', '', line).strip()
        # Extract URL if present
        url_match = re.search(r'url=([^\]]+)', line)
        url = url_match.group(1) if url_match else ""
        # Extract locator
        loc_match = re.search(r'loc=([^\],]+)', line)
        loc = loc_match.group(1) if loc_match else ""

        elements.append({
            "ref": ref,
            "text": text_part[:200] if text_part else "",
            "url": url,
            "loc": loc,
            "raw": line.strip()[:200],
        })

    return elements


def _find_survey_links(elements: list[dict]) -> list[dict]:
    """Find survey links from interactive elements."""
    surveys = []
    for el in elements:
        url = el.get("url", "")
        text = el.get("text", "").lower()
        if "/surveys/pre/" in url or "take survey" in text:
            surveys.append(el)
    return surveys


def _find_question_and_options(elements: list[dict], snapshot: str) -> tuple[str, list[dict]]:
    """Extract question text and answer options from snapshot elements."""
    # Look for question text in non-interactive lines
    lines = snapshot.split("\n")
    question_text = ""

    for line in lines:
        # Skip lines with refs (interactive elements)
        if re.search(r'\[ref=\d+', line):
            continue
        text = line.strip()
        # Question text is typically longer and contains question marks or keywords
        if len(text) > 15 and ('?' in text or 'select' in text.lower() or 'choose' in text.lower() or 'how' in text.lower()):
            question_text = text[:300]
            break
        # Also capture descriptive text
        if len(text) > 20 and not question_text:
            question_text = text[:300]

    # Find radio/checkbox options (elements with short text, near each other)
    options = []
    for el in elements:
        text = el.get("text", "").strip()
        ref = el.get("ref", "")
        if text and len(text) < 100 and len(text) > 1:
            # Filter out navigation elements
            if any(skip in text.lower() for skip in ["dashboard", "profile", "logout", "menu", "notification"]):
                continue
            options.append({"ref": ref, "text": text})

    return question_text, options
