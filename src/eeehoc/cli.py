"""CLI entrypoint: print a slate or serve the Live chiclet dashboard."""

from __future__ import annotations

import argparse
import json

from eeehoc import __version__
from eeehoc.live import DATE_RE, LiveFeed


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eeehoc", description="NHL Live chiclets dashboard")
    p.add_argument("--version", action="version", version=f"eeehoc {__version__}")
    p.add_argument("--dashboard", action="store_true", help="Serve the web dashboard")
    p.add_argument("--port", type=int, default=8083, help="Dashboard port (default 8083)")
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="Dashboard bind address (default 127.0.0.1; use 0.0.0.0 in Docker)",
    )
    p.add_argument("--live", action="store_true", help="Print today's slate (or --date) as JSON")
    p.add_argument("--date", default=None, help="Slate date YYYYMMDD (default: ESPN's current day)")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    dates = args.date
    if dates is not None and not DATE_RE.fullmatch(dates):
        build_parser().error("--date must be YYYYMMDD")

    if args.live:
        board = LiveFeed().get(dates=dates)
        print(json.dumps(board, indent=2))

    if args.dashboard:
        from eeehoc.dashboard import serve

        print(f"Serving dashboard on http://{args.host}:{args.port}")
        serve(port=args.port, host=args.host)

    if not args.dashboard and not args.live:
        build_parser().print_help()
        raise SystemExit(0)
