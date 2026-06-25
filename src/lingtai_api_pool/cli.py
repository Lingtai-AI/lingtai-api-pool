"""Command-line interface: ``serve``, ``check``, ``route``.

Uses stdlib :mod:`argparse` to keep the dependency footprint minimal.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from .config import ConfigError, load_config
from .pool import UpstreamPool
from .routing import select_least_inflight, select_sticky


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config", required=True, help="path to the TOML config file"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lingtai-api-pool",
        description="Local API upstream pool / virtual REST proxy.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="start the local HTTP proxy server")
    _add_config_arg(p_serve)
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)

    p_check = sub.add_parser(
        "check", help="validate config and print a secret-safe summary"
    )
    _add_config_arg(p_check)

    p_route = sub.add_parser(
        "route", help="show which upstream a sticky key would select"
    )
    _add_config_arg(p_route)
    p_route.add_argument(
        "--session-id", required=True, help="sticky key to resolve"
    )

    return parser


def cmd_check(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    summary = config.summary()
    print(json.dumps(summary, indent=2, sort_keys=True))
    warnings = [
        f"  upstream {u['id']!r}: unset env {u['missing_env']}"
        for u in summary["upstreams"]
        if u["missing_env"]
    ]
    if warnings:
        print("\nWarning: referenced environment variables are not set:")
        print("\n".join(warnings))
    return 0


def cmd_route(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    pool = UpstreamPool(config)
    candidates = pool.healthy()
    selected = select_sticky(args.session_id, candidates)
    if selected is None:
        print("no healthy upstreams available", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "session_id": args.session_id,
                "strategy": "weighted-rendezvous",
                "upstream_id": selected.id,
                "base_url": selected.base_url,
            },
            indent=2,
        )
    )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .proxy import create_app

    config = load_config(args.config)
    app = create_app(config)
    print(
        f"lingtai-api-pool serving {len(config.upstreams)} upstream(s) "
        f"on http://{args.host}:{args.port}"
    )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {"serve": cmd_serve, "check": cmd_check, "route": cmd_route}
    try:
        return handlers[args.command](args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
