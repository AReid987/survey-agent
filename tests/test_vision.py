"""Tests for the local vision escalation path.

The LM Studio SDK is stubbed throughout — these tests must pass on a machine
with no vision model downloaded and no LM Studio running, because that is the
state CI and a fresh checkout are in.

The behaviour that matters most: every failure mode returns None. A vision layer
that guesses at a CAPTCHA is worse than one that admits defeat, because a wrong
answer burns the survey and can flag the account, while None escalates to the
owner.
"""

from __future__ import annotations

import pytest

from survey_agent import vision


@pytest.fixture(autouse=True)
def _clear_model_cache():
    """The resolved model key is process-cached; reset around every test."""
    vision.reset_model_cache()
    yield
    vision.reset_model_cache()


@pytest.fixture()
def fake_vision(monkeypatch):
    """Pretend a vision model is installed and script its replies.

    Returns a setter; call it with the string the model should "say".
    """
    state = {"reply": "", "raise": None, "prompts": []}

    def _set(reply: str = "", raises: Exception | None = None):
        state["reply"] = reply
        state["raise"] = raises

    class _Prediction:
        def __init__(self, content):
            self.content = content

    class _Handle:
        def respond(self, chat, config=None):  # noqa: ARG002
            if state["raise"] is not None:
                raise state["raise"]
            return _Prediction(state["reply"])

    class _Chat:
        def add_user_message(self, prompt, images=None):  # noqa: ARG002
            state["prompts"].append(prompt)

    monkeypatch.setattr(vision, "list_downloaded_vision_models", lambda: ["prism-ml/bonsai-27b"])
    monkeypatch.setattr(vision, "_load_model", lambda key: _Handle())  # noqa: ARG005
    monkeypatch.setattr(vision, "_prepare_image", lambda image: object())  # noqa: ARG005

    fake_lms = type("_LMS", (), {"Chat": _Chat})
    monkeypatch.setitem(__import__("sys").modules, "lmstudio", fake_lms)

    return _set


# ── Model discovery ─────────────────────────────────────────────────────


def test_looks_multimodal_recognizes_vision_model_names():
    assert vision._looks_multimodal("qwen/qwen3-vl-8b")
    assert vision._looks_multimodal("llava-1.6-mistral-7b")
    assert vision._looks_multimodal("moondream2")
    assert vision._looks_multimodal("SmolVLM2-2.2B")


def test_looks_multimodal_rejects_text_only_models():
    """The models actually on this machine as of 2026-08-26 are text-only."""
    assert not vision._looks_multimodal("whiterabbitneo_whiterabbitneo-v3-7b")
    assert not vision._looks_multimodal("text-embedding-nomic-embed-text-v1.5")
    assert not vision._looks_multimodal("qwen3-coder:480b")


def test_no_models_means_not_ready(monkeypatch):
    monkeypatch.setattr(vision, "list_downloaded_vision_models", lambda: [])
    assert vision.vision_ready() is False


def test_preferred_model_wins_over_other_vision_models(monkeypatch):
    monkeypatch.setattr(
        vision,
        "list_downloaded_vision_models",
        lambda: ["prism-ml/bonsai-27b", "qwen/qwen3-vl-8b"],
    )
    assert vision._resolve_model_key() == "prism-ml/bonsai-27b"


def test_model_list_survives_sdk_errors(monkeypatch):
    """An unreachable LM Studio must not raise into the survey loop."""
    monkeypatch.setattr(vision, "sdk_available", lambda: True)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("connection refused")

    fake_lms = type("_LMS", (), {"list_downloaded_models": staticmethod(_boom)})
    monkeypatch.setitem(__import__("sys").modules, "lmstudio", fake_lms)

    assert vision.list_downloaded_vision_models() == []


def test_missing_sdk_returns_no_models(monkeypatch):
    monkeypatch.setattr(vision, "sdk_available", lambda: False)
    assert vision.list_downloaded_vision_models() == []


# ── Reasoning-model helpers ─────────────────────────────────────────────


def test_strip_reasoning_removes_think_blocks():
    text = "<think>\nSome thinking here\n</think>\nFinal answer: Xy7Kq2"
    assert vision._strip_reasoning(text) == "Final answer: Xy7Kq2"


def test_strip_reasoning_handles_no_think_blocks():
    text = 'The image shows a captcha-like image with the text "wwWuQ!".'
    assert vision._strip_reasoning(text) == text


def test_extract_captcha_from_reasoning_finds_double_quoted():
    text = 'The image shows a captcha-like image with the text "wwWuQ!".'
    assert vision._extract_captcha_from_reasoning(text) == "wwWuQ!"


def test_extract_captcha_from_reasoning_finds_backtick_quoted():
    text = "The CAPTCHA is `Xy7Kq2`. Reproduce it exactly."
    assert vision._extract_captcha_from_reasoning(text) == "Xy7Kq2"


def test_extract_captcha_from_reasoning_skips_unreadable_sentinel():
    text = 'The model replies: "UNREADABLE" because the text is too blurry.'
    assert vision._extract_captcha_from_reasoning(text) is None


def test_extract_captcha_from_reasoning_returns_none_when_no_quotes():
    text = "The image shows some text but I cannot make it out clearly."
    assert vision._extract_captcha_from_reasoning(text) is None


def test_read_captcha_uses_quoted_extraction_for_bonsai(fake_vision, tmp_path):
    """Bonsai-style inline reasoning with quoted answer."""
    fake_vision(
        "The user wants me to read the text from the image.\n"
        'The image shows a captcha-like image with the text "wwWuQ!".\n'
        "I need to reproduce it exactly.\n"
        "Let's check the characters: w, w, W, u, Q, !\n"
        'Wait, looking closely at the image: "wwWuQ!"'
    )
    img = tmp_path / "c.png"
    img.write_bytes(b"fake")
    assert vision.read_captcha(img) == "wwWuQ!"


# ── CAPTCHA reading ─────────────────────────────────────────────────────


def test_read_captcha_returns_none_when_no_model(monkeypatch, tmp_path):
    monkeypatch.setattr(vision, "list_downloaded_vision_models", lambda: [])
    img = tmp_path / "c.png"
    img.write_bytes(b"fake")
    assert vision.read_captcha(img) is None


def test_read_captcha_extracts_the_string(fake_vision, tmp_path):
    fake_vision("Xy7Kq2")
    img = tmp_path / "c.png"
    img.write_bytes(b"fake")
    assert vision.read_captcha(img) == "Xy7Kq2"


def test_read_captcha_preserves_case(fake_vision, tmp_path):
    """Case-sensitivity is the whole reason we need vision here."""
    fake_vision("aBcDeF")
    img = tmp_path / "c.png"
    img.write_bytes(b"fake")
    assert vision.read_captcha(img) == "aBcDeF"


def test_read_captcha_strips_quotes_and_prose(fake_vision, tmp_path):
    fake_vision('"H4RD7O"')
    img = tmp_path / "c.png"
    img.write_bytes(b"fake")
    assert vision.read_captcha(img) == "H4RD7O"


def test_read_captcha_honours_the_unreadable_sentinel(fake_vision, tmp_path):
    """Model admitting defeat must produce None, not the literal sentinel."""
    fake_vision("UNREADABLE")
    img = tmp_path / "c.png"
    img.write_bytes(b"fake")
    assert vision.read_captcha(img) is None


def test_read_captcha_rejects_an_implausibly_long_answer(fake_vision, tmp_path):
    fake_vision("I think the image probably shows something like a word but I am not sure at all")
    img = tmp_path / "c.png"
    img.write_bytes(b"fake")
    # Either None, or a short extracted token — never the whole sentence.
    result = vision.read_captcha(img)
    assert result is None or len(result) <= 24


def test_read_captcha_returns_none_when_the_model_errors(fake_vision, tmp_path):
    fake_vision(raises=RuntimeError("model crashed"))
    img = tmp_path / "c.png"
    img.write_bytes(b"fake")
    assert vision.read_captcha(img) is None


def test_read_captcha_returns_none_on_empty_reply(fake_vision, tmp_path):
    fake_vision("")
    img = tmp_path / "c.png"
    img.write_bytes(b"fake")
    assert vision.read_captcha(img) is None


@pytest.mark.usefixtures("fake_vision")
def test_read_captcha_handles_missing_file():
    assert vision.read_captcha("/nonexistent/path/x.png") is None


def test_read_captcha_accepts_raw_bytes(fake_vision):
    """Screenshots arrive as bytes from CDP, not always as files."""
    fake_vision("BYT3S")
    assert vision.read_captcha(b"\x89PNG fake bytes") == "BYT3S"


def test_captcha_prompt_demands_exact_case():
    assert "EXACTLY" in vision.CAPTCHA_PROMPT
    assert "uppercase and lowercase" in vision.CAPTCHA_PROMPT


# ── Screen description ──────────────────────────────────────────────────


def test_describe_screen_returns_the_answer(fake_vision, tmp_path):
    fake_vision("There is a blue Continue button at the bottom right.")
    img = tmp_path / "s.png"
    img.write_bytes(b"fake")
    answer = vision.describe_screen(img, "What buttons are visible?")
    assert answer is not None
    assert "Continue" in answer


def test_describe_screen_returns_none_on_unknown(fake_vision, tmp_path):
    fake_vision("UNKNOWN")
    img = tmp_path / "s.png"
    img.write_bytes(b"fake")
    assert vision.describe_screen(img, "Is there a checkbox?") is None


def test_describe_screen_returns_none_without_a_model(monkeypatch, tmp_path):
    monkeypatch.setattr(vision, "list_downloaded_vision_models", lambda: [])
    img = tmp_path / "s.png"
    img.write_bytes(b"fake")
    assert vision.describe_screen(img, "anything?") is None


# ── Target location ─────────────────────────────────────────────────────


def test_locate_target_parses_coordinates(fake_vision, tmp_path):
    fake_vision("412,689")
    img = tmp_path / "s.png"
    img.write_bytes(b"fake")
    assert vision.locate_target(img, "Continue button") == (412, 689)


def test_locate_target_tolerates_spacing(fake_vision, tmp_path):
    fake_vision("The button is at 100 , 250")
    img = tmp_path / "s.png"
    img.write_bytes(b"fake")
    assert vision.locate_target(img, "Next button") == (100, 250)


def test_locate_target_returns_none_when_not_found(fake_vision, tmp_path):
    fake_vision("NOTFOUND")
    img = tmp_path / "s.png"
    img.write_bytes(b"fake")
    assert vision.locate_target(img, "submit button") is None


def test_locate_target_returns_none_on_unparseable_reply(fake_vision, tmp_path):
    """Never click a guessed position."""
    fake_vision("somewhere near the middle")
    img = tmp_path / "s.png"
    img.write_bytes(b"fake")
    assert vision.locate_target(img, "button") is None


# ── Utilities ───────────────────────────────────────────────────────────


def test_image_to_data_url_round_trips(tmp_path):
    import base64

    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG payload")
    url = vision.image_to_data_url(img)
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == b"\x89PNG payload"


def test_reset_model_cache_forces_rediscovery(monkeypatch):
    calls = {"n": 0}

    def _list():
        calls["n"] += 1
        return ["qwen/qwen3-vl-8b"]

    monkeypatch.setattr(vision, "list_downloaded_vision_models", _list)
    vision._resolve_model_key()
    vision._resolve_model_key()
    assert calls["n"] == 1  # cached

    vision.reset_model_cache()
    vision._resolve_model_key()
    assert calls["n"] == 2  # rediscovered
