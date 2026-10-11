"""Checking the model setup.

python -m mapwright.llm models    list the models your key can use
python -m mapwright.llm ping      send one small request and show the answer
"""

from __future__ import annotations

import argparse
import sys

from .gemini import GeminiProvider
from .provider import LLMError, Request, parse_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mapwright.llm")
    parser.add_argument("command", choices=["models", "ping"])
    parser.add_argument("--model")
    args = parser.parse_args(argv)
    try:
        provider = GeminiProvider(model=args.model, min_gap=0)
        if args.command == "models":
            for name in provider.list_models():
                print(name)
            return 0
        request = Request("ping", "Answer with one JSON object.", 'Reply with {"ok": true}.', "ping")
        response = provider.complete(request)
        print(
            f"model {provider.model}: {parse_json(response.text)} "
            f"({response.input_tokens} tokens in, {response.output_tokens} out)"
        )
        return 0
    except (LLMError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
