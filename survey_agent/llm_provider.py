"""LLM provider factory — uses browser_use LLM wrappers for Agent compatibility."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)


# Provider configurations
PROVIDERS = {
    "gemini": {
        "name": "Google Gemini",
        "default_model": "gemini-2.5-flash",
        "env_var": "GEMINI_API_KEY",
        "class": "browser_use.llm.google.chat.ChatGoogle",
    },
    "groq": {
        "name": "Groq",
        "default_model": "llama-3.3-70b-versatile",
        "env_var": "GROQ_API_KEY",
        "class": "browser_use.llm.groq.chat.ChatGroq",
    },
    "mistral": {
        "name": "Mistral",
        "default_model": "mistral-large-latest",
        "env_var": "MISTRAL_API_KEY",
        "class": "browser_use.llm.mistral.chat.ChatMistral",
    },
    "openai": {
        "name": "OpenAI",
        "default_model": "gpt-4o",
        "env_var": "OPENAI_API_KEY",
        "class": "browser_use.llm.openai.chat.ChatOpenAI",
    },
    "openai-compatible": {
        "name": "OpenAI-Compatible (newapi, freellmapi, mimo, etc.)",
        "default_model": "gpt-4o",
        "env_var": "OPENAI_COMPAT_API_KEY",
        "base_url_env": "OPENAI_COMPAT_BASE_URL",
        "class": "browser_use.llm.openai.chat.ChatOpenAI",
    },
    "cerebras": {
        "name": "Cerebras",
        "default_model": "llama-3.3-70b",
        "env_var": "CEREBRAS_API_KEY",
        "class": "browser_use.llm.openai.chat.ChatOpenAI",
        "base_url": "https://api.cerebras.ai/v1",
    },
    "opencode": {
        "name": "OpenCode (free tier — many models)",
        "default_model": "deepseek-v4-flash-free",
        "env_var": "OPENCODE_API_KEY",
        "class": "browser_use.llm.openai.chat.ChatOpenAI",
        "base_url": "https://opencode.ai/zen/v1",
    },
    "freemodel": {
        "name": "FreeModel.dev (Claude + GPT proxy)",
        "default_model": "claude-haiku-4-5-20251001",
        "env_var": "FREEMODEL_API_KEY",
        "class": "browser_use.llm.openai.chat.ChatOpenAI",
        "base_url": "https://api.freemodel.dev/v1",
    },
    "cloudflare": {
        "name": "Cloudflare Workers AI",
        "default_model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        "env_var": "CLOUDFLARE_API_TOKEN",
        "class": "browser_use.llm.openai.chat.ChatOpenAI",
        "base_url_env": "CLOUDFLARE_AI_URL",  # https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/v1
    },
}


def _import_llm_class(dotted_path: str) -> type:
    """Dynamically import an LLM class from a dotted path."""
    parts = dotted_path.split(".")
    module_path = ".".join(parts[:-1])
    class_name = parts[-1]
    module = __import__(module_path, fromlist=[class_name])
    return getattr(module, class_name)


# Regex to strip <think>...</think> tags (MiniMax, DeepSeek, QwQ, etc.)
_THINK_TAG_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def _strip_thinking_tags(text: str) -> str:
    """Remove <think>...</think> blocks from model output."""
    return _THINK_TAG_RE.sub("", text).strip()


class _ThinkingTagStripper:
    """Wraps a ChatOpenAI-compatible LLM to strip <think> tags before JSON parsing.

    Many open-weight / proxy models (MiniMax-M3, DeepSeek-R1, QwQ, etc.) emit
    reasoning in <think>...</think> blocks even when structured output is requested.
    browser-use expects pure JSON — this wrapper catches the parse failure,
    strips the tags, and retries the parse.
    """

    def __init__(self, inner: Any):
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        """Delegate all attribute access to the inner LLM (model_name, provider, etc.)."""
        return getattr(self._inner, name)

    async def ainvoke(self, messages: Any, output_format: Any = None, **kwargs: Any) -> Any:
        from browser_use.llm.openai.chat import ChatInvokeCompletion  # noqa: PLC0415

        # Try the inner model first (may succeed if model cooperates)
        try:
            return await self._inner.ainvoke(messages, output_format=output_format, **kwargs)
        except Exception as exc:
            error_msg = str(exc)
            # Only intercept JSON parse errors from thinking tags
            if "json_invalid" not in error_msg and "Invalid JSON" not in error_msg:
                raise
            logger.warning("Model returned thinking tags, stripping and retrying: %s", error_msg[:200])

        # Fallback: call raw API, strip thinking tags, parse manually
        from browser_use.llm.openai.serializer import OpenAIMessageSerializer  # noqa: PLC0415

        openai_messages = OpenAIMessageSerializer.serialize_messages(messages)
        client = self._inner.get_client()

        model_params: dict[str, Any] = {}
        if self._inner.temperature is not None:
            model_params["temperature"] = self._inner.temperature
        if self._inner.max_completion_tokens is not None:
            model_params["max_completion_tokens"] = self._inner.max_completion_tokens

        response = await client.chat.completions.create(
            model=str(self._inner.model),
            messages=openai_messages,
            **model_params,
        )
        choice = response.choices[0]
        raw_content = choice.message.content or ""

        # Strip thinking tags
        cleaned = _strip_thinking_tags(raw_content)
        logger.debug("Stripped thinking tags: %r → %r", raw_content[:200], cleaned[:200])

        if output_format is not None:
            parsed = output_format.model_validate_json(cleaned)
            return ChatInvokeCompletion(
                completion=parsed,
                usage=None,
                stop_reason=choice.finish_reason,
            )
        return ChatInvokeCompletion(
            completion=cleaned,
            usage=None,
            stop_reason=choice.finish_reason,
        )


def get_llm(
    provider: str = "gemini",
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    temperature: float = 0.1,
) -> Any:
    """Create an LLM instance compatible with browser_use Agent.

    For 'openai-compatible' providers, wraps the LLM to strip <think> tags
    that models like MiniMax-M3, DeepSeek, and QwQ emit.

    Args:
        provider: Provider name (gemini, groq, mistral, openai, openai-compatible, cerebras)
        model: Model name (uses provider default if None)
        api_key: API key (uses env var if None)
        base_url: Custom base URL (for openai-compatible providers)
        temperature: LLM temperature (lower = more deterministic)

    Returns:
        A browser_use LLM instance with a .provider attribute.
    """
    provider = provider.lower().strip()
    config = PROVIDERS.get(provider)
    if not config:
        raise ValueError(f"Unknown provider: {provider}. Available: {list(PROVIDERS.keys())}")

    model = model or config["default_model"]
    api_key = api_key or os.environ.get(config["env_var"], "")

    llm_class = _import_llm_class(config["class"])
    kwargs: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
        "temperature": temperature,
    }

    # Resolve base URL: explicit arg > provider config > env var
    if not base_url:
        if "base_url" in config:
            base_url = config["base_url"]
        elif "base_url_env" in config:
            base_url = os.environ.get(config["base_url_env"], "")
    if base_url:
        kwargs["base_url"] = base_url

    llm = llm_class(**kwargs)  # type: ignore[call-arg]

    # Wrap openai-compatible providers to handle thinking tags
    # (MiniMax-M3, DeepSeek, QwQ etc. emit <think> blocks that break JSON parsing)
    _openai_compat = ("openai-compatible", "groq", "mistral", "cerebras", "opencode", "freemodel", "cloudflare")
    if provider in _openai_compat:
        llm = _ThinkingTagStripper(llm)
        logger.info("Wrapped %s LLM with thinking-tag stripper", provider)

    return llm
