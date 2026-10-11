"""Gemini over its REST API, with the standard library only.

The key is read from the environment (GOOGLE_API_KEY, then GEMINI_API_KEY,
the same order Google's SDKs use) and sent in a header, never in the URL,
so it cannot end up in logs, caches or error messages.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from .provider import LLMError, LLMRequest, LLMResponse

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3.8-flash"
KEY_VARIABLES = ("GOOGLE_API_KEY", "GEMINI_API_KEY")
RETRY_STATUSES = {429, 500, 502, 503, 504}


def api_key() -> str:
    for name in KEY_VARIABLES:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise LLMError("no API key: set GEMINI_API_KEY in this terminal first")


def default_model() -> str:
    return os.environ.get("MAPWRIGHT_MODEL", "").strip() or DEFAULT_MODEL


def takes_temperature(model: str) -> bool:
    """Gemini 3 models deprecate sampling parameters; older ones accept them."""
    return not model.startswith("gemini-3")


def thinking_level(model: str) -> str | None:
    """Gemini 3 thinks before answering; "low" is plenty for mapping and value lookups.

    MAPWRIGHT_THINKING_LEVEL overrides it (low, medium, high), and "default"
    sends nothing so the model uses its own default.
    """
    if not model.startswith("gemini-3"):
        return None
    level = os.environ.get("MAPWRIGHT_THINKING_LEVEL", "low").strip().lower()
    return None if level in ("", "default") else level


def build_body(request: LLMRequest, model: str = "") -> dict:
    model = model or request.model
    config: dict = {}
    if takes_temperature(model):
        config["temperature"] = request.temperature
    level = thinking_level(model)
    if level:
        config["thinkingConfig"] = {"thinkingLevel": level}
    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": request.prompt}]}],
        "generationConfig": config,
    }
    if request.system:
        body["systemInstruction"] = {"parts": [{"text": request.system}]}
    if request.json_output:
        body["generationConfig"]["responseMimeType"] = "application/json"
    return body


def parse_response(data: dict, model: str) -> LLMResponse:
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError) as error:
        reason = data.get("promptFeedback", {}).get("blockReason") or data.get("candidates", [{}])[0].get(
            "finishReason", "no candidates"
        )
        raise LLMError(f"model returned no text ({reason})") from error
    usage = data.get("usageMetadata", {})
    return LLMResponse(
        text="".join(part.get("text", "") for part in parts),
        model=data.get("modelVersion", model),
        input_tokens=usage.get("promptTokenCount", 0),
        output_tokens=usage.get("candidatesTokenCount", 0),
    )


class GeminiProvider:
    def __init__(
        self, model: str | None = None, retries: int = 4, timeout: float = 90.0, min_interval: float | None = None
    ):
        self.model = model or default_model()
        self.retries = retries
        self.timeout = timeout
        self.min_interval = (
            float(os.environ.get("MAPWRIGHT_MIN_INTERVAL", "6")) if min_interval is None else min_interval
        )
        self._last_call = 0.0
        self._key = api_key()

    def _wait_turn(self) -> None:
        """Space calls out to stay under free-tier requests-per-minute limits."""
        pause = self._last_call + self.min_interval - time.monotonic()
        if pause > 0:
            time.sleep(pause)
        self._last_call = time.monotonic()

    def complete(self, request: LLMRequest) -> LLMResponse:
        model = request.model or self.model
        payload = json.dumps(build_body(request, model)).encode("utf-8")
        http = urllib.request.Request(
            ENDPOINT.format(model=model),
            data=payload,
            headers={"Content-Type": "application/json", "x-goog-api-key": self._key},
            method="POST",
        )
        delay = 10.0
        for attempt in range(self.retries + 1):
            self._wait_turn()
            started = time.monotonic()
            label = f"  gemini {request.prompt_version or 'request'}" + (f" (retry {attempt})" if attempt else "")
            print(f"{label} ...", end="", flush=True)
            try:
                with urllib.request.urlopen(http, timeout=self.timeout) as reply:
                    response = parse_response(json.loads(reply.read().decode("utf-8")), model)
                print(f" {time.monotonic() - started:.1f}s", flush=True)
                return response
            except urllib.error.HTTPError as error:
                print(f" HTTP {error.code}", flush=True)
                detail = _error_message(error)
                if error.code in RETRY_STATUSES and attempt < self.retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise LLMError(f"Gemini returned HTTP {error.code}: {detail}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                print(f" {type(error).__name__} after {time.monotonic() - started:.0f}s", flush=True)
                if attempt < self.retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                reason = getattr(error, "reason", None) or type(error).__name__
                raise LLMError(f"could not reach Gemini after {self.retries + 1} tries: {reason}") from None
        raise LLMError("Gemini did not answer")


def _error_message(error: urllib.error.HTTPError) -> str:
    try:
        return json.loads(error.read().decode("utf-8"))["error"]["message"]
    except Exception:
        return error.reason or "no details"
