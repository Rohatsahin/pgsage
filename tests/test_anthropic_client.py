import json
from types import SimpleNamespace
from typing import Any

import pytest

from pgsage import parse_explain
from pgsage.ai import (
    SYSTEM_PROMPT,
    AnthropicClient,
    ExplanationError,
    Redaction,
    build_prompt,
    explain_plan,
)
from pgsage.schema import EXPLANATION_SCHEMA

PLAN = [
    {
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "orders",
            "Startup Cost": 0.0,
            "Total Cost": 10.0,
            "Plan Rows": 100,
            "Actual Rows": 100,
            "Actual Loops": 1,
            "Actual Total Time": 5.0,
        },
        "Execution Time": 5.0,
    }
]

ANSWER = json.dumps(
    {
        "summary": "s",
        "root_cause": "c",
        "evidence": [],
        "actions": [],
    }
)


class FakeMessages:
    def __init__(self, owner: "FakeSDK", beta: bool) -> None:
        self.owner = owner
        self.beta = beta

    def create(self, **kwargs: Any) -> Any:
        self.owner.calls.append({"beta": self.beta, **kwargs})
        return self.owner.reply


class FakeSDK:
    def __init__(self, reply: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reply = reply or response()
        self.messages = FakeMessages(self, beta=False)
        self.beta = SimpleNamespace(messages=FakeMessages(self, beta=True))


def response(text: str = ANSWER, stop_reason: str = "end_turn", **usage: int) -> Any:
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(category="cyber") if stop_reason == "refusal" else None,
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        usage=SimpleNamespace(
            input_tokens=usage.get("input_tokens", 100),
            cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
            cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
            output_tokens=usage.get("output_tokens", 50),
        ),
    )


def prompt() -> str:
    return build_prompt(parse_explain(PLAN), "SELECT 1")


def test_default_model_is_opus_with_high_effort_fallbacks_and_structured_output() -> None:
    sdk = FakeSDK()
    AnthropicClient(client=sdk).complete(SYSTEM_PROMPT, prompt())
    (call,) = sdk.calls
    assert call["beta"] is True
    assert call["model"] == "claude-opus-5-5"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["fallbacks"] == "default"
    assert call["output_config"]["effort"] == "high"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": EXPLANATION_SCHEMA}


def test_no_sampling_parameters_or_forced_tool_choice_are_ever_sent() -> None:
    sdk = FakeSDK()
    AnthropicClient(client=sdk).complete(SYSTEM_PROMPT, prompt())
    (call,) = sdk.calls
    for forbidden in ("temperature", "top_p", "top_k", "tool_choice", "thinking", "tools"):
        assert forbidden not in call


def test_system_prompt_is_a_cached_block_identical_to_the_markdown() -> None:
    sdk = FakeSDK()
    AnthropicClient(client=sdk).complete(SYSTEM_PROMPT, prompt())
    (call,) = sdk.calls
    assert call["system"] == [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
    ]


def test_user_message_caches_the_stable_part_and_leaves_attempts_uncached() -> None:
    sdk = FakeSDK()
    built = build_prompt(
        parse_explain(PLAN),
        "SELECT 1",
        attempts=[{"kind": "index", "statement": "x", "proven": False}],
    )
    AnthropicClient(client=sdk).complete(SYSTEM_PROMPT, built)
    stable, volatile = sdk.calls[0]["messages"][0]["content"]
    assert stable["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in volatile
    assert stable["text"] + volatile["text"] == str(built)
    assert "previous_attempts" in volatile["text"]
    assert "previous_attempts" not in stable["text"]


def test_second_round_reuses_the_exact_cached_prefix() -> None:
    sdk = FakeSDK()
    client = AnthropicClient(client=sdk)
    redaction = Redaction()
    result = parse_explain(PLAN)
    first = build_prompt(result, "SELECT 1", redaction=redaction)
    second = build_prompt(
        result, "SELECT 1", attempts=[{"kind": "rewrite", "statement": "y"}], redaction=redaction
    )
    client.complete(SYSTEM_PROMPT, first)
    client.complete(SYSTEM_PROMPT, second)
    first_blocks = sdk.calls[0]["messages"][0]["content"]
    second_blocks = sdk.calls[1]["messages"][0]["content"]
    assert first_blocks[0] == second_blocks[0]
    assert sdk.calls[0]["system"] == sdk.calls[1]["system"]


def test_plain_string_prompts_still_work() -> None:
    sdk = FakeSDK()
    AnthropicClient(client=sdk).complete("system", "plain text")
    assert sdk.calls[0]["messages"] == [{"role": "user", "content": "plain text"}]


def test_models_without_fallback_support_use_the_stable_endpoint() -> None:
    sdk = FakeSDK()
    AnthropicClient(model="claude-haiku-4-5", client=sdk).complete(SYSTEM_PROMPT, prompt())
    (call,) = sdk.calls
    assert call["beta"] is False
    assert "fallbacks" not in call and "betas" not in call
    assert "effort" not in call["output_config"]


def test_fallbacks_can_be_turned_off() -> None:
    sdk = FakeSDK()
    AnthropicClient(client=sdk, use_fallbacks=False).complete(SYSTEM_PROMPT, prompt())
    assert sdk.calls[0]["beta"] is False
    assert "fallbacks" not in sdk.calls[0]


def test_effort_is_configurable_and_skipped_for_unknown_models() -> None:
    sdk = FakeSDK()
    AnthropicClient(client=sdk, effort="xhigh").complete("s", prompt())
    assert sdk.calls[0]["output_config"]["effort"] == "xhigh"
    other = FakeSDK()
    AnthropicClient(model="some-other-model", client=other, effort="high").complete("s", prompt())
    assert "effort" not in other.calls[0]["output_config"]


def test_only_text_blocks_are_returned() -> None:
    sdk = FakeSDK()
    assert AnthropicClient(client=sdk).complete("s", prompt()) == ANSWER


def test_refusal_is_reported_with_its_category_not_parsed_as_an_answer() -> None:
    sdk = FakeSDK(response(text="", stop_reason="refusal"))
    with pytest.raises(ExplanationError, match="declined.*cyber"):
        AnthropicClient(client=sdk).complete("s", prompt())


def test_truncated_output_is_an_error_not_silent_garbage() -> None:
    sdk = FakeSDK(response(text='{"summary": "x', stop_reason="max_tokens"))
    with pytest.raises(ExplanationError, match="cut off"):
        AnthropicClient(client=sdk).complete("s", prompt())


def test_cache_usage_is_recorded_per_call() -> None:
    sdk = FakeSDK(response(cache_read_input_tokens=3000, cache_creation_input_tokens=10))
    client = AnthropicClient(client=sdk)
    client.complete("s", prompt())
    assert client.usage_log == [
        {
            "input_tokens": 100,
            "cache_creation_input_tokens": 10,
            "cache_read_input_tokens": 3000,
            "output_tokens": 50,
        }
    ]


def test_end_to_end_through_explain_plan() -> None:
    sdk = FakeSDK(
        response(
            text=json.dumps(
                {"summary": "fine", "root_cause": "none", "evidence": [], "actions": []}
            )
        )
    )
    explanation = explain_plan(AnthropicClient(client=sdk), parse_explain(PLAN), "SELECT 1")
    assert explanation.summary == "fine"
    assert explanation.actions == []
