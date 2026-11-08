"""AST extraction and the local SQLite symbol graph.

This module has two halves:

1. **Extraction** - :func:`parse_source` turns source bytes into a
   :class:`ParsedFile` (symbols + call/reference sites) using tree-sitter.
   Python, JavaScript, JSX, TypeScript and TSX are supported.  Extraction walks
   the syntax tree directly instead of using tree-sitter ``Query`` objects: the
   query API changed between tree-sitter 0.22 and 0.25, whereas the node API
   used here is stable across the whole supported range.

2. **Storage** - :class:`ProjectIndex` owns an embedded SQLite database
   (``zerotoken.db``) with an FTS5 index over symbol names and signatures, and
   supports incremental, per-file updates.

Nothing in this module writes to stdout: when embedded in an MCP stdio server,
stdout belongs to the JSON-RPC stream.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import inspect
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

log = logging.getLogger("zerotoken.indexer")

SCHEMA_VERSION = 1
DB_DIR_NAME = ".zerotoken"
DB_FILE_NAME = "zerotoken.db"

MAX_FILE_BYTES = 2 * 1024 * 1024  # skip generated / vendored monsters
MAX_DOC_CHARS = 2000
MAX_SIGNATURE_CHARS = 300
MAX_SNIPPET_CHARS = 160

#: Directory names that are never descended into.
IGNORED_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
        ".env", ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
        ".idea", ".next", ".nuxt", "dist", "build", "site-packages",
        "__pypackages__", DB_DIR_NAME,
    }
)

#: file suffix -> tree-sitter language key
EXTENSION_LANGUAGE = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
}

VALID_KINDS = ("function", "class", "method", "interface", "type", "enum")
_KIND_ALIASES = {
    "func": "function", "fn": "function", "def": "function", "functions": "function",
    "classes": "class", "methods": "method", "interfaces": "interface",
    "types": "type", "enums": "enum",
}


def normalize_kind(kind: Optional[str]) -> Optional[str]:
    """Normalise a user supplied ``kind`` filter, or raise ``ValueError``."""
    if kind is None or not str(kind).strip():
        return None
    k = str(kind).strip().lower()
    k = _KIND_ALIASES.get(k, k)
    if k not in VALID_KINDS:
        raise ValueError(f"unknown kind {kind!r}; valid kinds: {', '.join(VALID_KINDS)}")
    return k


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class Symbol:
    """A definition extracted from a source file (1-based, inclusive lines)."""

    name: str
    qualified_name: str
    kind: str
    signature: str
    docstring: Optional[str]
    start_line: int
    end_line: int
    parent: Optional[str] = None


@dataclass
class Reference:
    """A call / instantiation / decorator / JSX / inheritance site."""

    name: str
    kind: str  # call | new | decorator | jsx | inherit
    line: int
    snippet: str = ""
    caller: Optional[str] = None  # qualified name of the enclosing symbol


@dataclass
class ParsedFile:
    language: str
    symbols: list[Symbol] = field(default_factory=list)
    refs: list[Reference] = field(default_factory=list)
    line_count: int = 0


class ParserUnavailableError(RuntimeError):
    """Raised when the tree-sitter grammar for a language is not installed."""


# --------------------------------------------------------------------------- #
# Identifier helpers
# --------------------------------------------------------------------------- #

_CAMEL = re.compile(r"([a-z0-9])([A-Z])|([A-Z]+)([A-Z][a-z])")


def split_identifier(text: str) -> list[str]:
    """Split ``getHTTPResponse_v2`` into ``['get', 'http', 'response', 'v2']``."""
    spaced = _CAMEL.sub(
        lambda m: f"{m.group(1)} {m.group(2)}" if m.group(1) else f"{m.group(3)} {m.group(4)}",
        text,
    )
    return [w.lower() for w in re.findall(r"[A-Za-z0-9]+", spaced)]


def _fts_query(query: str, mode: str) -> Optional[str]:
    """Build a safe FTS5 MATCH expression (prefix terms, AND or OR)."""
    seen: list[str] = []
    for tok in split_identifier(query):
        if tok not in seen:
            seen.append(tok)
    if not seen:
        return None
    parts = [f'"{t}"*' for t in seen]
    return (" OR " if mode == "OR" else " ").join(parts)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --------------------------------------------------------------------------- #
# tree-sitter loading (version tolerant: 0.22 .. current)
# --------------------------------------------------------------------------- #

_TLS = threading.local()


@functools.lru_cache(maxsize=None)
def _get_language(language: str):
    """Load and cache the tree-sitter ``Language`` for ``language``."""
    try:
        from tree_sitter import Language

        if language == "python":
            import tree_sitter_python as mod

            ptr = mod.language()
        elif language == "javascript":
            import tree_sitter_javascript as mod

            ptr = mod.language()
        elif language == "typescript":
            import tree_sitter_typescript as mod

            ptr = mod.language_typescript()
        elif language == "tsx":
            import tree_sitter_typescript as mod

            ptr = mod.language_tsx()
        else:  # pragma: no cover - guarded by EXTENSION_LANGUAGE
            raise ParserUnavailableError(f"unsupported language {language!r}")
    except ImportError as exc:
        pkg = "tree-sitter-typescript" if language in ("typescript", "tsx") else f"tree-sitter-{language}"
        raise ParserUnavailableError(f"{pkg} is not installed ({exc})") from exc
    try:
        return Language(ptr)
    except TypeError:  # tree-sitter 0.22 wants an explicit name
        return Language(ptr, language)


def _make_parser(lang):
    from tree_sitter import Parser

    try:
        return Parser(lang)
    except TypeError:  # older constructor signature
        parser = Parser()
        if hasattr(parser, "set_language"):
            parser.set_language(lang)
        else:
            parser.language = lang
        return parser


def _get_parser(language: str):
    """Return a per-thread parser (tree-sitter parsers are not thread safe)."""
    parsers = getattr(_TLS, "parsers", None)
    if parsers is None:
        parsers = _TLS.parsers = {}
    parser = parsers.get(language)
    if parser is None:
        parser = parsers[language] = _make_parser(_get_language(language))
    return parser


# --------------------------------------------------------------------------- #
# Node helpers
# --------------------------------------------------------------------------- #


def _span(src: bytes, start: int, end: int) -> str:
    return src[start:end].decode("utf-8", "replace")


def _node_text(src: bytes, node) -> str:
    return _span(src, node.start_byte, node.end_byte)


def _lines_of(node) -> tuple[int, int]:
    """1-based inclusive (start, end) lines; ignores a trailing-newline overshoot."""
    start = node.start_point[0]
    end = node.end_point[0]
    if node.end_point[1] == 0 and end > start:
        end -= 1
    return start + 1, end + 1


def _clean_sig(raw: str, limit: int = MAX_SIGNATURE_CHARS) -> str:
    sig = " ".join(raw.split()).rstrip(" :{").rstrip()
    return sig if len(sig) <= limit else sig[: limit - 1] + "…"


def _qualify(parent: Optional[str], name: str) -> str:
    return f"{parent}.{name}" if parent else name


# --------------------------------------------------------------------------- #
# Python extraction
# --------------------------------------------------------------------------- #


def _py_docstring(body, src: bytes) -> Optional[str]:
    if body is None or body.type != "block":
        return None
    for child in body.named_children:
        if child.type == "comment":
            continue
        if child.type == "expression_statement" and child.named_child_count:
            lit = child.named_children[0]
            if lit.type in ("string", "concatenated_string"):
                raw = _node_text(src, lit)
                try:
                    val = ast.literal_eval(raw)
                except Exception:
                    val = raw.strip("'\"")
                if isinstance(val, str):
                    return inspect.cleandoc(val)[:MAX_DOC_CHARS] or None
        break
    return None


def _py_symbol(node, src: bytes, parent: Optional[str], in_class: bool) -> Optional[Symbol]:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = _node_text(src, name_node)
    body = node.child_by_field_name("body")
    if node.type == "class_definition":
        kind = "class"
    else:
        kind = "method" if in_class else "function"
    outer = node.parent if node.parent is not None and node.parent.type == "decorated_definition" else node
    start, _ = _lines_of(outer)
    _, end = _lines_of(node)
    sig = _clean_sig(_span(src, node.start_byte, body.start_byte if body is not None else node.end_byte))
    return Symbol(name, _qualify(parent, name), kind, sig, _py_docstring(body, src), start, end, parent)


def _extract_python(root, src: bytes) -> list[Symbol]:
    out: list[Symbol] = []
    stack = [(root, None, False)]
    while stack:
        node, parent, in_class = stack.pop()
        t = node.type
        if t == "function_definition":
            sym = _py_symbol(node, src, parent, in_class)
            if sym:
                out.append(sym)
            continue  # nested functions are intentionally not indexed
        if t == "class_definition":
            sym = _py_symbol(node, src, parent, in_class)
            if sym:
                out.append(sym)
            body = node.child_by_field_name("body")
            if body is not None:
                qn = sym.qualified_name if sym else parent
                for child in reversed(body.children):
                    stack.append((child, qn, True))
            continue
        for child in reversed(node.children):
            stack.append((child, parent, in_class))
    return out


# --------------------------------------------------------------------------- #
# JavaScript / TypeScript extraction
# --------------------------------------------------------------------------- #

_JS_FUNC_DECLS = frozenset({"function_declaration", "generator_function_declaration"})
_JS_CLASS_DECLS = frozenset({"class_declaration", "abstract_class_declaration"})
_JS_FUNC_VALUES = frozenset({"arrow_function", "function_expression", "function", "generator_function"})
_JS_NO_DESCEND = frozenset(
    {"arrow_function", "function_expression", "function", "generator_function", "string",
     "template_string", "comment", "regex"}
)


def _climb_export(node):
    parent = node.parent
    return parent if parent is not None and parent.type == "export_statement" else node


def _jsdoc(outer, src: bytes) -> Optional[str]:
    prev = outer.prev_named_sibling
    if prev is None or prev.type != "comment":
        return None
    if outer.start_point[0] - prev.end_point[0] > 1:
        return None
    text = _node_text(src, prev)
    if not text.startswith("/**"):
        return None
    body = text[3:]
    if body.endswith("*/"):
        body = body[:-2]
    lines = [re.sub(r"^\s*\*\s?", "", ln) for ln in body.splitlines()]
    return "\n".join(lines).strip()[:MAX_DOC_CHARS] or None


def _js_make(src, kind, parent, name, sig_raw, outer, end_node, doc) -> Symbol:
    start, _ = _lines_of(outer)
    _, end = _lines_of(end_node)
    return Symbol(name, _qualify(parent, name), kind, _clean_sig(sig_raw), doc, start, end, parent)


def _js_decl(node, src: bytes, parent: Optional[str], kind: str) -> Optional[Symbol]:
    """Function/class/method/interface/enum declarations with a ``name`` field."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    body = node.child_by_field_name("body")
    outer = _climb_export(node)
    sig = _span(src, node.start_byte, body.start_byte if body is not None else node.end_byte)
    return _js_make(src, kind, parent, _node_text(src, name_node), sig, outer, node, _jsdoc(outer, src))


def _js_bound(src, parent, kind, name_node, value, sig_prefix, sig_start, outer) -> Symbol:
    body = value.child_by_field_name("body")
    sig = sig_prefix + _span(src, sig_start, body.start_byte if body is not None else value.end_byte)
    return _js_make(src, kind, parent, _node_text(src, name_node), sig, outer, outer, _jsdoc(outer, src))


def _extract_js(root, src: bytes) -> list[Symbol]:
    out: list[Symbol] = []
    stack = [(root, None, False)]
    while stack:
        node, parent, in_class = stack.pop()
        t = node.type

        if t in _JS_FUNC_DECLS:
            sym = _js_decl(node, src, parent, "function")
            if sym:
                out.append(sym)
            continue

        if t in _JS_CLASS_DECLS:
            sym = _js_decl(node, src, parent, "class")
            if sym:
                out.append(sym)
            body = node.child_by_field_name("body")
            if body is not None:
                qn = sym.qualified_name if sym else parent
                for child in reversed(body.named_children):
                    stack.append((child, qn, True))
            continue

        if t == "method_definition":
            if in_class:
                sym = _js_decl(node, src, parent, "method")
                if sym:
                    out.append(sym)
            continue

        if t in ("field_definition", "public_field_definition"):
            value = node.child_by_field_name("value")
            if in_class and value is not None and value.type in _JS_FUNC_VALUES:
                name_node = node.child_by_field_name("property") or node.child_by_field_name("name")
                if name_node is not None:
                    out.append(_js_bound(src, parent, "method", name_node, value, "", node.start_byte, node))
            continue

        if t in ("lexical_declaration", "variable_declaration"):
            outer = _climb_export(node)
            first = node.children[0] if node.children else None
            prefix = f"{first.type} " if first is not None and first.type in ("const", "let", "var") else ""
            for decl in node.named_children:
                if decl.type != "variable_declarator":
                    continue
                name_node = decl.child_by_field_name("name")
                value = decl.child_by_field_name("value")
                if (
                    name_node is not None and name_node.type == "identifier"
                    and value is not None and value.type in _JS_FUNC_VALUES
                ):
                    out.append(_js_bound(src, parent, "function", name_node, value, prefix, decl.start_byte, outer))
            continue

        if t == "interface_declaration":
            sym = _js_decl(node, src, parent, "interface")
            if sym:
                out.append(sym)
            continue

        if t == "enum_declaration":
            sym = _js_decl(node, src, parent, "enum")
            if sym:
                out.append(sym)
            continue

        if t == "type_alias_declaration":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                outer = _climb_export(node)
                out.append(
                    _js_make(src, "type", parent, _node_text(src, name_node),
                             _node_text(src, node), outer, node, _jsdoc(outer, src))
                )
            continue

        if t in _JS_NO_DESCEND:
            continue
        for child in reversed(node.children):
            stack.append((child, parent, in_class))
    return out


# --------------------------------------------------------------------------- #
# Reference extraction (calls, `new`, decorators, JSX components, inheritance)
# --------------------------------------------------------------------------- #

_IDENT_TYPES = frozenset(
    {"identifier", "type_identifier", "property_identifier", "private_property_identifier"}
)
_CLAUSE_TYPES = frozenset({"extends_clause", "implements_clause", "extends_type_clause"})


def _callee(node, src: bytes) -> Optional[str]:
    """Reduce a callee expression to the bare name being invoked."""
    if node is None:
        return None
    t = node.type
    if t in _IDENT_TYPES:
        return _node_text(src, node)
    if t == "attribute":
        return _callee(node.child_by_field_name("attribute"), src)
    if t in ("member_expression", "nested_identifier"):
        return _callee(node.child_by_field_name("property"), src)
    if t in ("generic_type", "nested_type_identifier"):
        return _callee(node.child_by_field_name("name"), src)
    return None


def _extract_refs(root, src: bytes, language: str) -> list[Reference]:
    refs: list[Reference] = []

    def add(name: Optional[str], kind: str, node) -> None:
        if name:
            refs.append(Reference(name, kind, node.start_point[0] + 1))

    stack = [root]
    while stack:
        node = stack.pop()
        t = node.type
        if language == "python":
            if t == "call":
                add(_callee(node.child_by_field_name("function"), src), "call", node)
            elif t == "decorator":
                child = node.named_children[0] if node.named_children else None
                if child is not None and child.type != "call":
                    add(_callee(child, src), "decorator", node)
            elif t == "class_definition":
                supers = node.child_by_field_name("superclasses")
                if supers is not None:
                    for child in supers.named_children:
                        if child.type != "keyword_argument":
                            add(_callee(child, src), "inherit", child)
        else:
            if t == "call_expression":
                add(_callee(node.child_by_field_name("function"), src), "call", node)
            elif t == "new_expression":
                add(_callee(node.child_by_field_name("constructor"), src), "new", node)
            elif t in ("jsx_opening_element", "jsx_self_closing_element"):
                name = _callee(node.child_by_field_name("name"), src)
                if name and name[0].isupper():  # lowercase tags are DOM elements
                    add(name, "jsx", node)
            elif t == "class_heritage" or t in _CLAUSE_TYPES:
                for child in node.named_children:
                    if child.type in _CLAUSE_TYPES or child.type == "type_arguments":
                        continue
                    add(_callee(child, src), "inherit", child)
        stack.extend(node.children)
    return refs


def _attribute_callers(symbols: list[Symbol], refs: list[Reference], nlines: int) -> None:
    """Fill ``Reference.caller`` with the innermost enclosing symbol."""
    owner: list[Optional[str]] = [None] * (nlines + 2)
    for sym in sorted(symbols, key=lambda s: -(s.end_line - s.start_line)):
        for ln in range(max(sym.start_line, 1), min(sym.end_line, nlines) + 1):
            owner[ln] = sym.qualified_name
    for ref in refs:
        if 0 < ref.line <= nlines:
            ref.caller = owner[ref.line]


def parse_source(language: str, data: bytes) -> ParsedFile:
    """Parse ``data`` and return its symbols and reference sites.

    Raises :class:`ParserUnavailableError` if the grammar is not installed.
    """
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    tree = _get_parser(language).parse(data)
    root = tree.root_node
    symbols = _extract_python(root, data) if language == "python" else _extract_js(root, data)
    refs = _extract_refs(root, data, language)
    lines = data.decode("utf-8", "replace").split("\n")
    for ref in refs:
        if 0 < ref.line <= len(lines):
            ref.snippet = lines[ref.line - 1].strip()[:MAX_SNIPPET_CHARS]
    _attribute_callers(symbols, refs, len(lines))
    symbols.sort(key=lambda s: (s.start_line, -s.end_line))
    return ParsedFile(language, symbols, refs, len(lines))


# --------------------------------------------------------------------------- #
# SQLite storage
# --------------------------------------------------------------------------- #

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE files (
    path        TEXT PRIMARY KEY,
    language    TEXT NOT NULL,
    mtime_ns    INTEGER NOT NULL,
    size        INTEGER NOT NULL,
    sha1        TEXT NOT NULL,
    line_count  INTEGER NOT NULL,
    indexed_at  REAL NOT NULL
);

CREATE TABLE symbols (
    id              INTEGER PRIMARY KEY,
    file_path       TEXT NOT NULL,
    name            TEXT NOT NULL,
    qualified_name  TEXT NOT NULL,
    kind            TEXT NOT NULL,
    signature       TEXT NOT NULL,
    docstring       TEXT,
    start_line      INTEGER NOT NULL,
    end_line        INTEGER NOT NULL,
    parent          TEXT,
    tokens          TEXT NOT NULL
);
CREATE INDEX idx_symbols_name ON symbols(name COLLATE NOCASE);
CREATE INDEX idx_symbols_qname ON symbols(qualified_name COLLATE NOCASE);
CREATE INDEX idx_symbols_file ON symbols(file_path, start_line);

CREATE TABLE refs (
    id         INTEGER PRIMARY KEY,
    file_path  TEXT NOT NULL,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    line       INTEGER NOT NULL,
    caller     TEXT,
    snippet    TEXT
);
CREATE INDEX idx_refs_name ON refs(name);
CREATE INDEX idx_refs_file ON refs(file_path);

CREATE VIRTUAL TABLE symbols_fts USING fts5(
    name, qualified_name, signature, tokens,
    content='symbols', content_rowid='id'
);

CREATE TRIGGER symbols_ai AFTER INSERT ON symbols BEGIN
    INSERT INTO symbols_fts(rowid, name, qualified_name, signature, tokens)
    VALUES (new.id, new.name, new.qualified_name, new.signature, new.tokens);
END;
CREATE TRIGGER symbols_ad AFTER DELETE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name, signature, tokens)
    VALUES ('delete', old.id, old.name, old.qualified_name, old.signature, old.tokens);
END;
"""

_DROP_ALL = """
DROP TRIGGER IF EXISTS symbols_ai;
DROP TRIGGER IF EXISTS symbols_ad;
DROP TABLE IF EXISTS symbols_fts;
DROP TABLE IF EXISTS refs;
DROP TABLE IF EXISTS symbols;
DROP TABLE IF EXISTS files;
DROP TABLE IF EXISTS meta;
"""


class ProjectIndex:
    """A project's symbol graph, backed by SQLite.

    The instance is thread-safe: a single connection is shared and guarded by a
    re-entrant lock.  Parsing happens outside the lock so a background watcher
    never blocks queries for longer than one file's write.

    Args:
        root: Project directory to index.
        db_path: Database location.  Defaults to
            ``<root>/.zerotoken/zerotoken.db``.
    """

    def __init__(self, root: str | os.PathLike, db_path: str | os.PathLike | None = None):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise NotADirectoryError(f"not a directory: {self.root}")
        if db_path:
            self.db_path = Path(db_path).expanduser().resolve()
        else:
            self.db_path = self.root / DB_DIR_NAME / DB_FILE_NAME
            self._prepare_default_db_dir()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._warned: set[str] = set()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    # -- setup ------------------------------------------------------------- #

    def _prepare_default_db_dir(self) -> None:
        d = self.db_path.parent
        d.mkdir(parents=True, exist_ok=True)
        gi = d / ".gitignore"
        if not gi.exists():
            try:
                gi.write_text("*\n", encoding="utf-8")  # keep the index out of git
            except OSError:
                pass

    def _init_db(self) -> None:
        with self._lock:
            c = self._conn
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            version = c.execute("PRAGMA user_version").fetchone()[0]
            stored_root = None
            if version == SCHEMA_VERSION:
                try:
                    row = c.execute("SELECT value FROM meta WHERE key='root'").fetchone()
                    stored_root = row[0] if row else None
                except sqlite3.DatabaseError:
                    stored_root = None
            if version != SCHEMA_VERSION or stored_root != str(self.root):
                self._reset_schema()

    def _reset_schema(self) -> None:
        c = self._conn
        try:
            c.executescript(_DROP_ALL + _SCHEMA)
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).lower():
                raise RuntimeError(
                    "This Python's SQLite build was compiled without FTS5, which ZeroToken requires."
                ) from exc
            raise
        c.execute("INSERT INTO meta(key, value) VALUES ('root', ?)", (str(self.root),))
        c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        c.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- path handling ------------------------------------------------------ #

    def classify(self, path: str | os.PathLike) -> Optional[tuple[str, str]]:
        """Return ``(relative_posix_path, language)`` if ``path`` is indexable."""
        p = Path(path)
        abs_path = p if p.is_absolute() else self.root / p
        try:
            parts = abs_path.relative_to(self.root).parts
        except ValueError:
            return None
        if not parts:
            return None
        if any(part in IGNORED_DIRS or part.endswith(".egg-info") for part in parts[:-1]):
            return None
        name = parts[-1]
        lower = name.lower()
        if lower.startswith(".env") or lower.endswith(".min.js"):
            return None
        language = EXTENSION_LANGUAGE.get(Path(lower).suffix)
        if language is None:
            return None
        return "/".join(parts), language

    def is_ignored_dir(self, path: str | os.PathLike) -> bool:
        """True if ``path`` is outside the root or inside/equal to an ignored directory."""
        p = Path(path)
        abs_path = p if p.is_absolute() else self.root / p
        try:
            parts = abs_path.relative_to(self.root).parts
        except ValueError:
            return True
        return any(part in IGNORED_DIRS or part.endswith(".egg-info") for part in parts)

    def resolve_rel(self, path: str) -> str:
        """Map a user supplied (relative or absolute) path to a root-relative POSIX path."""
        p = Path(path).expanduser()
        abs_path = (p if p.is_absolute() else self.root / p).resolve()
        try:
            return abs_path.relative_to(self.root).as_posix()
        except ValueError:
            raise ValueError(f"path is outside the project root: {path}") from None

    def iter_source_files(self) -> Iterator[tuple[Path, str, str]]:
        """Yield ``(absolute_path, relative_path, language)`` for every indexable file."""
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = sorted(
                d for d in dirnames if d not in IGNORED_DIRS and not d.endswith(".egg-info")
            )
            for fname in sorted(filenames):
                abs_path = Path(dirpath) / fname
                info = self.classify(abs_path)
                if info is not None:
                    yield abs_path, info[0], info[1]

    # -- indexing ------------------------------------------------------------ #

    def _file_row(self, rel: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT * FROM files WHERE path=?", (rel,)).fetchone()

    def _delete_rows(self, rel: str) -> None:
        # Explicit deletes (no FK cascade) so the FTS triggers always fire.
        self._conn.execute("DELETE FROM symbols WHERE file_path=?", (rel,))
        self._conn.execute("DELETE FROM refs WHERE file_path=?", (rel,))
        self._conn.execute("DELETE FROM files WHERE path=?", (rel,))

    def _index_one(self, abs_path: Path, rel: str, language: str, force: bool = False) -> bool:
        """(Re)index one file. Returns True if the stored rows changed."""
        try:
            st = abs_path.stat()
        except OSError:
            return self.remove_file(rel)
        if st.st_size > MAX_FILE_BYTES:
            log.info("skipping %s (%d bytes > limit)", rel, st.st_size)
            return self.remove_file(rel)
        try:
            data = abs_path.read_bytes()
        except OSError as exc:
            log.warning("cannot read %s: %s", rel, exc)
            return False
        if b"\0" in data[:8192]:  # binary masquerading as source
            return self.remove_file(rel)
        sha = hashlib.sha1(data).hexdigest()

        if not force:
            row = self._file_row(rel)
            if row is not None and row["sha1"] == sha:
                with self._lock, self._conn:
                    self._conn.execute(
                        "UPDATE files SET mtime_ns=?, size=? WHERE path=?",
                        (st.st_mtime_ns, st.st_size, rel),
                    )
                return False

        try:
            parsed = parse_source(language, data)
        except ParserUnavailableError as exc:
            if language not in self._warned:
                self._warned.add(language)
                log.warning("skipping %s files: %s", language, exc)
            return False
        except Exception:
            log.exception("failed to parse %s", rel)
            return False

        sym_rows = [
            (rel, s.name, s.qualified_name, s.kind, s.signature, s.docstring,
             s.start_line, s.end_line, s.parent, " ".join(split_identifier(s.qualified_name)))
            for s in parsed.symbols
        ]
        ref_rows = [(rel, r.name, r.kind, r.line, r.caller, r.snippet) for r in parsed.refs]
        with self._lock, self._conn:
            self._delete_rows(rel)
            self._conn.execute(
                "INSERT INTO files(path, language, mtime_ns, size, sha1, line_count, indexed_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (rel, language, st.st_mtime_ns, st.st_size, sha, parsed.line_count, time.time()),
            )
            self._conn.executemany(
                "INSERT INTO symbols(file_path, name, qualified_name, kind, signature, docstring, "
                "start_line, end_line, parent, tokens) VALUES (?,?,?,?,?,?,?,?,?,?)",
                sym_rows,
            )
            self._conn.executemany(
                "INSERT INTO refs(file_path, name, kind, line, caller, snippet) VALUES (?,?,?,?,?,?)",
                ref_rows,
            )
        return True

    def index_directory(
        self,
        force: bool = False,
        progress: Optional[Callable[[int, str], None]] = None,
    ) -> dict[str, int]:
        """Walk the project and (re)index whatever changed.

        With ``force=False`` unchanged files (same mtime and size) are skipped,
        files that disappeared are purged.  ``force=True`` re-parses everything.
        """
        stats = {"scanned": 0, "indexed": 0, "unchanged": 0, "removed": 0}
        seen: set[str] = set()
        for abs_path, rel, language in self.iter_source_files():
            seen.add(rel)
            stats["scanned"] += 1
            if progress:
                progress(stats["scanned"], rel)
            if not force:
                row = self._file_row(rel)
                try:
                    st = abs_path.stat()
                except OSError:
                    continue
                if row and row["mtime_ns"] == st.st_mtime_ns and row["size"] == st.st_size:
                    stats["unchanged"] += 1
                    continue
            if self._index_one(abs_path, rel, language, force=force):
                stats["indexed"] += 1
            else:
                stats["unchanged"] += 1
        with self._lock:
            known = [r["path"] for r in self._conn.execute("SELECT path FROM files")]
        for rel in known:
            if rel not in seen and self.remove_file(rel):
                stats["removed"] += 1
        return stats

    def update_file(self, path: str | os.PathLike) -> bool:
        """Incrementally refresh a single file (index, re-index or purge it)."""
        info = self.classify(path)
        if info is None:
            return False
        rel, language = info
        abs_path = self.root / rel
        if not abs_path.exists():
            return self.remove_file(rel)
        return self._index_one(abs_path, rel, language)

    def remove_file(self, rel: str) -> bool:
        """Purge a file's rows. Returns True if anything was stored for it."""
        with self._lock, self._conn:
            existed = self._conn.execute("SELECT 1 FROM files WHERE path=?", (rel,)).fetchone()
            self._delete_rows(rel)
        return existed is not None

    def remove_prefix(self, path: str | os.PathLike) -> int:
        """Purge every indexed file under a (deleted/moved) directory."""
        p = Path(path)
        abs_path = p if p.is_absolute() else self.root / p
        try:
            rel = abs_path.relative_to(self.root).as_posix()
        except ValueError:
            return 0
        if rel in ("", "."):
            return 0
        pattern = _escape_like(rel) + "/%"
        with self._lock:
            paths = [
                r["path"]
                for r in self._conn.execute("SELECT path FROM files WHERE path LIKE ? ESCAPE '\\'", (pattern,))
            ]
        return sum(1 for rel_path in paths if self.remove_file(rel_path))

    def ensure_fresh(self, rel: str) -> bool:
        """Re-index ``rel`` if it changed on disk since it was indexed.

        Lets definition/outline lookups return correct line ranges even when no
        file watcher is running.  Returns True if the index was modified.
        """
        row = self._file_row(rel)
        abs_path = self.root / rel
        try:
            st = abs_path.stat()
        except OSError:
            return self.remove_file(rel) if row else False
        if row is not None and row["mtime_ns"] == st.st_mtime_ns and row["size"] == st.st_size:
            return False
        return self.update_file(abs_path)

    # -- queries --------------------------------------------------------------- #

    def search_symbols(self, query: str, kind: Optional[str] = None, limit: int = 20) -> list[dict]:
        """Fuzzy symbol search: FTS5 prefix AND, then OR, then substring fallback."""
        q = (query or "").strip()
        kind = normalize_kind(kind)
        if not q:
            return []
        kind_sql = " AND s.kind = ?" if kind else ""
        kind_args: tuple = (kind,) if kind else ()
        with self._lock:
            rows: list[sqlite3.Row] = []
            modes = ("AND", "OR") if len(split_identifier(q)) > 1 else ("AND",)
            for mode in modes:
                match = _fts_query(q, mode)
                if match is None:
                    break
                try:
                    rows = self._conn.execute(
                        "SELECT s.* FROM symbols_fts JOIN symbols s ON s.id = symbols_fts.rowid "
                        f"WHERE symbols_fts MATCH ?{kind_sql} "
                        "ORDER BY (s.name = ? COLLATE NOCASE) DESC, "
                        "(s.qualified_name = ? COLLATE NOCASE) DESC, "
                        "bm25(symbols_fts, 10.0, 6.0, 2.0, 4.0) LIMIT ?",
                        (match, *kind_args, q, q, limit),
                    ).fetchall()
                except sqlite3.OperationalError as exc:
                    log.debug("fts query failed (%s): %s", match, exc)
                    rows = []
                if rows:
                    break
            if not rows:
                like = f"%{_escape_like(q)}%"
                rows = self._conn.execute(
                    "SELECT s.* FROM symbols s WHERE (s.name LIKE ? ESCAPE '\\' "
                    f"OR s.qualified_name LIKE ? ESCAPE '\\'){kind_sql} "
                    "ORDER BY length(s.name), s.file_path, s.start_line LIMIT ?",
                    (like, like, *kind_args, limit),
                ).fetchall()
        return [dict(r) for r in rows]

    def _lookup(self, name: str, rel: Optional[str]) -> list[dict]:
        with self._lock:
            for collate in ("", " COLLATE NOCASE"):
                sql = f"SELECT * FROM symbols WHERE (name = ?{collate} OR qualified_name = ?{collate})"
                args: list = [name, name]
                if rel:
                    sql += " AND file_path = ?"
                    args.append(rel)
                sql += " ORDER BY file_path, start_line"
                rows = self._conn.execute(sql, args).fetchall()
                if rows:
                    return [dict(r) for r in rows]
        return []

    def find_symbols(self, name: str, file_path: Optional[str] = None) -> list[dict]:
        """Exact (then case-insensitive) lookup by name or ``Class.method`` name.

        Files involved are refreshed first if they changed on disk, so the
        returned line ranges match the current file contents.
        """
        rel = self.resolve_rel(file_path) if file_path else None
        rows = self._lookup(name, rel)
        refreshed = False
        for r in {row["file_path"] for row in rows} or ({rel} if rel else set()):
            refreshed |= self.ensure_fresh(r)
        return self._lookup(name, rel) if refreshed else rows

    def get_outline(self, file_path: str) -> Optional[tuple[dict, list[dict]]]:
        """Return ``(file_row, symbols_in_order)`` or None if the file isn't indexed."""
        rel = self.resolve_rel(file_path)
        self.ensure_fresh(rel)
        with self._lock:
            frow = self._file_row(rel)
            if frow is None:
                return None
            syms = self._conn.execute(
                "SELECT * FROM symbols WHERE file_path=? ORDER BY start_line, end_line DESC, id", (rel,)
            ).fetchall()
        return dict(frow), [dict(s) for s in syms]

    def find_references(self, name: str, limit: int = 100) -> tuple[list[dict], int]:
        """Return ``(rows, total)`` of reference sites whose callee name is ``name``."""
        bare = name.strip().split(".")[-1]
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM refs WHERE name=?", (bare,)).fetchone()[0]
            rows = self._conn.execute(
                "SELECT * FROM refs WHERE name=? ORDER BY file_path, line LIMIT ?", (bare, limit)
            ).fetchall()
        return [dict(r) for r in rows], total

    def read_lines(self, rel: str, start: int, end: int) -> list[str]:
        """Read 1-based inclusive lines ``start..end`` of a project file."""
        text = (self.root / rel).read_bytes().decode("utf-8", "replace")
        return [ln.rstrip("\r") for ln in text.split("\n")[max(start - 1, 0):end]]

    def stats(self) -> dict[str, int]:
        with self._lock:
            c = self._conn
            return {
                "files": c.execute("SELECT COUNT(*) FROM files").fetchone()[0],
                "symbols": c.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
                "references": c.execute("SELECT COUNT(*) FROM refs").fetchone()[0],
            }
