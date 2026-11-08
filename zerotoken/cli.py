"""Command line interface.

    zerotoken index <path> [--db FILE] [--full]
    zerotoken serve [--path PATH] [--watch] [--db FILE]

``serve`` speaks MCP over stdio, so it never writes anything but protocol
frames to stdout; diagnostics go to stderr.  ``index`` is a normal
interactive command and reports to stdout.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Optional, Sequence

from . import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zerotoken",
        description="Zero-token semantic code graph for AI coding assistants (MCP server).",
    )
    parser.add_argument("--version", action="version", version=f"zerotoken {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="{index,serve}")

    p_index = sub.add_parser("index", help="build or refresh the SQLite index for a project")
    p_index.add_argument("path", help="project directory to index")
    p_index.add_argument("--db", help="database path (default: <path>/.zerotoken/zerotoken.db)")
    p_index.add_argument("--full", action="store_true", help="re-parse every file, ignoring cached state")
    p_index.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    p_serve = sub.add_parser("serve", help="run the MCP server on stdio")
    p_serve.add_argument("--path", default=".", help="project directory to serve (default: current directory)")
    p_serve.add_argument("--db", help="database path (default: <path>/.zerotoken/zerotoken.db)")
    p_serve.add_argument("--watch", action="store_true", help="watch the filesystem and re-index edits live")
    return parser


def _cmd_index(args: argparse.Namespace) -> int:
    from .indexer import ProjectIndex  # deferred: keeps `--help` instant

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="zerotoken: %(levelname)s %(message)s",
    )
    try:
        index = ProjectIndex(args.path, args.db)
    except (NotADirectoryError, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    started = time.perf_counter()
    try:
        stats = index.index_directory(force=args.full)
        totals = index.stats()
    finally:
        index.close()
    elapsed = time.perf_counter() - started
    print(
        f"Indexed {stats['indexed']} file(s), {stats['unchanged']} unchanged, "
        f"{stats['removed']} removed in {elapsed:.2f}s"
    )
    print(
        f"Index now holds {totals['files']} files, {totals['symbols']} symbols, "
        f"{totals['references']} references"
    )
    print(f"Database: {index.db_path}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from .server import run_server  # deferred: imports the MCP SDK

    try:
        run_server(args.path, args.db, watch=args.watch)
    except (NotADirectoryError, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)  # stderr: stdout is the protocol channel
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "index":
        return _cmd_index(args)
    if args.command == "serve":
        return _cmd_serve(args)
    return 2  # pragma: no cover - argparse enforces a command


if __name__ == "__main__":
    sys.exit(main())
