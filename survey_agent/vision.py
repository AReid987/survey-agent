"""Local vision reading via LM Studio — the escalation path for untrusted DOM.

When DOM extraction fails, the agent needs to *look* at the screen: read a
case-sensitive CAPTCHA string, or locate a click target inside an iframe that
refuses to yield text. This module is that capability, backed by a small
vision-language model running locally through LM Studio (owner's choice,
2026-08-26 — MLX-backed on Apple Silicon, driven by the `lms` CLI and the
`lmstudio` Python SDK).

Design constraints that come from the survey domain:

* **Low context, high fidelity.** The model gets one image and one narrow
  question. It is never asked to plan; it reads characters or names a target.
* **Fast enough not to blow a survey timeout.** Loads are cached and the model
  is kept resident with a TTL rather than re-loaded per call.
* **Never fabricate.** A failed read returns None so the caller can escalate to
  the owner. A confidently wrong CAPTCHA guess burns the survey; admitting
  failure does not.

Nothing here talks to the network. If LM Studio is not running or no vision
model is present, every entry point degrades to ``None``/``False`` and says why.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# Preferred model, then fallbacks, in descending capability order. Owner
# chooses these by downloading into LM Studio; we discover them at runtime
# via the lmstudio SDK. Order matters: the first downloaded match wins.
# LFM2.5-VL models are optimized for low VRAM (450M=0.9GB, 1.6B=3.2GB) and are
# ideal for captcha/iframe tasks on 16GB Apple Silicon. Bonsai 27B (8.5GB) is
# kept as a fallback but is too large for this machine alongside Chrome.
PREFERRED_VISION_MODELS: tuple[str, ...] = (
    "lfm2.5-vl-450m-mlx",  # LFM2.5-VL 450M MLX 8bit, 562MB — Apple Silicon native, fits 16GB
    "lfm2.5-vl-450m",  # fallback torch variant (needs MLX runtime, may fail)
    "lfm2.5-vl-1.6b",
    "lfm2.5-vl-3b",
    "liquidai/lfm2.5-vl-450m",
    "liquidai/lfm2.5-vl-1.6b",
    "liquidai/lfm2.5-vl-3b",
    "qwen/qwen3-vl-4b",
    "qwen/qwen3-vl-8b",
    "prism-ml/bonsai-27b",  # 27B, 8.52 GB — only if no LFM available
    "qwen/qwen3-vl-30b",
)

# Curated set of known-multimodal model keys. The lmstudio SDK does not expose
# vision capability metadata, so we maintain a small explicit list alongside
# the heuristic marker check below.
KNOWN_MULTIMODAL: frozenset[str] = frozenset(
    {
        "prism-ml/bonsai-27b",
        "lfm2.5-vl-450m-mlx",
        "lfm2.5-vl-450m",
        "lfm2.5-vl-1.6b",
        "lfm2.5-vl-3b",
        "liquidai/lfm2.5-vl-450m",
        "liquidai/lfm2.5-vl-1.6b",
        "liquidai/lfm2.5-vl-3b",
    }
    | {m for m in PREFERRED_VISION_MODELS if "-vl" in m or "vl" in m}
)

# Substrings marking a model key as multimodal (publishers name these loosely).
VISION_MARKERS: tuple[str, ...] = (
    "-vl",
    "vision",
    "llava",
    "moondream",
    "smolvlm",
    "pixtral",
    "minicpm-v",
    "internvl",
    "gemma-3",
    "lfm",
    "liquidai",
)

# Keep the model resident between escalations, but release it when a run ends.
MODEL_TTL_SECONDS = 900

CAPTCHA_PROMPT = (
    'Ignore the text "Please enter the text that you see in the image below." and the input field. '
    "Look ONLY at the small colorful noisy rectangle in the center. "
    "What distorted characters are inside that rectangle? Return ONLY those characters EXACTLY, preserving uppercase and lowercase. "
    "If not legible, reply with exactly: UNREADABLE"
)

CAPTCHA_MAX_TOKENS = 128  # reasoning models need headroom for thinking tokens

# CAPTCHAs often include special characters, so the extractor must accept them.
CAPTCHA_CHARS = r"0-9A-Za-z!@#$%^&*()_+-="
CAPTCHA_PATTERN = rf"[{CAPTCHA_CHARS}]{{3,24}}"

UNREADABLE_SENTINEL = "UNREADABLE"


def _strip_reasoning(text: str) -> str:
    """Remove <think>...</think> blocks from model output.

    Some reasoning models (e.g. Prism ML Bonsai) emit thinking tokens before
    the actual answer. The survey domain needs the final answer only.
    """
    import re as _re  # noqa: PLC0415

    # Remove all <think>...</think> blocks.
    text = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL | _re.IGNORECASE)
    return text.strip()


def _extract_captcha_from_reasoning(text: str) -> str | None:
    """Pull the CAPTCHA string out of inline reasoning text.

    Some reasoning models quote the answer inside their thinking output
    instead of emitting it cleanly. We look for the first plausible quoted
    string and return it.
    """
    import re as _re  # noqa: PLC0415

    # Look for text in backticks, double quotes, or single quotes.
    for pattern in (
        rf"`([{CAPTCHA_CHARS}]{{3,20}})`",
        rf'"([{CAPTCHA_CHARS}]{{3,20}})"',
        rf"'([{CAPTCHA_CHARS}]{{3,20}})'",
    ):
        if match := _re.search(pattern, text):
            candidate = match.group(1)
            if candidate.lower() != UNREADABLE_SENTINEL.lower():
                return candidate

    return None


class VisionUnavailableError(RuntimeError):
    """No usable local vision model. Callers should escalate to the owner."""


@dataclass(frozen=True)
class VisionRead:
    """The result of asking the model to read something on screen."""

    text: str
    model: str
    ok: bool = True

    def __bool__(self) -> bool:
        return self.ok and bool(self.text)


def _looks_multimodal(model_key: str) -> bool:
    lowered = model_key.lower()
    if any(marker in lowered for marker in VISION_MARKERS):
        return True
    # Owner-curated list for models whose keys don't carry a vision marker.
    return model_key in KNOWN_MULTIMODAL


def sdk_available() -> bool:
    """True when the lmstudio Python SDK can be imported."""
    try:
        import lmstudio  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def list_downloaded_vision_models() -> list[str]:
    """Vision-capable model keys present on disk, preferred ones first.

    Returns an empty list when the SDK is missing, LM Studio is unreachable, or
    nothing multimodal is downloaded — all of which mean "escalate instead".
    """
    if not sdk_available():
        return []
    try:
        import lmstudio as lms  # noqa: PLC0415

        keys: list[str] = []
        for entry in lms.list_downloaded_models("llm"):
            key = getattr(entry, "model_key", None) or str(entry)
            if _looks_multimodal(key):
                keys.append(key)
    except Exception as exc:
        logger.warning("vision: could not list LM Studio models: %s", exc)
        return []

    # Preferred models first, then anything else multimodal we found.
    ordered = [m for m in PREFERRED_VISION_MODELS if m in keys]
    ordered += sorted(k for k in keys if k not in ordered)
    return ordered


@lru_cache(maxsize=1)
def _resolve_model_key() -> str | None:
    """Pick which downloaded vision model to use. Cached per process."""
    models = list_downloaded_vision_models()
    if not models:
        logger.warning(
            "vision: no multimodal model downloaded. Install one with: "
            "lms get https://huggingface.co/LiquidAI/LFM2.5-VL-450M --yes  # 902MB MLX, fits 16GB"
        )
        return None
    logger.info("vision: using %s", models[0])
    return models[0]


def vision_ready() -> bool:
    """True when a local vision read could actually be attempted."""
    return _resolve_model_key() is not None


def _load_model(model_key: str):
    """Get a handle to the model, loading it into memory if needed."""
    import lmstudio as lms  # noqa: PLC0415

    # `llm()` reuses an already-loaded instance when one matches, so repeated
    # escalations in a run do not pay the load cost again.
    return lms.llm(model_key, ttl=MODEL_TTL_SECONDS)


def _prepare_image(image: str | Path | bytes):
    """Hand an image to the SDK, from a path or raw PNG bytes."""
    import lmstudio as lms  # noqa: PLC0415

    if isinstance(image, bytes):
        # prepare_image accepts a path; write bytes to a temp file first.
        import tempfile  # noqa: PLC0415

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tf.write(image)
            temp_path = tf.name
        try:
            return lms.prepare_image(temp_path)
        finally:
            Path(temp_path).unlink(missing_ok=True)

    path = Path(image)
    if not path.exists():
        raise FileNotFoundError(f"image not found: {path}")
    return lms.prepare_image(str(path))


def _ask(image: str | Path | bytes, prompt: str, max_tokens: int = 64) -> VisionRead | None:
    """Single-turn image question. Returns None on any failure."""
    model_key = _resolve_model_key()
    if model_key is None:
        return None

    try:
        import lmstudio as lms  # noqa: PLC0415

        handle = _load_model(model_key)
        image_handle = _prepare_image(image)
        chat = lms.Chat()
        chat.add_user_message(prompt, images=[image_handle])
        prediction = handle.respond(chat, config={"maxTokens": max_tokens, "temperature": 0.0})
        raw = str(getattr(prediction, "content", prediction))
        answer = _strip_reasoning(raw).strip()
    except Exception as exc:
        logger.warning("vision: read failed (%s): %s", model_key, exc)
        return None

    if not answer:
        return None
    return VisionRead(text=answer, model=model_key)


def read_captcha(image: str | Path | bytes) -> str | None:
    """Read a case-sensitive character string from a CAPTCHA image.

    Returns the characters, or None when unreadable / unavailable. None is the
    signal to escalate — never guess at a CAPTCHA, since a wrong answer burns
    the survey and can flag the account.
    """
    result = _ask(image, CAPTCHA_PROMPT, max_tokens=CAPTCHA_MAX_TOKENS)
    if result is None:
        return None

    answer = result.text.strip()
    # Reasoning models often quote the answer inside inline thinking text.
    # Try to pull the quoted string before falling back to regex.
    quoted = _extract_captcha_from_reasoning(answer)
    if quoted is not None:
        logger.info("vision: read CAPTCHA as %r via %s", quoted, result.model)
        return quoted

    # Fallback: strip quotes and prose, then extract the first plausible token.
    answer = answer.strip("\"'`").strip()
    match = re.search(CAPTCHA_PATTERN, answer)
    candidate = match.group(0) if match else answer

    if UNREADABLE_SENTINEL.lower() in answer.lower():
        logger.info("vision: model reported the CAPTCHA as unreadable")
        return None
    if not candidate or len(candidate) > 24:
        logger.info("vision: implausible CAPTCHA read %r — treating as failure", answer[:40])
        return None

    logger.info("vision: read CAPTCHA as %r via %s", candidate, result.model)
    return candidate


def describe_screen(image: str | Path | bytes, question: str) -> str | None:
    """Ask a narrow question about what is on screen.

    Used when the DOM is unusable and the agent needs one specific fact — "what
    is the text of the button at the bottom?", "is there a consent checkbox?".
    Deliberately not a planning interface.
    """
    prompt = (
        f"{question}\n\n"
        "Answer in one short sentence. State only what is visibly present. "
        "If you cannot tell from the image, reply with exactly: UNKNOWN"
    )
    result = _ask(image, prompt, max_tokens=96)
    if result is None:
        return None
    if result.text.strip().upper().startswith("UNKNOWN"):
        return None
    return result.text.strip()


def locate_target(image: str | Path | bytes, target_description: str) -> tuple[int, int] | None:
    """Find where to click for ``target_description``, in image pixel coords.

    Returns None when the model cannot find it — the caller must then escalate
    rather than clicking a guessed position.
    """
    prompt = (
        f"Find the {target_description} in this screenshot. "
        "Reply with ONLY its center pixel coordinates as two integers "
        "separated by a comma, e.g. 412,689. "
        "If it is not visible, reply with exactly: NOTFOUND"
    )
    result = _ask(image, prompt, max_tokens=24)
    if result is None:
        return None

    answer = result.text.strip()
    if "notfound" in answer.lower().replace(" ", ""):
        return None
    if match := re.search(r"(\d{1,5})\s*,\s*(\d{1,5})", answer):
        x, y = int(match.group(1)), int(match.group(2))
        logger.info("vision: located %r at (%d, %d)", target_description, x, y)
        return x, y

    logger.info("vision: could not parse coordinates from %r", answer[:40])
    return None


def image_to_data_url(image_path: str | Path) -> str:
    """Encode a PNG as a data URL (for backends that want inline images)."""
    data = Path(image_path).read_bytes()
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def reset_model_cache() -> None:
    """Forget the resolved model. Call after installing or removing models."""
    _resolve_model_key.cache_clear()
