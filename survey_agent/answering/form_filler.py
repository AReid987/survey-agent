"""Form filler — handles all form input types in survey pages."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..browser.human_behavior import HumanBehavior
from .question_classifier import QuestionType

if TYPE_CHECKING:
    from ..browser.engine import BrowserPage


class FormFiller:
    """Fill survey form inputs based on question type and answer value."""

    def __init__(self, behavior: HumanBehavior | None = None):
        self._behavior = behavior or HumanBehavior()

    async def fill(
        self,
        page: BrowserPage,
        question_type: QuestionType,
        selector: str,
        value: Any,
        options: list[str] | None = None,
    ) -> bool:
        """Fill a form element based on its type.

        Args:
            page: The browser page.
            question_type: The type of question.
            selector: CSS selector for the form element or container.
            value: The answer value.
            options: Available options (for radio/checkbox/select).

        Returns:
            True if the form was filled successfully.
        """
        try:
            match question_type:
                case QuestionType.RADIO:
                    return await self._fill_radio(page, selector, value, options)
                case QuestionType.CHECKBOX:
                    return await self._fill_checkbox(page, selector, value, options)
                case QuestionType.TEXT | QuestionType.OPEN_ENDED:
                    return await self._fill_text(page, selector, str(value))
                case QuestionType.TEXTAREA:
                    return await self._fill_textarea(page, selector, str(value))
                case QuestionType.SELECT:
                    return await self._fill_select(page, selector, str(value))
                case QuestionType.SCALE:
                    return await self._fill_scale(page, selector, value)
                case QuestionType.SLIDER:
                    return await self._fill_slider(page, selector, value)
                case QuestionType.DATE:
                    return await self._fill_date(page, selector, str(value))
                case _:
                    return await self._fill_text(page, selector, str(value))
        except Exception as e:
            print(f"Form fill error: {e}")
            return False

    async def _fill_radio(
        self, page: BrowserPage, container_selector: str, value: Any, options: list[str] | None
    ) -> bool:
        """Click a radio button matching the value."""
        # Try to find radio inputs within the container
        radios = await page.query_selector_all(f"{container_selector} input[type='radio']")
        if not radios:
            # Try label-based matching
            labels = await page.query_selector_all(f"{container_selector} label")
            for label in labels:
                text = await label.evaluate("el => el.textContent.trim()")
                if self._matches(str(value), text):
                    await self._behavior.human_click(page, f"{container_selector} label:has-text('{text}')")
                    return True
            return False

        # Try matching by label text
        for label in await page.query_selector_all(f"{container_selector} label"):
            text = await label.evaluate("el => el.textContent.trim()")
            if self._matches(str(value), text):
                await label.click()
                await self._behavior.random_delay(0.2)
                return True

        # Fallback: click by value attribute
        for radio in radios:
            radio_value = await radio.evaluate("el => el.value")
            if self._matches(str(value), str(radio_value)):
                await radio.click()
                await self._behavior.random_delay(0.2)
                return True

        return False

    async def _fill_checkbox(
        self, page: BrowserPage, container_selector: str, value: Any, options: list[str] | None
    ) -> bool:
        """Check checkbox(es) matching the value."""
        values = value if isinstance(value, list) else [value]
        checked_any = False

        for val in values:
            val_str = str(val)
            # Try label matching
            labels = await page.query_selector_all(f"{container_selector} label")
            for label in labels:
                text = await label.evaluate("el => el.textContent.trim()")
                if self._matches(val_str, text):
                    # Get the associated checkbox
                    for_id = await label.evaluate("el => el.getAttribute('for')")
                    if for_id:
                        checkbox = await page.query_selector(f"#{for_id}")
                        if checkbox:
                            is_checked = await checkbox.evaluate("el => el.checked")
                            if not is_checked:
                                await checkbox.click()
                            checked_any = True
                            break
                    else:
                        # Checkbox inside label
                        checkbox = await label.query_selector("input[type='checkbox']")
                        if checkbox:
                            is_checked = await checkbox.evaluate("el => el.checked")
                            if not is_checked:
                                await checkbox.click()
                            checked_any = True
                            break
            await self._behavior.random_delay(0.2)

        return checked_any

    async def _fill_text(self, page: BrowserPage, selector: str, value: str) -> bool:
        """Fill a text input."""
        element = await page.query_selector(selector)
        if not element:
            return False
        await self._behavior.human_type(page, selector, value)
        return True

    async def _fill_textarea(self, page: BrowserPage, selector: str, value: str) -> bool:
        """Fill a textarea."""
        element = await page.query_selector(selector)
        if not element:
            return False
        await self._behavior.human_type(page, selector, value)
        return True

    async def _fill_select(self, page: BrowserPage, selector: str, value: str) -> bool:
        """Select an option from a dropdown."""
        element = await page.query_selector(selector)
        if not element:
            return False
        # Get all options
        options = await page.evaluate(f"""
            Array.from(document.querySelector('{selector}').options).map(o => ({{value: o.value, text: o.text}}))
        """)
        # Find best match
        for opt in options:
            if self._matches(value, opt.get("text", "")) or self._matches(value, opt.get("value", "")):
                await self._behavior.human_select(page, selector, opt["value"])
                return True
        # Fallback: try direct value
        try:
            await self._behavior.human_select(page, selector, value)
            return True
        except Exception:
            return False

    async def _fill_scale(self, page: BrowserPage, container_selector: str, value: Any) -> bool:
        """Click a scale rating (1-5, 1-10, etc.)."""
        val = int(value) if isinstance(value, (int, float, str)) and str(value).isdigit() else 3
        # Try clicking by value/label
        labels = await page.query_selector_all(f"{container_selector} label")
        for label in labels:
            text = await label.evaluate("el => el.textContent.trim()")
            if text.strip() == str(val):
                await label.click()
                await self._behavior.random_delay(0.2)
                return True
        # Try radio inputs
        radios = await page.query_selector_all(f"{container_selector} input[type='radio']")
        if len(radios) >= val > 0:
            await radios[val - 1].click()
            await self._behavior.random_delay(0.2)
            return True
        return False

    async def _fill_slider(self, page: BrowserPage, selector: str, value: Any) -> bool:
        """Set a range slider value."""
        try:
            await page.evaluate(f"""
                const el = document.querySelector('{selector}');
                const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                nativeInputValueSetter.call(el, {value});
                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
            """)
            return True
        except Exception:
            return False

    async def _fill_date(self, page: BrowserPage, selector: str, value: str) -> bool:
        """Fill a date input."""
        try:
            await page.evaluate(f"""
                const el = document.querySelector('{selector}');
                const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                nativeInputValueSetter.call(el, '{value}');
                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
            """)
            return True
        except Exception:
            return False

    @staticmethod
    def _matches(expected: str, actual: str) -> bool:
        """Case-insensitive fuzzy match."""
        return expected.lower().strip() in actual.lower().strip() or actual.lower().strip() in expected.lower().strip()
