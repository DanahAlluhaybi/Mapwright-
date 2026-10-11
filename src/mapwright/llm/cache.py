"""A response cache that makes LLM runs reproducible.

Every request is stored with its response under ``<directory>/<key>.json``,
where the key hashes the model, prompt version, system text and prompt. In
``record`` mode a miss calls the real provider and saves the answer; in
``replay`` mode a miss is an error and nothing leaves the machine. Anyone
with the cache can re-run and re-score a published run without an API key.
"""

from __future__ import annotations

import json
from pathlib import Path

from .provider import LLMError, LLMRequest, LLMResponse, Provider


class CacheMiss(LLMError):
    """Replay mode found no stored answer for a request."""


class CachedProvider:
    def __init__(self, directory: str | Path, inner: Provider | None = None, model: str = ""):
        self.directory = Path(directory)
        self.inner = inner
        self.model = inner.model if inner is not None else model

    @property
    def replay_only(self) -> bool:
        return self.inner is None

    def path(self, request: LLMRequest) -> Path:
        return self.directory / f"{request.key()}.json"

    def complete(self, request: LLMRequest) -> LLMResponse:
        if not request.model:
            raise LLMError("cached requests must name their model, or the cache key is ambiguous")
        path = self.path(request)
        if path.exists():
            stored = json.loads(path.read_text("utf-8"))["response"]
            return LLMResponse(**{**stored, "cached": True})
        if self.inner is None:
            raise CacheMiss(f"no cached answer for request {request.key()[:12]} (replay mode)")

        response = self.inner.complete(request)
        self.directory.mkdir(parents=True, exist_ok=True)
        record = {"request": request.to_dict(), "response": {**response.to_dict(), "cached": False}}
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return response
