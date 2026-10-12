import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from pgsage import parse_explain
from pgsage.ai import (
    SYSTEM_PROMPT,
    AnthropicClient,
    ExplanationError,
    OpenAICompatibleClient,
    Redaction,
    build_prompt,
    explain_plan,
    parse_explanation,
)
from pgsage.schema import EXPLANATION_SCHEMA
from tests.fakes import FakeClient

PLAN = [
    {
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "orders",
            "Filter": "((email = 'jane@example.com'::text) AND (customer_id = 7) AND (x > 1.5))",
            "Startup Cost": 0.0,
            "Total Cost": 10.0,
            "Plan Rows": 2000,
            "Actual Rows": 2000,
            "Actual Loops": 1,
            "Actual Total Time": 20.0,
            "Rows Removed by Filter": 198000,
            "Plans": [
                {
                    "Node Type": "Index Scan",
                    "Index Cond": "(id = 42)",
                    "Startup Cost": 0.0,
                    "Total Cost": 1.0,
                    "Plan Rows": 1,
                }
            ],
        },
        "Execution Time": 20.0,
    }
]


VALID = {
    "summary": "A sequential scan dominates.",
    "root_cause": "No index on customer_id.",
    "actions": [
        {
            "description": "Add an index",
            "sql": "CREATE INDEX ON orders (customer_id)",
            "index": {"table": "orders", "columns": ["customer_id"]},
        }
    ],
}


def reply_transport(status: int, body: dict[str, object]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload["model"] == "llama3"
        assert payload["messages"][0]["role"] == "system"
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


PLAN_ANTHROPIC_CLIENT = [
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
    return build_prompt(parse_explain(PLAN_ANTHROPIC_CLIENT), "SELECT 1")


def answer(actions: list[Any]) -> dict[str, Any]:
    return {"summary": "s", "root_cause": "c", "evidence": ["e"], "actions": actions}


GOOD: dict[str, Any] = {"description": "d", "rewrite": "SELECT 1"}


def reply(**action: Any) -> str:
    return json.dumps(
        {"summary": "s", "root_cause": "c", "actions": [{"description": "d", **action}]}
    )


class Sdk:
    def __init__(self, error: Exception | None = None, **response: Any) -> None:
        self.error = error
        self.response = SimpleNamespace(
            stop_reason=response.get("stop_reason", "end_turn"),
            stop_details=None,
            content=[SimpleNamespace(type="text", text="{}")],
            usage=None,
        )
        self.messages = SimpleNamespace(create=self.create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs: Any) -> Any:
        if self.error:
            raise self.error
        return self.response


def test_explain_plan_parses_json_reply() -> None:
    result = parse_explain(PLAN)
    client = FakeClient(json.dumps(VALID))
    explanation = explain_plan(client, result)
    assert explanation.actions[0].sql == "CREATE INDEX ON orders (customer_id)"
    assert explanation.actions[0].index is not None
    assert explanation.actions[0].index.columns == ["customer_id"]
    assert len(client.prompts) == 1


def test_openai_compatible_client_returns_message_content() -> None:
    transport = reply_transport(200, {"choices": [{"message": {"content": "hello"}}]})
    client = OpenAICompatibleClient("llama3", "http://llm.local/v1", transport=transport)
    assert client.complete("sys", "prompt") == "hello"


@pytest.mark.parametrize(
    ("status", "body"), [(500, {"error": "boom"}), (200, {"choices": []}), (200, {})]
)
def test_openai_compatible_client_wraps_failures(status: int, body: dict[str, object]) -> None:
    client = OpenAICompatibleClient(
        "llama3", "http://llm.local/v1", transport=reply_transport(status, body)
    )
    with pytest.raises(ExplanationError):
        client.complete("sys", "prompt")


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
        parse_explain(PLAN_ANTHROPIC_CLIENT),
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
    result = parse_explain(PLAN_ANTHROPIC_CLIENT)
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
    explanation = explain_plan(
        AnthropicClient(client=sdk), parse_explain(PLAN_ANTHROPIC_CLIENT), "SELECT 1"
    )
    assert explanation.summary == "fine"
    assert explanation.actions == []


def test_plain_json() -> None:
    assert parse_explanation(json.dumps(answer([GOOD]))).actions[0].rewrite == "SELECT 1"


def test_json_wrapped_in_fences_and_prose() -> None:
    wrapped = (
        f"Sure! Here is the analysis:\n```json\n{json.dumps(answer([GOOD]))}\n```\nHope it helps."
    )
    assert parse_explanation(wrapped).summary == "s"


def test_json_surrounded_by_prose_without_fences() -> None:
    assert parse_explanation(f"Analysis: {json.dumps(answer([]))} Done.").actions == []


@pytest.mark.parametrize(
    "text", ["", "I cannot help with that.", "[]", '"just a string"', "{broken"]
)
def test_garbage_is_an_explanation_error(text: str) -> None:
    with pytest.raises(ExplanationError):
        parse_explanation(text)


def test_missing_actions_is_an_error() -> None:
    with pytest.raises(ExplanationError):
        parse_explanation(json.dumps({"summary": "s", "root_cause": "c"}))


def test_missing_summary_is_an_error() -> None:
    with pytest.raises(ExplanationError):
        parse_explanation(json.dumps({"root_cause": "c", "actions": []}))


def test_one_bad_action_does_not_discard_the_good_ones() -> None:
    bad_kind: dict[str, Any] = {"description": "x", "index": {"table": "t", "columns": []}}
    explanation = parse_explanation(json.dumps(answer([bad_kind, GOOD])))
    assert [a.rewrite for a in explanation.actions] == ["SELECT 1"]
    assert len(explanation.rejected) == 1
    assert "action 1 rejected" in explanation.rejected[0]


def test_an_action_with_two_change_kinds_is_rejected() -> None:
    both = {
        "description": "x",
        "rewrite": "SELECT 1",
        "index": {"table": "t", "columns": ["a"]},
    }
    explanation = parse_explanation(json.dumps(answer([both])))
    assert explanation.actions == []
    assert "at most one" in explanation.rejected[0]


def test_more_than_three_actions_are_truncated_and_reported() -> None:
    explanation = parse_explanation(json.dumps(answer([GOOD] * 5)))
    assert len(explanation.actions) == 3
    assert any("more than 3" in line for line in explanation.rejected)


def test_explicit_nulls_as_required_by_the_schema_are_accepted() -> None:
    action = {
        "description": "d",
        "confidence": "high",
        "expected_effect": None,
        "index": {
            "table": "t",
            "columns": ["a"],
            "schema_name": None,
            "method": "btree",
            "include": [],
            "where": None,
        },
        "statistics": None,
        "rewrite": None,
        "sql": None,
    }
    explanation = parse_explanation(json.dumps(answer([action])))
    assert explanation.rejected == []
    assert explanation.actions[0].index is not None
    assert explanation.actions[0].confidence == "high"


def test_unknown_confidence_values_are_rejected_per_action() -> None:
    explanation = parse_explanation(json.dumps(answer([{**GOOD, "confidence": "certain"}])))
    assert explanation.actions == []
    assert explanation.rejected


def test_a_bad_statistics_action_is_rejected_without_losing_the_rest() -> None:
    explanation = parse_explanation(
        reply(statistics={"table": "t", "columns": ["a"], "kinds": ["mcv"]})
    )
    assert explanation.actions == []
    assert "action 1 rejected" in explanation.rejected[0]


def test_missing_credentials_get_an_actionable_message() -> None:
    error = TypeError('"Could not resolve authentication method. Expected api_key to be set."')
    with pytest.raises(ExplanationError, match="ANTHROPIC_API_KEY"):
        AnthropicClient(client=Sdk(error)).complete("s", "p")


def test_other_api_failures_are_wrapped_with_their_type() -> None:
    with pytest.raises(ExplanationError, match=r"LLM request failed \(RuntimeError\): overloaded"):
        AnthropicClient(client=Sdk(RuntimeError("overloaded"))).complete("s", "p")


@pytest.mark.parametrize("stop_reason", ["pause_turn", "tool_use", "something_new"])
def test_unexpected_stop_reasons_are_reported(stop_reason: str) -> None:
    with pytest.raises(ExplanationError, match="unexpected stop_reason"):
        AnthropicClient(client=Sdk(stop_reason=stop_reason)).complete("s", "p")


@pytest.mark.parametrize("stop_reason", ["end_turn", "stop_sequence"])
def test_normal_stop_reasons_pass(stop_reason: str) -> None:
    assert AnthropicClient(client=Sdk(stop_reason=stop_reason)).complete("s", "p") == "{}"


def test_an_unreachable_openai_compatible_endpoint_is_a_clean_error() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = OpenAICompatibleClient(
        "m", "http://127.0.0.1:1/v1", transport=httpx.MockTransport(refuse)
    )
    with pytest.raises(ExplanationError, match="LLM request failed"):
        client.complete("s", "p")


def test_an_openai_compatible_request_is_deterministic_and_carries_both_messages() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatibleClient("m", transport=httpx.MockTransport(handler))
    assert client.complete("sys", "user") == "ok"
    assert seen[0]["temperature"] == 0
    assert [m["role"] for m in seen[0]["messages"]] == ["system", "user"]
