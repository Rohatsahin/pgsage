import json

import httpx
import pytest

from pgsage.ai import (
    ExplanationError,
    OpenAICompatibleClient,
    Redaction,
)


def test_redaction_round_trips_literals() -> None:
    redaction = Redaction()
    redacted = redaction.apply("SELECT * FROM t WHERE email = 'a@b.com' AND n > 5 LIMIT 10")
    assert "a@b.com" not in redacted
    assert "5" not in redacted.replace("__L", "")
    assert (
        redaction.restore(redacted) == "SELECT * FROM t WHERE email = 'a@b.com' AND n > 5 LIMIT 10"
    )


def test_redaction_leaves_identifiers_with_digits() -> None:
    redaction = Redaction()
    assert redaction.apply('SELECT col1, "t2".x FROM t2') == 'SELECT col1, "t2".x FROM t2'


def test_restore_keeps_unknown_tokens() -> None:
    assert Redaction().restore("WHERE a = __L9__") == "WHERE a = __L9__"


def reply_transport(status: int, body: dict[str, object]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload["model"] == "llama3"
        assert payload["messages"][0]["role"] == "system"
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


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
