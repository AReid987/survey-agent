"""Research phase for the autoresearch pipeline.

When the brain identifies a recurring failure, this module:
1. Searches the web for known solutions
2. Analyzes the codebase for relevant code
3. Checks past learnings for similar patterns
4. Provides context to the digest LLM for better diagnosis
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def search_web(query: str, max_results: int = 3) -> list[dict[str, str]]:
    """Search the web for solutions to a failure pattern.

    Returns list of {title, url, snippet}.
    """
    try:
        # Use curl to search via a simple API
        # This can be replaced with a proper search API later
        result = subprocess.run(
            [
                "curl", "-s", "-L",
                f"https://html.duckduckgo.com/html/?q={query}",
                "-H", "User-Agent: Mozilla/5.0",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []

        # Simple HTML parsing for search results
        # This is a basic implementation — can be improved
        results = []
        lines = result.stdout.split("\n")
        for i, line in enumerate(lines):
            if "result__a" in line and len(results) < max_results:
                # Extract title and URL from the link
                title_start = line.find(">") + 1
                title_end = line.find("</a>")
                if title_start > 0 and title_end > title_start:
                    title = line[title_start:title_end].strip()
                    # Get snippet from next lines
                    snippet = ""
                    for j in range(i + 1, min(i + 5, len(lines))):
                        if "result__snippet" in lines[j]:
                            snippet_start = lines[j].find(">") + 1
                            snippet_end = lines[j].find("</a>")
                            if snippet_start > 0:
                                snippet = lines[j][snippet_start:snippet_end].strip()
                            break
                    results.append({"title": title, "url": "", "snippet": snippet})
        return results
    except Exception as e:
        logger.warning(f"Web search failed: {e}")
        return []


def analyze_codebase(failure_context: dict[str, Any], root_dir: Path) -> list[dict[str, str]]:
    """Find relevant code for a failure context.

    Returns list of {file, line, excerpt} for code related to the failure.
    """
    results = []

    # Extract keywords from failure context
    keywords = set()
    if "error_type" in failure_context:
        keywords.add(failure_context["error_type"])
    if "root_cause" in failure_context:
        keywords.update(failure_context["root_cause"].lower().split()[:3])
    if "challenge_type" in failure_context:
        keywords.add(failure_context["challenge_type"])

    if not keywords:
        return results

    # Search for relevant code
    try:
        for keyword in keywords:
            if len(keyword) < 3:
                continue
            result = subprocess.run(
                ["grep", "-r", "-n", "-i", "--include=*.py", keyword, str(root_dir / "survey_agent")],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n")[:3]:
                    if ":" in line:
                        parts = line.split(":", 2)
                        if len(parts) >= 3:
                            results.append({
                                "file": parts[0].replace(str(root_dir) + "/", ""),
                                "line": parts[1],
                                "excerpt": parts[2].strip()[:200],
                            })
    except Exception as e:
        logger.warning(f"Codebase analysis failed: {e}")

    return results[:10]  # Limit results


def check_learnings(
    failure_signature: str, learnings_path: Path
) -> list[dict[str, Any]]:
    """Check past learnings for similar failure patterns.

    Returns list of relevant past learnings.
    """
    if not learnings_path.exists():
        return []

    results = []
    try:
        with open(learnings_path) as f:
            for line in f:
                try:
                    learning = json.loads(line.strip())
                    # Simple similarity check
                    if failure_signature in learning.get("failure_pattern", ""):
                        results.append(learning)
                    elif any(
                        word in learning.get("failure_pattern", "").lower()
                        for word in failure_signature.lower().split()[:3]
                    ):
                        results.append(learning)
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.warning(f"Learnings check failed: {e}")

    return results[:5]  # Limit results


def gather_research_context(
    failure_events: list[dict],
    data_dir: Path,
    root_dir: Path,
) -> dict[str, Any]:
    """Gather all research context for a set of failure events.

    Returns a context dict with web results, code analysis, and past learnings.
    """
    context: dict[str, Any] = {
        "web_results": [],
        "code_references": [],
        "past_learnings": [],
    }

    if not failure_events:
        return context

    # Build search query from failure events
    event = failure_events[0]
    event_type = event.get("event_type", "unknown")
    site = event.get("site", "unknown")
    failure_context = event.get("context", {})

    # Web search
    query_parts = [site, event_type]
    if "challenge_type" in failure_context:
        query_parts.append(failure_context["challenge_type"])
    if "root_cause" in failure_context:
        query_parts.append(failure_context["root_cause"])
    query = " ".join(query_parts) + " fix solution"
    context["web_results"] = search_web(query)

    # Code analysis
    context["code_references"] = analyze_codebase(failure_context, root_dir)

    # Past learnings
    learnings_path = data_dir / "brain" / "learnings.jsonl"
    signature = f"{site}/{event_type}"
    context["past_learnings"] = check_learnings(signature, learnings_path)

    return context
