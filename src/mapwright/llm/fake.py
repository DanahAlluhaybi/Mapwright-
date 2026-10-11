"""A scripted provider for tests: answers come from a function, never the network."""

from __future__ import annotations

import json
from typing import Callable

from .provider import LLMRequest, LLMResponse


class FakeProvider:
    def __init__(self, answer: Callable[[LLMRequest], object], model: str = "fake-model"):
        self.answer = answer
        self.model = model
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        result = self.answer(request)
        text = result if isinstance(result, str) else json.dumps(result)
        return LLMResponse(
            text=text, model=self.model, input_tokens=len(request.prompt) // 4, output_tokens=len(text) // 4
        )
