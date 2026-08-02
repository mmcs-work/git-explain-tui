from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from git_explain_tui.llm_client import LLMClient


def _response(text: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def test_default_model_is_cheap() -> None:
    client = LLMClient(api_key="test", completion=lambda **_: _response("ok"))

    assert client.model == "gpt-5-nano"
    assert client.provider_model == "openai/gpt-5-nano"
    assert client.max_output_tokens == 600
    assert client.max_context_chars == 40000


def test_renamed_settings_prefer_new_names_and_accept_existing_ones() -> None:
    with patch.dict("os.environ", {"GIT_EXPLAIN_MODEL": "ollama/legacy"}, clear=True):
        legacy_client = LLMClient(completion=lambda **_: _response("ok"))

    with patch.dict(
        "os.environ",
        {
            "GIT_EXPLAIN_MODEL": "ollama/legacy",
            "GIT_EXPLAIN_TUI_MODEL": "ollama/current",
        },
        clear=True,
    ):
        client = LLMClient(completion=lambda **_: _response("ok"))

    assert legacy_client.model == "ollama/legacy"
    assert client.model == "ollama/current"


def test_first_message_sends_context() -> None:
    captured = {}

    def completion(**kwargs):
        captured.update(kwargs)
        return _response("The answer")

    client = LLMClient(api_key="test", completion=completion)
    answer = client.ask("Why?", commit_context="the patch")

    assert answer == "The answer"
    assert captured["model"] == "openai/gpt-5-nano"
    assert "the patch" in captured["messages"][1]["content"]
    assert captured["messages"][-1] == {"role": "user", "content": "Why?"}
    assert captured["max_tokens"] == 600


def test_follow_up_resends_context_and_saved_history() -> None:
    captured = {}

    def completion(**kwargs):
        captured.update(kwargs)
        return _response("Follow-up")

    client = LLMClient(api_key="test", completion=completion)
    answer = client.ask(
        "And tests?",
        commit_context="the patch",
        history=[("you", "Why?"), ("assistant", "The answer")],
    )

    assert answer == "Follow-up"
    assert captured["messages"][2:] == [
        {"role": "user", "content": "Why?"},
        {"role": "assistant", "content": "The answer"},
        {"role": "user", "content": "And tests?"},
    ]


def test_provider_prefix_is_preserved() -> None:
    client = LLMClient(
        api_key="test", model="anthropic/claude-sonnet-4-5", completion=lambda **_: _response("ok")
    )

    assert client.provider_model == "anthropic/claude-sonnet-4-5"


def test_api_key_hint_names_the_selected_provider() -> None:
    client = LLMClient(model="anthropic/claude-sonnet-4-5", completion=lambda **_: _response("ok"))

    assert client.has_api_key is False
    assert client.api_key_hint == "Set ANTHROPIC_API_KEY to use Chat."


def test_local_ollama_does_not_require_an_api_key() -> None:
    client = LLMClient(model="ollama/gemma3", completion=lambda **_: _response("ok"))

    assert client.has_api_key is True
