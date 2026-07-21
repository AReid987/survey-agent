"""Two-stage LLM answer pipeline for survey questions.

Stage 1: Parse page elements into structured question groups
Stage 2: Ask LLM to answer questions (not write code/selectors)
Stage 3: Resolve LLM answers to actual element selectors
Stage 4: Deterministic fallback for any unanswered questions
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# ── Stage 0: Parse page elements into structured question groups ────────


def parse_question_groups(elements_raw: str, page_text: str) -> list[dict]:
    """Parse raw element JSON into structured question groups.

    Each group represents one survey question:
    - Radio groups (same name attribute = one question)
    - Checkbox groups (same name attribute = one question)
    - Text/textarea inputs (individual)
    - Select dropdowns (individual)
    """
    try:
        elements = json.loads(elements_raw) if elements_raw else []
    except (json.JSONDecodeError, TypeError):
        return []

    if not elements:
        return []

    groups: list[dict] = []
    seen_selectors: set[str] = set()

    # Radio groups — group by name
    radio_groups: dict[str, list[dict]] = {}
    for el in elements:
        if el.get("type") == "radio" and el.get("name"):
            radio_groups.setdefault(el["name"], []).append(el)

    for name, options in radio_groups.items():
        labels = [_get_element_label(o, page_text) for o in options]
        group = {
            "question_id": f"radio:{name}",
            "type": "radio",
            "options": [
                {
                    "value": o.get("value", ""),
                    "label": _get_element_label(o, page_text),
                    "checked": o.get("checked", False),
                    "selector": _build_element_selector(o),
                    "id": o.get("id", ""),
                }
                for o in options
            ],
            "all_labels": [l for l in labels if l],
        }
        groups.append(group)
        seen_selectors.add(name)

    # Checkbox groups — group by name
    checkbox_groups: dict[str, list[dict]] = {}
    for el in elements:
        if el.get("type") == "checkbox" and el.get("name"):
            checkbox_groups.setdefault(el["name"], []).append(el)

    for name, options in checkbox_groups.items():
        group = {
            "question_id": f"checkbox:{name}",
            "type": "checkbox",
            "options": [
                {
                    "value": o.get("value", ""),
                    "label": _get_element_label(o, page_text),
                    "checked": o.get("checked", False),
                    "selector": _build_element_selector(o),
                }
                for o in options
            ],
        }
        groups.append(group)

    # Text inputs and selects (individual)
    for el in elements:
        el_type = el.get("type", "")
        tag = el.get("tag", "")
        field_id = el.get("id", "") or el.get("name", "")

        if el_type in ("text", "email", "number", "tel") or tag == "textarea":
            if field_id and field_id not in seen_selectors:
                seen_selectors.add(field_id)
                label = _get_element_label(el, page_text)
                placeholder = el.get("placeholder", "")
                groups.append(
                    {
                        "question_id": f"text:{field_id or id(el)}",
                        "type": "textarea" if tag == "textarea" else "text",
                        "label": label or placeholder or "unlabeled text input",
                        "selector": _build_element_selector(el),
                        "placeholder": placeholder,
                    }
                )
        elif tag == "select":
            if field_id and field_id not in seen_selectors:
                seen_selectors.add(field_id)
                opts = el.get("opts", [])
                groups.append(
                    {
                        "question_id": f"select:{field_id}",
                        "type": "select",
                        "options": [
                            {"value": o.get("v", ""), "label": o.get("t", "")}
                            for o in opts
                        ],
                        "selector": _build_element_selector(el),
                    }
                )

    return groups


def _get_element_label(el: dict, page_text: str) -> str:
    """Get the human-readable label for an element."""
    text = el.get("text", "").strip()
    if text:
        return text
    # Try to find label from surrounding text context
    name = el.get("name", "")
    value = el.get("value", "")
    if name and value:
        # Look for "value" near "name" in page text
        lines = page_text.split("\n")
        for line in lines:
            if name.lower() in line.lower() and value.lower() in line.lower():
                return line.strip()[:100]
    return value or text or ""


def _build_element_selector(el: dict) -> str:
    """Build the most reliable CSS selector for an element."""
    el_id = el.get("id", "")
    el_name = el.get("name", "")
    # The JS extractor puts radio/checkbox values in 'text', not 'value'
    el_value = el.get("value") or el.get("text", "")
    el_tag = el.get("tag", "")
    el_text = el.get("text", "")

    # Prefer ID if present
    if el_id:
        return f"#{_css_escape(el_id)}"

    # For elements with both name and value (radio buttons, checkboxes)
    if el_name and el_value:
        # Escape special chars in name and value
        safe_name = el_name.replace("'", "\\'").replace('"', '\\"')
        safe_value = el_value.replace("'", "\\'").replace('"', '\\"')
        return f"{el_tag or 'input'}[name='{safe_name}'][value='{safe_value}']"

    # For elements with only name
    if el_name:
        safe_name = el_name.replace("'", "\\'").replace('"', '\\"')
        return f"{el_tag or 'input'}[name='{safe_name}']"

    # Fallback: use text content for label-based matching
    # The JS _apply_answers has a text-content fallback
    if el_tag:
        return el_tag

    return "input"


def _css_escape(s: str) -> str:
    """Escape a string for CSS selector — only escape truly necessary chars."""
    if not s:
        return s
    escaped = ""
    for i, ch in enumerate(s):
        cp = ord(ch)
        # Valid CSS identifier chars don't need escaping
        if ch.isalnum() or ch in ("-", "_"):
            escaped += ch
        elif cp == 0:
            escaped += "\ufffd"
        elif (0x01 <= cp <= 0x1F) or cp == 0x7F:
            escaped += f"\\{cp:02X} "
        elif i == 0 and ch.isdigit():
            escaped += f"\\{cp:02X} "
        else:
            escaped += f"\\{ch}"
    return escaped


# ── Stage 1: Build LLM prompt for answering questions ──────────────────


def build_answer_prompt(
    page_text: str,
    question_groups: list[dict],
    profile_context: str,
    is_prescreener: bool = False,
) -> str:
    """Build a prompt asking the LLM to answer questions (not generate code)."""
    # Format question groups for the prompt
    groups_text = []
    for g in question_groups:
        gtype = g["type"]
        qid = g["question_id"]
        if gtype in ("radio", "checkbox"):
            opts_text = "; ".join(
                f"{o['label']}" + (" (selected)" if o.get("checked") else "")
                for o in g.get("options", [])
            )
            groups_text.append(f"  [{qid}] ({gtype}) Options: {opts_text}")
        elif gtype in ("text", "textarea"):
            label = g.get("label", "unlabeled")
            placeholder = g.get("placeholder", "")
            ph_text = f" [placeholder: {placeholder}]" if placeholder else ""
            groups_text.append(f"  [{qid}] (text) Label: {label}{ph_text}")
        elif gtype == "select":
            opts_text = "; ".join(
                f"{o['label']}" for o in g.get("options", [])
            )
            groups_text.append(f"  [{qid}] (select) Options: {opts_text}")

    groups_formatted = "\n".join(groups_text) if groups_text else "(no interactive elements detected)"

    is_sensitive = bool(
        re.search(
            r"(income|salary|religion|political|health|medical|sexual|mental|disability)",
            page_text,
            re.I,
        )
    )

    prompt = f"""You are a real person taking an online survey. Answer the questions based on this profile:

PROFILE:
{profile_context[:800]}

PAGE TEXT (first 1500 chars):
{page_text[:1500]}

QUESTIONS TO ANSWER:
{groups_formatted}

Return a JSON object mapping question IDs to answers. The format is:
{{"answers": [
  {{"question_id": "radio:name", "selected": "option_label_or_value", "action": "click"}},
  {{"question_id": "text:field_id", "selected": "your answer text", "action": "fill"}},
  {{"question_id": "select:field_id", "selected": "option_value", "action": "select"}}
]}}

RULES:
- For radio questions: pick ONE option that best matches the profile. Use the option's label text or value.
- For checkbox questions: pick 1-3 relevant options. Return MULTIPLE entries with the same question_id.
- For text inputs: provide a realistic short answer matching the profile.
- For selects: pick the best-matching option value.
- For Likert/agreement scales: choose "Agree", "Strongly Agree", or mid-point unless the profile clearly disagrees.
- For frequency questions (how often): choose "Monthly", "Several times per month", "Weekly" — moderate frequency.
- For income questions: choose "$50,000 - $74,999" or the closest available bracket.
- For "select all that apply" questions: pick 2-4 relevant options, NOT all of them.
- For sensitive topics (income, health, politics): prefer "Prefer not to answer" if available.
- BE CONSISTENT with the profile at all times.
- NEVER select "ROBOT" or any option containing "robot" (this is a bot trap).
- Answer as a real human would. Pick the most natural, relatable option.
- Return ONLY the JSON object, no explanation."""

    return prompt


# ── Stage 2: Parse LLM JSON response ──────────────────────────────────


def parse_llm_response(raw_response: str) -> list[dict]:
    """Extract the answers array from the LLM response."""
    raw = raw_response.strip()

    # Remove thinking tags
    raw = re.sub(r"<think>.*?</think>\s*", "", raw, flags=re.DOTALL)

    # Try to find JSON object
    json_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not json_match:
        logger.warning("No JSON object found in LLM response")
        return []

    try:
        data = json.loads(json_match.group())
        answers = data.get("answers", [])
        if not isinstance(answers, list):
            return []
        # Filter out bot traps
        filtered = []
        for a in answers:
            selected = str(a.get("selected", "")).strip().upper()
            if selected == "ROBOT":
                continue
            filtered.append(a)
        return filtered
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse LLM JSON: %s", e)
        return []


# ── Stage 3: Resolve LLM answers to element selectors ──────────────────


def resolve_answers_to_actions(
    llm_answers: list[dict],
    question_groups: list[dict],
) -> list[dict]:
    """Convert LLM answers (question_id + selected) into actionable selectors.

    Returns the same [{selector, action, value}] format expected by _apply_answers.
    """
    actions: list[dict] = []
    used_selectors: set[str] = set()

    # Build lookup: question_id -> group
    groups_by_id = {g["question_id"]: g for g in question_groups}

    for ans in llm_answers:
        qid = ans.get("question_id", "")
        selected = ans.get("selected", "")
        action = ans.get("action", "click")

        group = groups_by_id.get(qid)
        if not group:
            logger.warning("Unknown question_id in LLM answer: %s", qid)
            continue

        gtype = group["type"]

        if gtype in ("radio",):
            # Find the matching option by label or value
            matched = _match_option(selected, group.get("options", []))
            if matched:
                sel = matched.get("selector", "")
                if sel and sel not in used_selectors:
                    used_selectors.add(sel)
                    actions.append({"selector": sel, "action": "click", "value": ""})
        elif gtype in ("checkbox",):
            matched = _match_option(selected, group.get("options", []))
            if matched:
                sel = matched.get("selector", "")
                if sel and sel not in used_selectors:
                    used_selectors.add(sel)
                    actions.append({"selector": sel, "action": "click", "value": ""})
        elif gtype in ("text", "textarea"):
            sel = group.get("selector", "")
            if sel and sel not in used_selectors:
                used_selectors.add(sel)
                actions.append({"selector": sel, "action": "fill", "value": str(selected)})
        elif gtype == "select":
            sel = group.get("selector", "")
            if sel and sel not in used_selectors:
                used_selectors.add(sel)
                actions.append({"selector": sel, "action": "select", "value": str(selected)})

    return actions


def _match_option(selected: str, options: list[dict]) -> dict | None:
    """Find the option that best matches the selected value."""
    if not selected:
        return None
    selected_lower = selected.strip().lower()

    # Exact match on label
    for opt in options:
        if opt.get("label", "").strip().lower() == selected_lower:
            return opt

    # Exact match on value
    for opt in options:
        if opt.get("value", "").strip().lower() == selected_lower:
            return opt

    # Substring match on label
    for opt in options:
        if selected_lower in opt.get("label", "").lower():
            return opt

    # Substring match on value
    for opt in options:
        if selected_lower in opt.get("value", "").lower():
            return opt

    # Fuzzy: first option whose label partially contains the selection
    for opt in options:
        label_lower = opt.get("label", "").lower()
        if label_lower and (label_lower in selected_lower or selected_lower in label_lower):
            return opt

    return None


# ── Stage 4: Deterministic fallback (uses _PROFILE from harness_backend) ──


def get_deterministic_actions(
    question_groups: list[dict],
    page_text: str,
    profile_dict: dict[str, str] | None = None,
) -> list[dict]:
    """Generate actions without LLM for common question types.

    Uses the profile_dict (from _PROFILE) for keyword-based matching.
    Falls back to safe defaults.
    """
    actions: list[dict] = []
    used_selectors: set[str] = set()

    page_lower = page_text.lower()
    profile = profile_dict or {}

    for group in question_groups:
        gtype = group["type"]
        qid = group["question_id"]
        options = group.get("options", [])
        selector = group.get("selector", "")

        if not options and gtype in ("radio", "checkbox", "select"):
            continue
        if not selector and gtype in ("text", "textarea", "select"):
            continue

        if gtype == "radio":
            picked = _pick_deterministic_radio(page_lower, options, profile, qid)
            if picked:
                sel = picked.get("selector", "")
                if sel and sel not in used_selectors:
                    used_selectors.add(sel)
                    actions.append({"selector": sel, "action": "click", "value": ""})

        elif gtype == "checkbox":
            # For checkboxes, pick "None of the above" if available
            none_opt = None
            for opt in options:
                label = (opt.get("label") or "").lower()
                if "none" in label or "none of the above" in label or "prefer not" in label:
                    none_opt = opt
                    break
            if none_opt:
                sel = none_opt.get("selector", "")
                if sel and sel not in used_selectors:
                    used_selectors.add(sel)
                    actions.append({"selector": sel, "action": "click", "value": ""})

        elif gtype in ("text", "textarea"):
            label = group.get("label", "").lower()
            placeholder = group.get("placeholder", "").lower()
            value = _resolve_text_value(label, placeholder, page_lower)
            if selector and selector not in used_selectors:
                used_selectors.add(selector)
                actions.append({"selector": selector, "action": "fill", "value": value})

        elif gtype == "select":
            if options:
                # Pick first non-empty option after "Select/Choose"
                for opt in options:
                    label = (opt.get("label") or "").lower()
                    val = opt.get("value", "")
                    if label not in ("select", "choose", "pick one", "") and val:
                        sel = selector  # Use the group selector for select
                        if sel and sel not in used_selectors:
                            used_selectors.add(sel)
                            actions.append({"selector": sel, "action": "select", "value": val})
                        break

    return actions


def _pick_deterministic_radio(
    page_lower: str,
    options: list[dict],
    profile: dict[str, str],
    qid: str,
) -> dict | None:
    """Pick a radio option deterministically using profile keywords."""

    # Profile keyword matching
    for key, value in profile.items():
        if key in page_lower:
            for opt in options:
                label = (opt.get("label") or "").lower()
                if str(value).lower() in label:
                    return opt

    # Gender
    if "gender" in page_lower or "sex" in page_lower:
        for opt in options:
            label = (opt.get("label") or "").lower()
            if "male" in label and "female" not in label:
                return opt

    # Age
    if "age" in page_lower:
        for opt in options:
            label = (opt.get("label") or "").lower()
            if any(a in label for a in ["35", "39", "25-44", "30-44", "35-44"]):
                return opt

    # Sensitive topics
    sensitive = ["income", "salary", "religion", "political", "health", "medical", "sexual", "mental"]
    if any(kw in page_lower for kw in sensitive):
        for opt in options:
            if "prefer not" in (opt.get("label") or "").lower():
                return opt

    # Positive/ownership questions
    positive = ["own", "have", "use", "purchase", "buy", "subscribe", "member"]
    if any(kw in page_lower for kw in positive):
        for opt in options:
            label = (opt.get("label") or "").lower().strip()
            if label in ("yes", "yeah", "yep", "true", "correct"):
                return opt

    # Prefer not to answer as general safety
    for opt in options:
        if "prefer not" in (opt.get("label") or "").lower():
            return opt

    # Default: middle option (often "neutral")
    if options:
        return options[len(options) // 2]
    return options[0] if options else None


def _resolve_text_value(label: str, placeholder: str, page_lower: str) -> str:
    """Resolve a text input value based on its label and placeholder."""
    combined = f"{label} {placeholder}".lower()

    if "email" in combined:
        return "read.musik@gmail.com"
    if "age" in combined:
        return "39"
    if any(n in combined for n in ["first name", "full name", "your name"]):
        return "Antonio"
    if "last name" in combined:
        return "Reid"
    if "phone" in combined:
        return "512-555-0142"
    if any(z in combined for z in ["zip", "postal", "zip code"]):
        return "78736"
    if "city" in combined:
        return "Austin"
    if "state" in combined:
        return "Texas"
    if "occupation" in combined or "job title" in combined:
        return "CTO / Founder"
    if "company" in combined or "employer" in combined:
        return "Self-employed"
    if "website" in combined:
        return ""
    if "comment" in combined or "feedback" in combined or "other" in combined:
        return "I enjoy sharing my opinions and helping improve products and services."
    if "textarea" in label or not label:
        # Generic textarea — often open-ended
        return "I feel that the products and services I use meet my needs well. There's always room for improvement in customer service and product features."

    return "N/A"


# ── Pipeline orchestrator ──────────────────────────────────────────────


async def generate_actions(
    page_text: str,
    elements_raw: str,
    profile_context: str,
    profile_dict: dict[str, str] | None = None,
    llm_call_fn=None,
) -> str:
    """Full pipeline: parse → LLM (if available) → deterministic → actions JSON.

    Args:
        page_text: Text content of the page.
        elements_raw: JSON string of interactive elements from the page.
        profile_context: User profile as a formatted string.
        profile_dict: Profile keyword dict for deterministic matching.
        llm_call_fn: Async callable (prompt) -> str response. If None, skip LLM stage.

    Returns:
        JSON string of actions: [{"selector": "...", "action": "click|fill|select", "value": "..."}]
    """
    # Stage 0: Parse elements into question groups
    groups = parse_question_groups(elements_raw, page_text)
    logger.info("Parsed %d question groups from page (%d chars text, %d chars elements)",
                 len(groups), len(page_text), len(elements_raw))

    if not groups:
        return "[]"

    llm_answers: list[dict] = []

    # Stage 1-2: LLM answering (if available)
    if llm_call_fn:
        try:
            prompt = build_answer_prompt(page_text, groups, profile_context)
            logger.info("Sending LLM prompt (%d chars) with %d question groups",
                        len(prompt), len(groups))
            raw_response = await llm_call_fn(prompt)
            if raw_response:
                llm_answers = parse_llm_response(raw_response)
                logger.info("LLM returned %d answers", len(llm_answers))
        except Exception as e:
            logger.warning("LLM answer stage failed: %s", e)

    # Stage 3: Resolve LLM answers to actionable selectors
    actions = resolve_answers_to_actions(llm_answers, groups)

    # Track which question IDs were answered by LLM
    answered_qids: set[str] = set()
    for ans in llm_answers:
        answered_qids.add(ans.get("question_id", ""))

    # Stage 4: Deterministic fallback for unanswered questions
    if profile_dict:
        det_actions = get_deterministic_actions(groups, page_text, profile_dict)
        # Only add deterministic actions for questions not already answered by LLM
        for det in det_actions:
            # Check if this selector/group was already handled
            sel = det.get("selector", "")
            if sel and not any(a.get("selector") == sel for a in actions):
                actions.append(det)

    logger.info("Total actions generated: %d (LLM: %d, deterministic: %d)",
                len(actions), len(llm_answers), len(actions) - len(llm_answers))

    return json.dumps(actions) if actions else "[]"
