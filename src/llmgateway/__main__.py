"""Run the gateway: `python -m llmgateway --config config/gateway.yaml`."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import uvicorn

from .app import create_app
from .config import load_config


def _default_config() -> str:
    if env := os.environ.get("LLMGW_CONFIG"):
        return env
    local = Path("config/gateway.yaml")
    return str(local if local.exists() else Path("config/gateway.example.yaml"))


def main() -> None:
    parser = argparse.ArgumentParser(prog="llmgateway")
    parser.add_argument("--config", default=_default_config())
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper(), format="%(message)s")
    app = create_app(load_config(args.config))
    # The access log replaces uvicorn's own per-request line.
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level, access_log=False)


if __name__ == "__main__":
    main()
