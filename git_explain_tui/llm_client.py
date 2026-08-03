from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from threading import Lock
from typing import Any


class ChatError(RuntimeError):
    """Raised when a model request cannot be completed."""


SYSTEM_PROMPT = """You are an expert code-review companion inside a Git commit browser.
Answer questions about the selected commit using the supplied commit metadata and patch.
Be precise, cite file paths and relevant changed lines when possible, and distinguish facts
visible in the patch from reasonable inferences. Keep answers concise unless asked for detail."""


Completion = Callable[..., Any]
PROVIDER_API_KEYS = {
    # These are hints for the UI, not a replacement for LiteLLM's provider support.
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "google": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
LOCAL_PROVIDERS = {"ollama"}


def _setting(name: str, legacy_name: str, default: str = "") -> str:
    """Prefer the public name while keeping existing local configuration usable."""
    return os.environ.get(name) or os.environ.get(legacy_name, default)


class LLMClient:
    """Provider-neutral boundary around LiteLLM's chat-completions API."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        completion: Completion | None = None,
    ):
        # Constructor arguments make tests independent from real credentials and networks.
        self.api_key = api_key
        self.api_base = _setting(
            "GIT_EXPLAIN_TUI_API_BASE", "GIT_EXPLAIN_API_BASE"
        ) or os.environ.get("OPENAI_BASE_URL")
        self.model = model or _setting(
            "GIT_EXPLAIN_TUI_MODEL", "GIT_EXPLAIN_MODEL", "gpt-5-nano"
        )
        self.max_output_tokens = int(
            _setting("GIT_EXPLAIN_TUI_MAX_OUTPUT_TOKENS", "GIT_EXPLAIN_MAX_OUTPUT_TOKENS", "600")
        )
        self.max_context_chars = int(
            _setting("GIT_EXPLAIN_TUI_CONTEXT_CHARS", "GIT_EXPLAIN_CONTEXT_CHARS", "40000")
        )
        # LiteLLM has a large import cost. Keep browsing instant and load it only
        # when the user actually sends their first chat request.
        self._completion = completion
        self._completion_lock = Lock()

    def _get_completion(self) -> Completion:
        """Load LiteLLM once, safely if future UI work calls Chat concurrently."""
        if self._completion is None:
            with self._completion_lock:
                if self._completion is None:
                    from litellm import completion as litellm_completion

                    self._completion = litellm_completion
        return self._completion

    @property
    def provider_model(self) -> str:
        # Bare names remain an OpenAI convenience; other providers use provider/model.
        return self.model if "/" in self.model else f"openai/{self.model}"

    @property
    def has_api_key(self) -> bool:
        """Whether the selected provider has an apparent credential source."""
        provider = self.provider_model.split("/", 1)[0]
        provider_key = PROVIDER_API_KEYS.get(provider)
        return bool(
            self.api_key
            or self.api_base  # A configured compatible/local endpoint may not require a key.
            or provider in LOCAL_PROVIDERS
            or (provider_key and os.environ.get(provider_key))
        )

    @property
    def api_key_hint(self) -> str:
        """Return an actionable setup message for the currently selected provider."""
        provider = self.provider_model.split("/", 1)[0]
        provider_key = PROVIDER_API_KEYS.get(provider)
        if provider in LOCAL_PROVIDERS:
            return "Start Ollama and pull the selected model to use Chat."
        if provider_key:
            return f"Set {provider_key} to use Chat."
        return "Set the selected provider's API key, or configure a local Ollama model."

    def ask(
        self,
        question: str,
        *,
        commit_context: str,
        history: Sequence[tuple[str, str]] = (),
    ) -> str:
        if not question.strip():
            raise ChatError("Question cannot be empty.")
        if not commit_context:
            raise ChatError("Commit context is required for chat.")

        messages: list[dict[str, str]] = [
            # Keep Git context separate from the actual question so the model can cite it.
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Here is the selected Git context for this conversation.\n\n"
                    f"<commit>\n{commit_context}\n</commit>"
                ),
            },
        ]
        messages.extend(
            # Only user/assistant turns are provider-neutral chat history; errors are local UI data.
            {"role": "user" if role == "you" else "assistant", "content": text}
            for role, text in history
            if role in {"you", "assistant"}
        )
        messages.append({"role": "user", "content": question})

        try:
            request: dict[str, Any] = {
                "model": self.provider_model,
                "messages": messages,
                "max_tokens": self.max_output_tokens,
            }
            if self.api_key:
                # Normally credentials come from provider environment variables; this is for tests.
                request["api_key"] = self.api_key
            if self.api_base:
                request["api_base"] = self.api_base
            response = self._get_completion()(**request)
            text = self._response_text(response)
        except Exception as exc:
            raise ChatError(f"LLM request failed: {exc}") from exc
        if not text:
            raise ChatError("The model returned an empty response.")
        return text.strip()

    @staticmethod
    def _response_text(response: Any) -> str:
        """Extract the common Chat Completions text shape and reject unfamiliar responses clearly."""
        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, KeyError, TypeError) as exc:
            raise ChatError("The model returned an unreadable response.") from exc
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        return ""
