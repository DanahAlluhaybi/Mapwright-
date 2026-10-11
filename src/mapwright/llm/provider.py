"""The provider interface every model backend implements.

A request is plain data, so it can be hashed for the cache and written to
the audit log. A response carries the text and the token counts the
evaluation reports as cost.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Protocol


class LLMError(RuntimeError):
    """The model could not be reached or returned something unusable."""


@dataclass(frozen=True)
class LLMRequest:
    prompt: str
    system: str = ""
    model: str = ""
    prompt_version: str = ""
    temperature: float = 0.0
    json_output: bool = True
    sample: int = 0  # repeat number, so repeated runs get their own cache entries

    def key(self) -> str:
        """Stable hash of everything that can change the answer."""
        payload = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached: bool = False

    def json(self) -> object:
        try:
            return json.loads(self.text)
        except json.JSONDecodeError as error:
            raise LLMError(f"response is not valid JSON: {error}") from error

    def to_dict(self) -> dict:
        return asdict(self)


class Provider(Protocol):
    model: str

    def complete(self, request: LLMRequest) -> LLMResponse: ...


@dataclass
class Usage:
    """Running totals for one run, in the shape result.json reports."""

    llm_calls: int = 0
    cached_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    calls: list[dict] = field(default_factory=list)

    def record(self, request: LLMRequest, response: LLMResponse) -> None:
        self.llm_calls += 1
        self.cached_calls += response.cached
        self.input_tokens += response.input_tokens
        self.output_tokens += response.output_tokens
        self.calls.append(
            {
                "key": request.key(),
                "model": response.model,
                "prompt_version": request.prompt_version,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "cached": response.cached,
            }
        )

    def summary(self) -> dict:
        return {"llm_calls": self.llm_calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens}
