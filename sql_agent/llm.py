"""Thin wrapper over any OpenAI-compatible chat endpoint.

Deliberately not tied to one vendor: `OPENAI_BASE_URL` points at DeepInfra,
Together, Groq, OpenAI itself or a local server. The agent only needs tool
calling, which all of them speak the same way.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

DEFAULT_MODEL = "deepseek-ai/DeepSeek-V3"


@dataclass
class Reply:
    message: Any            # the raw assistant message
    usage: Any              # token usage, or None
    latency_ms: int


class LLM:
    def __init__(self, model: str | None = None, *, client: Any = None):
        self.model = model or os.getenv("SQL_AGENT_MODEL", DEFAULT_MODEL)
        if client is not None:
            self.client = client
            return
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Copy .env.example to .env and fill it in, "
                "or export the variable."
            )
        self.client = OpenAI(
            api_key=api_key,
            base_url=os.getenv("OPENAI_BASE_URL") or None,
        )

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Reply:
        t0 = time.perf_counter()
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=0,
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return Reply(
            message=resp.choices[0].message,
            usage=getattr(resp, "usage", None),
            latency_ms=latency_ms,
        )
