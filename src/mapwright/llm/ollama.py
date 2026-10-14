"""A local model through Ollama (https://ollama.com), with the standard library only.

Free, no API key, and the data never leaves the machine. Models are named
``ollama/<model>``, for example ``ollama/qwen2.5:7b``; the prefix keeps
their cache entries apart from Gemini's. Pull a model once with
``ollama pull qwen2.5:7b``. OLLAMA_HOST changes the address (default
http://127.0.0.1:11434).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from .provider import LLMError, LLMRequest, LLMResponse

PREFIX = "ollama/"
DEFAULT_MODEL = PREFIX + "qwen2.5:7b"


def host() -> str:
    value = os.environ.get("OLLAMA_HOST", "").strip() or "http://127.0.0.1:11434"
    if not value.startswith(("http://", "https://")):
        value = "http://" + value
    return value.rstrip("/")


def is_ollama(model: str | None) -> bool:
    return bool(model) and model.startswith(PREFIX)


def build_body(request: LLMRequest, name: str) -> dict:
    messages = []
    if request.system:
        messages.append({"role": "system", "content": request.system})
    messages.append({"role": "user", "content": request.prompt})
    body = {
        "model": name,
        "messages": messages,
        "stream": False,
        # The sample number seeds the model, so repeated runs differ the way Gemini's do.
        "options": {"temperature": request.temperature, "seed": request.sample},
    }
    if request.json_output:
        body["format"] = "json"
    return body


def _get(path: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(host() + path, timeout=timeout) as reply:
        return json.loads(reply.read().decode("utf-8"))


def installed_models() -> list[str]:
    """Local models, as ``ollama/<name>``; empty when Ollama is not running."""
    try:
        data = _get("/api/tags", timeout=2.0)
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
        return []
    return sorted(PREFIX + m["name"] for m in data.get("models", []) if m.get("name"))


class OllamaProvider:
    def __init__(self, model: str | None = None, timeout: float = 600.0):
        self.model = model or DEFAULT_MODEL
        if not is_ollama(self.model):
            self.model = PREFIX + self.model
        self.timeout = timeout

    def complete(self, request: LLMRequest) -> LLMResponse:
        model = request.model or self.model
        name = model.removeprefix(PREFIX)
        payload = json.dumps(build_body(request, name)).encode("utf-8")
        http = urllib.request.Request(
            host() + "/api/chat", data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        started = time.monotonic()
        print(f"  {model} {request.prompt_version or 'request'} ...", end="", flush=True)
        try:
            with urllib.request.urlopen(http, timeout=self.timeout) as reply:
                data = json.loads(reply.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            print(f" HTTP {error.code}", flush=True)
            try:
                detail = json.loads(error.read().decode("utf-8")).get("error", "")
            except Exception:
                detail = error.reason
            if error.code == 404:
                raise LLMError(f"Ollama has no model {name!r}: run  ollama pull {name}") from None
            raise LLMError(f"Ollama returned HTTP {error.code}: {detail}") from None
        except (urllib.error.URLError, ConnectionError) as error:
            print(" not reachable", flush=True)
            raise LLMError(
                f"Ollama is not running at {host()}: start the Ollama app (or `ollama serve`) and try again"
            ) from error
        except TimeoutError:
            print(" timed out", flush=True)
            raise LLMError(f"{model} took longer than {self.timeout:.0f}s; try a smaller model") from None
        print(f" {time.monotonic() - started:.1f}s", flush=True)
        text = (data.get("message") or {}).get("content", "")
        if not text:
            raise LLMError(f"{model} returned no text")
        return LLMResponse(
            text=text,
            model=model,
            input_tokens=data.get("prompt_eval_count", 0),
            output_tokens=data.get("eval_count", 0),
        )
