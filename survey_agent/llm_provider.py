"""LLM provider factory — supports multiple providers without hardcoding."""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


# Provider configurations
PROVIDERS = {
    "gemini": {
        "name": "Google Gemini",
        "default_model": "gemini-2.5-flash",
        "env_var": "GEMINI_API_KEY",
        "requires": ["langchain-google-genai"],
    },
    "groq": {
        "name": "Groq",
        "default_model": "llama-3.3-70b-versatile",
        "env_var": "GROQ_API_KEY",
        "requires": ["langchain-groq"],
    },
    "mistral": {
        "name": "Mistral",
        "default_model": "mistral-large-latest",
        "env_var": "MISTRAL_API_KEY",
        "requires": ["langchain-mistralai"],
    },
    "openai": {
        "name": "OpenAI",
        "default_model": "gpt-4o",
        "env_var": "OPENAI_API_KEY",
        "requires": ["langchain-openai"],
    },
    "openai-compatible": {
        "name": "OpenAI-Compatible (newapi, freellmapi, mimo, etc.)",
        "default_model": "gpt-4o",
        "env_var": "OPENAI_COMPAT_API_KEY",
        "base_url_env": "OPENAI_COMPAT_BASE_URL",
        "requires": ["langchain-openai"],
    },
    "cerebras": {
        "name": "Cerebras",
        "default_model": "llama-3.3-70b",
        "env_var": "CEREBRAS_API_KEY",
        "requires": ["langchain-openai"],
        "base_url": "https://api.cerebras.ai/v1",
    },
}


def get_llm(
    provider: str = "gemini",
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    temperature: float = 0.1,
) -> Any:
    """Create an LLM instance for the given provider.

    Args:
        provider: Provider name (gemini, groq, mistral, openai, openai-compatible, cerebras)
        model: Model name (uses provider default if None)
        api_key: API key (uses env var if None)
        base_url: Custom base URL (for openai-compatible providers)
        temperature: LLM temperature (lower = more deterministic)

    Returns:
        A langchain chat model instance.
    """
    provider = provider.lower().strip()
    config = PROVIDERS.get(provider)
    if not config:
        raise ValueError(f"Unknown provider: {provider}. Available: {list(PROVIDERS.keys())}")

    model = model or config["default_model"]
    api_key = api_key or os.environ.get(config["env_var"], "")

    if provider == "gemini":
        return _get_gemini(model, api_key, temperature)
    elif provider == "groq":
        return _get_groq(model, api_key, temperature)
    elif provider == "mistral":
        return _get_mistral(model, api_key, temperature)
    elif provider == "openai":
        return _get_openai(model, api_key, temperature)
    elif provider == "openai-compatible":
        base_url = base_url or os.environ.get(config.get("base_url_env", ""), "")
        return _get_openai_compatible(model, api_key, base_url, temperature)
    elif provider == "cerebras":
        base_url = base_url or config.get("base_url", "")
        return _get_openai_compatible(model, api_key, base_url, temperature)
    else:
        raise ValueError(f"Provider {provider} not implemented")


def _get_gemini(model: str, api_key: str, temperature: float) -> Any:
    from langchain_google_genai import ChatGoogleGenerativeAI  # noqa: PLC0415

    return ChatGoogleGenerativeAI(
        model=model,
        google_api_key=api_key,
        temperature=temperature,
    )


def _get_groq(model: str, api_key: str, temperature: float) -> Any:
    from langchain_groq import ChatGroq  # noqa: PLC0415

    return ChatGroq(
        model=model,
        groq_api_key=api_key,
        temperature=temperature,
    )


def _get_mistral(model: str, api_key: str, temperature: float) -> Any:
    from langchain_mistralai import ChatMistralAI  # noqa: PLC0415

    return ChatMistralAI(
        model=model,
        mistral_api_key=api_key,
        temperature=temperature,
    )


def _get_openai(model: str, api_key: str, temperature: float) -> Any:
    from langchain_openai import ChatOpenAI  # noqa: PLC0415

    return ChatOpenAI(
        model=model,
        api_key=api_key,
        temperature=temperature,
    )


def _get_openai_compatible(model: str, api_key: str, base_url: str, temperature: float) -> Any:
    from langchain_openai import ChatOpenAI  # noqa: PLC0415

    kwargs = {
        "model": model,
        "api_key": api_key,
        "temperature": temperature,
    }
    if base_url:
        kwargs["base_url"] = base_url
    return ChatOpenAI(**kwargs)
