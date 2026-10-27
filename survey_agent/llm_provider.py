"""LLM provider factory — uses browser_use LLM wrappers for Agent compatibility."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# Load project-local secrets first, without overriding existing env vars.
# This keeps keys scoped to this repo instead of polluting the user's shell.
_DOTENV_PATH = Path(__file__).resolve().parent.parent / "data" / ".env"
if _DOTENV_PATH.exists():
    try:
        import dotenv  # noqa: PLC0415

        dotenv.load_dotenv(_DOTENV_PATH, override=False)
    except ImportError:
        pass


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
        "default_model": "qwen/qwen3.6-27b",
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
        "default_model": "gpt-oss-120b",
        "env_var": "CEREBRAS_API_KEY",
        "class": "browser_use.llm.openai.chat.ChatOpenAI",
        "base_url": "https://api.cerebras.ai/v1",
    },
    "opencode": {
        "name": "OpenCode (free tier — many models)",
        "default_model": "hy3-free",
        "env_var": "OPENCODE_API_KEY",
        "class": "browser_use.llm.openai.chat.ChatOpenAI",
        "base_url": "https://opencode.ai/zen/v1",
    },
    "opencode-go": {
        "name": "OpenCode Go (paid tier — reliable access)",
        "default_model": "qwen3.8-flash",
        "env_var": "OPENCODE_GO_API_KEY",
        "class": "browser_use.llm.openai.chat.ChatOpenAI",
        "base_url": "https://opencode.ai/zen/go/v1/chat/completions",
        "default_headers": {"User-Agent": "OpenCode-Go/1.0"},
    },
    "openrouter": {
        "name": "OpenRouter (free tier)",
        "default_model": "minimax/minimax-m3:free",
        "env_var": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        "class": "browser_use.llm.openai.chat.ChatOpenAI",
    },
    "kilocode": {
        "name": "Kilocode (free tier)",
        "default_model": "kilo-auto/free",
        "env_var": "KILOCODE_API_KEY",
        "class": "browser_use.llm.openai.chat.ChatOpenAI",
        "base_url": "https://api.kilocode.ai/v1",
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
    "lmstudio": {
        "name": "LM Studio (local OpenAI-compatible)",
        "default_model": "lfm2.5-vl-450m-mlx",
        "env_var": "LMSTUDIO_API_KEY",
        "class": "browser_use.llm.openai.chat.ChatOpenAI",
        "base_url": "http://localhost:1234/v1",
    },
}


def _import_llm_class(dotted_path: str) -> type:
    """Dynamically import an LLM class from a dotted path."""
    parts = dotted_path.split(".")
    module_path = ".".join(parts[:-1])
    class_name = parts[-1]
    module = __import__(module_path, fromlist=[class_name])
    return getattr(module, class_name)


# Regex to strip <think>...</think> blocks (MiniMax, DeepSeek, QwQ, etc.)
_THINK_TAG_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

# Regex to strip inline thinking="..." or thinking='...' wrappers
_INLINE_THINK_RE = re.compile(r'\s*thinking=["\'][^"\']*["\']?\s*', re.DOTALL)


def _strip_thinking_tags(text: str) -> str:
    """Remove <think>...</think> blocks and inline thinking wrappers from model output."""
    text = _THINK_TAG_RE.sub("", text)
    text = _INLINE_THINK_RE.sub("", text)
    return text.strip()


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

        result = await self._inner.ainvoke(messages, output_format=output_format, **kwargs)
        raw = getattr(result, "completion", result)
        text = raw if isinstance(raw, str) else getattr(raw, "content", str(raw))
        cleaned = _strip_thinking_tags(text)
        if cleaned != text:
            logger.debug("Stripped thinking tags from output")
        if output_format is not None:
            parsed = output_format.model_validate_json(cleaned)
            return ChatInvokeCompletion(
                completion=parsed,
                usage=getattr(result, "usage", None),
                stop_reason=getattr(result, "stop_reason", None),
            )
        return ChatInvokeCompletion(
            completion=cleaned,
            usage=getattr(result, "usage", None),
            stop_reason=getattr(result, "stop_reason", None),
        )


class _GoChat:
    """Minimal OpenCode Go wrapper that bypasses browser-use's ChatOpenAI.

    browser-use's ChatOpenAI wrapper triggers Cloudflare blocking against
    the Go endpoint. This thin wrapper talks to OpenCode Go directly
    through the raw openai SDK while still satisfying BaseChatModel.
    """

    def __init__(self, model: str, api_key: str) -> None:
        self._model = model
        self._api_key = api_key
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://opencode.ai/zen/go/v1",
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "opencode-go"

    @property
    def name(self) -> str:
        return self._model

    @property
    def model_name(self) -> str:
        return self._model

    async def ainvoke(
        self,
        messages: list[Any],
        output_format: Any = None,
        **kwargs: Any,
    ) -> Any:
        from browser_use.llm.openai.chat import ChatInvokeCompletion  # noqa: PLC0415

        openai_messages = []
        for m in messages:
            role = getattr(m, "role", "user")
            content = getattr(m, "content", "")
            if isinstance(content, list):
                text = " ".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
            else:
                text = str(content)
            openai_messages.append({"role": role, "content": text})

        response = await self._client.chat.completions.create(
            model=self._model,
            messages=openai_messages,
        )
        choice = response.choices[0]
        text = choice.message.content or ""

        if output_format is not None:
            parsed = output_format.model_validate_json(text)
            return ChatInvokeCompletion(
                completion=parsed,
                usage=None,
                stop_reason=choice.finish_reason,
            )
        return ChatInvokeCompletion(
            completion=text,
            usage=None,
            stop_reason=choice.finish_reason,
        )

    def get_client(self) -> AsyncOpenAI:
        return self._client


class _OpenRouterChat:
    """Minimal OpenAI-compatible wrapper for OpenRouter free models.

    OpenRouter free models require HTTP-Referer and X-Title headers,
    and often reject strict JSON schema enforcement, so this wrapper
    bypasses both browser-use's ChatOpenAI wrapper and schema forcing.
    """

    def __init__(self, model: str, api_key: str) -> None:
        self._model = model
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            default_headers={
                "HTTP-Referer": "https://github.com/browser-use/browser-use",
                "X-Title": "Survey Agent",
            },
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "openrouter"

    @property
    def name(self) -> str:
        return self._model

    @property
    def model_name(self) -> str:
        return self._model

    async def ainvoke(
        self,
        messages: list[Any],
        output_format: Any = None,
        **kwargs: Any,
    ) -> Any:
        openai_messages = []
        for msg in messages:
            role = getattr(msg, "role", "user")
            content = getattr(msg, "content", "")
            if isinstance(content, list):
                text = " ".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
            else:
                text = str(content)
            openai_messages.append({"role": role, "content": text})

        response = await self._client.chat.completions.create(
            model=self._model,
            messages=openai_messages,
        )
        choice = response.choices[0] if response.choices else None
        if choice is None:
            raise RuntimeError("OpenRouter returned empty choices")
        text = choice.message.content or ""

        if output_format is not None:
            from browser_use.llm.openai.chat import ChatInvokeCompletion  # noqa: PLC0415

            parsed = output_format.model_validate_json(text)
            return ChatInvokeCompletion(
                completion=parsed,
                usage=None,
                stop_reason=choice.finish_reason,
            )
        from browser_use.llm.openai.chat import ChatInvokeCompletion  # noqa: PLC0415

        return ChatInvokeCompletion(
            completion=text,
            usage=None,
            stop_reason=choice.finish_reason,
        )

    def get_client(self) -> AsyncOpenAI:
        return self._client


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
    api_key = api_key or os.environ.get(config.get("env_var", ""), "")

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

    llm_kwargs = dict(kwargs)

    # Provider-specific compatibility tweaks
    if provider == "opencode-go":
        # Use direct SDK wrapper to bypass browser-use's Cloudflare-blocked wrapper
        opencode_go_key = api_key or os.environ.get("OPENCODE_GO_API_KEY", "")
        llm = _GoChat(model=model, api_key=opencode_go_key)
    elif provider == "opencode":
        # Free-tier OpenCode models often do not support the strict structured-output
        # schema browser-use normally forces; disable that enforcement here.
        if "dont_force_structured_output" not in llm_kwargs:
            llm_kwargs["dont_force_structured_output"] = True
        llm = llm_class(**llm_kwargs)  # type: ignore[call-arg]
    elif provider == "openrouter":
        # OpenRouter free models require HTTP-Referer/X-Title headers
        # and often fail strict JSON schema enforcement, so bypass both.
        if "dont_force_structured_output" not in llm_kwargs:
            llm_kwargs["dont_force_structured_output"] = True
        openrouter_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        llm = _OpenRouterChat(model=model, api_key=openrouter_key)
    else:
        llm = llm_class(**llm_kwargs)  # type: ignore[call-arg]

    # Wrap openai-compatible providers to handle thinking tags
    # (MiniMax-M3, DeepSeek, QwQ etc. emit <think> blocks that break JSON parsing)
    _openai_compat = (
        "openai-compatible",
        "groq",
        "mistral",
        "cerebras",
        "opencode",
        "opencode-go",
        "kilocode",
        "freemodel",
        "cloudflare",
    )
    if provider in _openai_compat:
        llm = _ThinkingTagStripper(llm)
        logger.info("Wrapped %s LLM with thinking-tag stripper", provider)

    return llm
