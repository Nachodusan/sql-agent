"""Shared fixtures.

The agent tests use a scripted fake model, so the whole suite runs offline,
deterministically and for free. Testing an agent against a live model tests the
model; this tests the loop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from sql_agent import db


@pytest.fixture(scope="session")
def shop_db(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("data") / "shop.db"
    db.build(path)
    return str(path)


# --- a fake OpenAI-compatible message ------------------------------------

@dataclass
class FakeFunction:
    name: str
    arguments: str


@dataclass
class FakeToolCall:
    id: str
    function: FakeFunction
    type: str = "function"


@dataclass
class FakeMessage:
    content: str | None = None
    tool_calls: list[FakeToolCall] | None = None


@dataclass
class FakeUsage:
    prompt_tokens: int = 120
    completion_tokens: int = 40


@dataclass
class FakeReply:
    message: FakeMessage
    usage: FakeUsage
    latency_ms: int = 7


class ScriptedLLM:
    """Replays a fixed list of turns.

    Each turn is either a list of (tool_name, arguments) pairs, or a string,
    which becomes a plain assistant answer with no tool call.
    """

    model = "scripted/test-model"

    def __init__(self, turns: list[Any]):
        self.turns = list(turns)
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages, tools):  # noqa: ARG002 - signature mirrors LLM.chat
        self.calls.append(list(messages))
        if not self.turns:
            return FakeReply(FakeMessage(content="ran out of script"), FakeUsage())
        turn = self.turns.pop(0)
        if isinstance(turn, str):
            return FakeReply(FakeMessage(content=turn), FakeUsage())
        calls = [
            FakeToolCall(id=f"call_{i}", function=FakeFunction(name=name,
                                                               arguments=json.dumps(args)))
            for i, (name, args) in enumerate(turn)
        ]
        return FakeReply(FakeMessage(content=None, tool_calls=calls), FakeUsage())
