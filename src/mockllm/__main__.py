"""Run the mock upstream: `python -m mockllm --port 9000 --ttft-ms 300 --tokens-per-s 50`."""

from __future__ import annotations

import argparse

import uvicorn

from .app import MockConfig, create_mock_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="mockllm")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--ttft-ms", type=float, default=200)
    parser.add_argument("--tokens-per-s", type=float, default=50)
    parser.add_argument("--output-tokens", type=int, default=64)
    parser.add_argument("--error-rate", type=float, default=0.0)
    parser.add_argument("--error-status", type=int, default=503)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    config = MockConfig(ttft_ms=args.ttft_ms, tokens_per_s=args.tokens_per_s,
                        output_tokens=args.output_tokens, error_rate=args.error_rate,
                        error_status=args.error_status, seed=args.seed)
    uvicorn.run(create_mock_app(config), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
