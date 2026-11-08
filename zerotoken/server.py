"""FastMCP server: token-efficient code navigation tools over stdio.

Four tools are exposed -- ``search_symbols``, ``get_symbol_definition``,
``get_file_outline`` and ``find_callers``.  Every tool returns compact plain
text (not JSON) because the output is paid for in the model's context window.

IMPORTANT: stdout carries the JSON-RPC stream.  Nothing in this module may
print to stdout; logging is routed to stderr.
"""

import functools
import logging
import sys
import threading
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .indexer import ProjectIndex, normalize_kind
from .watcher import IndexWatcher

log = logging.getLogger("zerotoken.server")

READY_WAIT_SECONDS = 10.0  # how long a tool waits for the initial sync
MAX_DEFINITION_LINES = 400
MAX_SEARCH_RESULTS = 25
MAX_CALLER_RESULTS = 100

INSTRUCTIONS = (
    "ZeroToken is a local code-navigation index. Prefer it over reading whole files: "
    "search_symbols to locate a function/class, get_file_outline to see a file's structure "
    "without bodies, get_symbol_definition to read just one definition, and find_callers to "
    "see where a name is used. Read a full file only when these are not enough."
)

mcp = FastMCP("zerotoken", instructions=INSTRUCTIONS)


class _Runtime:
    """Process-wide state shared by the tool functions."""

    def __init__(self) -> None:
        self.index: Optional[ProjectIndex] = None
        self.ready = threading.Event()
        self.sync_error: Optional[str] = None


RUNTIME = _Runtime()


def configure(index: ProjectIndex, ready: bool = True) -> None:
    """Attach an index to the server (used by :func:`run_server` and tests)."""
    RUNTIME.index = index
    if ready:
        RUNTIME.ready.set()


def _index() -> tuple[ProjectIndex, str]:
    """Return the index and a (possibly empty) note about initial indexing."""
    idx = RUNTIME.index
    if idx is None:
        raise RuntimeError("ZeroToken server is not configured with a project index")
    note = ""
    if not RUNTIME.ready.wait(READY_WAIT_SECONDS):
        note = "\n(note: initial indexing is still running; results may be incomplete)"
    elif RUNTIME.sync_error:
        note = f"\n(note: initial indexing failed: {RUNTIME.sync_error})"
    return idx, note


def _first_line(text: Optional[str], limit: int = 90) -> str:
    if not text:
        return ""
    line = text.strip().splitlines()[0].strip()
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _loc(sym: dict) -> str:
    return f"{sym['file_path']}:{sym['start_line']}-{sym['end_line']}"


def _guard(fn):
    """Turn exceptions into short error strings instead of protocol errors."""

    @functools.wraps(fn)  # keeps name, docstring and signature visible to FastMCP
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            return f"Error: {exc}"
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("tool %s failed", fn.__name__)
            return f"Error: {type(exc).__name__}: {exc}"

    return wrapper


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


@mcp.tool()
@_guard
def search_symbols(query: str, kind: Optional[str] = None, limit: int = 20) -> str:
    """Find functions, classes, methods, interfaces, types and enums by name.

    Fuzzy: matches word fragments, camelCase / snake_case parts and prefixes,
    so "auth login" finds ``AuthService.login``. Returns one entry per symbol
    with its kind, qualified name, file:start-end lines and signature.

    Args:
        query: Name or words to look for (e.g. "parse config", "UserRepo").
        kind: Optional filter: function, class, method, interface, type or enum.
        limit: Maximum results (default 20, max 25).
    """
    idx, note = _index()
    limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))
    kind = normalize_kind(kind)
    rows = idx.search_symbols(query, kind, limit)
    if not rows:
        return f"No symbols match {query!r}" + (f" with kind={kind}" if kind else "") + "." + note
    out = [f"{len(rows)} match(es) for {query!r}:"]
    for r in rows:
        doc = _first_line(r["docstring"])
        out.append(f"{r['kind']} {r['qualified_name']}  {_loc(r)}")
        out.append(f"  {r['signature']}" + (f"  # {doc}" if doc else ""))
    return "\n".join(out) + note


@mcp.tool()
@_guard
def get_symbol_definition(symbol_name: str, file_path: Optional[str] = None) -> str:
    """Return the exact source of one function/class/method definition.

    Reads only the lines of that definition, never the whole file. Use
    ``Class.method`` to target a method. If the name is defined in several
    places, the candidates are listed and you should repeat the call with
    ``file_path``.

    Args:
        symbol_name: Symbol name or qualified name (e.g. "login", "AuthService.login").
        file_path: Optional path (relative to the project root, or absolute) to disambiguate.
    """
    idx, note = _index()
    matches = idx.find_symbols(symbol_name, file_path)
    if not matches:
        hint = " Try search_symbols for fuzzy matching." if not file_path else ""
        return f"No definition found for {symbol_name!r}" + (f" in {file_path}" if file_path else "") + "." + hint + note
    if len(matches) > 1:
        out = [f"{len(matches)} definitions match {symbol_name!r}; repeat with file_path to pick one:"]
        for m in matches[:20]:
            out.append(f"{m['kind']} {m['qualified_name']}  {_loc(m)}")
            out.append(f"  {m['signature']}")
        if len(matches) > 20:
            out.append(f"... and {len(matches) - 20} more")
        return "\n".join(out) + note
    m = matches[0]
    lines = idx.read_lines(m["file_path"], m["start_line"], m["end_line"])
    truncated = 0
    if len(lines) > MAX_DEFINITION_LINES:
        truncated = len(lines) - MAX_DEFINITION_LINES
        lines = lines[:MAX_DEFINITION_LINES]
    out = [f"# {m['kind']} {m['qualified_name']} - {_loc(m)}", *lines]
    if truncated:
        out.append(f"# ... truncated {truncated} more lines (definition spans {m['start_line']}-{m['end_line']})")
    return "\n".join(out) + note


@mcp.tool()
@_guard
def get_file_outline(file_path: str) -> str:
    """Compact structural outline of a file: classes, functions and method signatures only.

    Function bodies are omitted, so this costs a small fraction of reading the
    file. Each line ends with its 1-based line range.

    Args:
        file_path: Path relative to the project root, or absolute.
    """
    idx, note = _index()
    result = idx.get_outline(file_path)
    if result is None:
        rel = idx.resolve_rel(file_path)
        if idx.classify(rel) is None:
            return f"{rel} is not an indexable source file (supported: .py .js .jsx .mjs .cjs .ts .tsx)." + note
        return f"{rel} is not in the index (does it exist?)." + note
    frow, syms = result
    depth_of: dict[str, int] = {}
    out = [f"{frow['path']} ({frow['language']}, {frow['line_count']} lines, {len(syms)} symbols)"]
    for s in syms:
        depth = depth_of.get(s["parent"], -1) + 1 if s["parent"] else 0
        depth_of[s["qualified_name"]] = depth
        doc = _first_line(s["docstring"], 70)
        out.append(f"{'  ' * depth}{s['signature']}  [{s['start_line']}-{s['end_line']}]" + (f"  # {doc}" if doc else ""))
    if not syms:
        out.append("(no functions, classes or types found)")
    return "\n".join(out) + note


@mcp.tool()
@_guard
def find_callers(symbol_name: str) -> str:
    """Find call sites and references to a symbol across the indexed project.

    Matching is by name (no type resolution), so unrelated symbols that share a
    name can appear; each hit shows the enclosing function and the source line
    so it is easy to tell them apart. Covers calls, ``new`` expressions,
    decorators, JSX components and class inheritance.

    Args:
        symbol_name: Function, method or class name ("login" or "AuthService.login").
    """
    idx, note = _index()
    rows, total = idx.find_references(symbol_name, MAX_CALLER_RESULTS)
    bare = symbol_name.strip().split(".")[-1]
    if not rows:
        return f"No references to {bare!r} found in the index." + note
    out = [f"{total} reference(s) to {bare!r}" + (f" (showing first {len(rows)})" if total > len(rows) else "") + ":"]
    current = None
    for r in rows:
        if r["file_path"] != current:
            current = r["file_path"]
            out.append(f"{current}:")
        where = f" [in {r['caller']}]" if r["caller"] else " [module level]"
        tag = "" if r["kind"] == "call" else f" ({r['kind']})"
        out.append(f"  {r['line']}{where}{tag}: {r['snippet']}")
    return "\n".join(out) + note


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def run_server(root: str, db_path: Optional[str] = None, watch: bool = False) -> None:
    """Index ``root`` (incrementally, in the background) and serve MCP on stdio."""
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="zerotoken: %(levelname)s %(message)s"
    )
    index = ProjectIndex(root, db_path)
    configure(index, ready=False)

    watcher: Optional[IndexWatcher] = None
    if watch:
        watcher = IndexWatcher(index)
        watcher.start()  # start first so edits made during the initial sync are not missed

    def initial_sync() -> None:
        try:
            stats = index.index_directory()
            log.info("initial sync complete: %s", stats)
        except Exception as exc:
            RUNTIME.sync_error = f"{type(exc).__name__}: {exc}"
            log.exception("initial sync failed")
        finally:
            RUNTIME.ready.set()

    threading.Thread(target=initial_sync, name="zerotoken-sync", daemon=True).start()
    try:
        mcp.run(transport="stdio")
    finally:
        if watcher is not None:
            watcher.stop()
        index.close()
