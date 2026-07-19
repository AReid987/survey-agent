"""Survey completion engine — answers questions on survey pages.

Uses the LLM to generate answers based on the user profile,
then interacts with the page (click radio, fill text, submit).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def answer_quick_question(page: Any, profile_context: str, llm_fn: Any) -> bool:
    """Answer a quick-question on the dashboard (checkbox/radio + submit).

    Returns True if a question was answered.
    """
    try:
        # Check for quick-question forms on the dashboard
        form_data = await page.evaluate("""() => {
            const forms = document.querySelectorAll('form');
            for (const form of forms) {
                const text = form.closest('[class*="quick"], [class*="poll"], [class*="question"]')?.innerText ||
                             form.parentElement?.innerText?.substring(0, 500) || '';
                if (!text || text.length < 10) continue;

                const radios = form.querySelectorAll('input[type="radio"]');
                const checkboxes = form.querySelectorAll('input[type="checkbox"]');
                const submit = form.querySelector('button[type="submit"], input[type="submit"], button');

                if ((radios.length > 0 || checkboxes.length > 0) && submit) {
                    const options = [];
                    radios.forEach(r => {
                        const label = r.closest('label')?.textContent?.trim() ||
                                      document.querySelector(`label[for="${r.id}"]`)?.textContent?.trim() || r.value;
                        options.push({type: 'radio', name: r.name, value: r.value || label, label: label});
                    });
                    checkboxes.forEach(c => {
                        const label = c.closest('label')?.textContent?.trim() ||
                                      document.querySelector(`label[for="${c.id}"]`)?.textContent?.trim() || c.value;
                        options.push({type: 'checkbox', name: c.name, value: c.value || label, label: label});
                    });
                    return JSON.stringify({question: text.substring(0, 300), options: options, submitSelector: submit.id || submit.className});
                }
            }
            return null;
        }""")

        if not form_data:
            return False

        data = json.loads(form_data)
        question = data["question"]
        options = data["options"]

        if not options:
            return False

        # Ask LLM to pick the best answer
        prompt = f"""Answer this survey question as the user described below.

USER PROFILE:
{profile_context}

QUESTION: {question}

OPTIONS:
{json.dumps(options, indent=2)}

Reply with ONLY the label text of the best option. If multiple options apply (checkboxes), reply with a JSON array of labels."""

        answer = await llm_fn(prompt)
        logger.info(f"Quick question answer: {answer[:100]}")

        if not answer or answer == "None" or len(answer) < 2:
            return False

        # Parse answer and click the matching option
        if answer.startswith("["):
            # Multiple answers (checkboxes)
            try:
                selected = json.loads(answer)
            except json.JSONDecodeError:
                selected = [answer]
        else:
            selected = [answer.strip().strip('"')]

        for opt in options:
            opt_label = opt["label"].lower()
            for sel in selected:
                if sel.lower().strip() in opt_label or opt_label in sel.lower().strip():
                    # Find and click the element
                    try:
                        if opt["type"] == "radio":
                            await page.evaluate(f"""() => {{
                                const radios = document.querySelectorAll('input[type="radio"][name="{opt["name"]}"]');
                                for (const r of radios) {{
                                    const label = r.closest('label')?.textContent?.trim() || '';
                                    if (label.includes('{opt["label"][:30]}')) {{
                                        r.click();
                                        return true;
                                    }}
                                }}
                                return false;
                            }}""")
                        elif opt["type"] == "checkbox":
                            await page.evaluate(f"""() => {{
                                const cbs = document.querySelectorAll('input[type="checkbox"]');
                                for (const c of cbs) {{
                                    const label = c.closest('label')?.textContent?.trim() || '';
                                    if (label.includes('{opt["label"][:30]}')) {{
                                        c.click();
                                        return true;
                                    }}
                                }}
                                return false;
                            }}""")
                    except Exception as e:
                        logger.warning(f"Failed to click option: {e}")

        # Click submit — buttons are INPUT[type=submit], not BUTTON
        await asyncio.sleep(0.5)
        submitted = await page.evaluate("""() => {
            // Find visible submit INPUT elements (quick-question and poll)
            const btns = document.querySelectorAll('input.submit-quick-question, input.submit-poll, input[type="submit"]');
            for (const btn of btns) {
                if (btn.offsetParent !== null) {
                    btn.scrollIntoView({behavior: 'smooth', block: 'center'});
                    btn.click();
                    return 'clicked: ' + btn.className.substring(0, 50);
                }
            }
            return 'not found';
        }""")
        logger.info(f"Quick question submit: {submitted}")

        await asyncio.sleep(2)
        return True

    except Exception as e:
        logger.warning(f"Quick question answering failed: {e}")
        return False


async def answer_survey_question(page: Any, profile_context: str, llm_fn: Any) -> str:
    """Answer a single survey question. Returns 'answered', 'captcha', 'disqualified', 'completed', or 'error'."""

    try:
        page_data = await page.evaluate("""() => {
            const body = document.body?.innerText?.substring(0, 3000) || '';
            const url = location.href;

            // Check for disqualification
            if (body.toLowerCase().includes('not a match') || body.toLowerCase().includes('disqualified') ||
                body.toLowerCase().includes('screened out') || body.toLowerCase().includes('do not qualify')) {
                return JSON.stringify({status: 'disqualified', url: url});
            }

            // Check for completion
            if (body.toLowerCase().includes('thank you') || body.toLowerCase().includes('survey complete') ||
                body.toLowerCase().includes('successfully completed') || body.toLowerCase().includes('earned')) {
                return JSON.stringify({status: 'completed', url: url});
            }

            // Find question text
            const questionEls = document.querySelectorAll(
                '[class*="question"], [class*="survey-question"], [id*="question"], .q-text, legend, h2, h3'
            );
            let questionText = '';
            for (const el of questionEls) {
                const text = (el.textContent || '').trim();
                if (text.length > 10 && text.length < 500) {
                    questionText = text;
                    break;
                }
            }

            // Find answer options
            const options = [];
            const radios = document.querySelectorAll('input[type="radio"]');
            radios.forEach(r => {
                const label = r.closest('label')?.textContent?.trim() ||
                              document.querySelector(`label[for="${r.id}"]`)?.textContent?.trim() || r.value;
                if (label) options.push({type: 'radio', name: r.name, value: r.value, label: label.substring(0, 100)});
            });

            const checkboxes = document.querySelectorAll('input[type="checkbox"]');
            checkboxes.forEach(c => {
                const label = c.closest('label')?.textContent?.trim() ||
                              document.querySelector(`label[for="${c.id}"]`)?.textContent?.trim() || c.value;
                if (label) options.push({type: 'checkbox', name: c.name, value: c.value, label: label.substring(0, 100)});
            });

            const selects = document.querySelectorAll('select');
            selects.forEach(s => {
                const opts = [];
                Array.from(s.options).forEach(o => {
                    if (o.value && o.text.trim()) opts.push({value: o.value, label: o.text.trim()});
                });
                if (opts.length > 0) options.push({type: 'select', name: s.name, options: opts});
            });

            const textInputs = document.querySelectorAll('input[type="text"], textarea');
            textInputs.forEach(t => {
                if (t.offsetParent !== null) options.push({type: 'text', name: t.name || t.id, placeholder: t.placeholder});
            });

            // Find next/submit buttons
            const buttons = [];
            document.querySelectorAll('button, input[type="submit"]').forEach(b => {
                const text = (b.textContent || b.value || '').trim();
                if (text && b.offsetParent !== null) buttons.push(text.substring(0, 50));
            });

            return JSON.stringify({
                status: 'question',
                question: questionText,
                options: options,
                buttons: buttons,
                url: url,
                body_preview: body.substring(0, 500),
            });
        }""")

        data = json.loads(page_data)

        if data["status"] == "disqualified":
            return "disqualified"
        if data["status"] == "completed":
            return "completed"

        if data["status"] != "question" or not data.get("options"):
            return "no_question"

        question = data["question"]
        options = data["options"]

        # Generate answer using LLM
        prompt = f"""Answer this survey question as the user described below.

USER PROFILE:
{profile_context}

QUESTION: {question}

OPTIONS:
{json.dumps(options[:20], indent=2)}

Reply with ONLY the label text of the best option. For text inputs, reply with the text to enter."""

        answer = await llm_fn(prompt)
        logger.info(f"Survey answer: {answer[:100]}")

        # Apply the answer
        answer_clean = answer.strip().strip('"').strip("'")

        for opt in options:
            if opt["type"] == "radio":
                if answer_clean.lower() in opt["label"].lower() or opt["label"].lower() in answer_clean.lower():
                    await page.evaluate(f"""() => {{
                        const radios = document.querySelectorAll('input[type="radio"][name="{opt["name"]}"]');
                        for (const r of radios) {{
                            const label = r.closest('label')?.textContent?.trim() || '';
                            if (label.includes('{opt["label"][:30]}')) {{
                                r.click();
                                return true;
                            }}
                        }}
                        return false;
                    }}""")
                    break
            elif opt["type"] == "checkbox":
                if answer_clean.lower() in opt["label"].lower() or opt["label"].lower() in answer_clean.lower():
                    await page.evaluate(f"""() => {{
                        const cbs = document.querySelectorAll('input[type="checkbox"]');
                        for (const c of cbs) {{
                            const label = c.closest('label')?.textContent?.trim() || '';
                            if (label.includes('{opt["label"][:30]}')) {{
                                c.click();
                                return true;
                            }}
                        }}
                        return false;
                    }}""")
                    break
            elif opt["type"] == "select":
                try:
                    # Find the best matching option
                    for o in opt.get("options", []):
                        if answer_clean.lower() in o["label"].lower() or o["label"].lower() in answer_clean.lower():
                            await page.evaluate(f"""() => {{
                                const sel = document.querySelector('select[name="{opt["name"]}"]');
                                if (sel) {{ sel.value = '{o["value"]}'; sel.dispatchEvent(new Event('change', {{bubbles: true}})); return true; }}
                                return false;
                            }}""")
                            break
                except Exception:
                    pass
                break
            elif opt["type"] == "text":
                try:
                    sel = opt.get("name", "")
                    if sel:
                        escaped_answer = answer_clean.replace("'", "\\'")
                        await page.evaluate(
                            "(() => {"
                            f"const input = document.querySelector('input[name=\\\"{sel}\\\"], textarea[name=\\\"{sel}\\\"], #{sel}');"
                            "if (input) {"
                            f"input.value = '{escaped_answer}';"
                            "input.dispatchEvent(new Event('input', {bubbles: true}));"
                            "input.dispatchEvent(new Event('change', {bubbles: true}));"
                            "return true;}"
                            "return false;})()"
                        )
                except Exception:
                    pass
                break

        await asyncio.sleep(0.5)

        # Click Next/Submit
        await page.evaluate("""() => {
            const btns = document.querySelectorAll('button, input[type="submit"]');
            for (const btn of btns) {
                const text = (btn.textContent || btn.value || '').toLowerCase();
                if (btn.offsetParent !== null && (text.includes('next') || text.includes('submit') || text.includes('continue') || text.includes('>>'))) {
                    btn.click();
                    return true;
                }
            }
            return false;
        }""")

        await asyncio.sleep(2)
        return "answered"

    except Exception as e:
        logger.warning(f"Survey question answering failed: {e}")
        return "error"
