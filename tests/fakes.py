import json
from typing import Any

import pytest

from pgsage import cli


class FakeClient:
    def __init__(self, reply: str | list[str]) -> None:
        self.replies = [reply] if isinstance(reply, str) else list(reply)
        self.prompts: list[str] = []

    @property
    def reply(self) -> str:
        return self.replies[0]

    @reply.setter
    def reply(self, value: str) -> None:
        self.replies = [value]

    def complete(self, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        index = min(len(self.prompts) - 1, len(self.replies) - 1)
        return self.replies[index]


def use_client(monkeypatch: pytest.MonkeyPatch, client: FakeClient) -> None:
    def make_client(
        model: str | None,
        provider: str = "anthropic",
        base_url: str | None = None,
        effort: str | None = "high",
    ) -> FakeClient:
        return client

    monkeypatch.setattr(cli, "make_client", make_client)


def parse_prompt(prompt: str) -> dict[str, Any]:
    start = prompt.index("UNTRUSTED INPUT BEGINS")
    end = prompt.index("UNTRUSTED INPUT ENDS")
    body = prompt[prompt.index("\n", start) + 1 : end]
    decoder = json.JSONDecoder()
    merged: dict[str, Any] = {}
    position = 0
    while position < len(body):
        document, offset = decoder.raw_decode(body, position)
        merged.update(document)
        position = offset
        while position < len(body) and body[position].isspace():
            position += 1
    return merged
