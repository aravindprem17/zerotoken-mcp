# ZeroToken-MCP

**A local, zero-token semantic graph of your codebase, served to AI coding assistants over MCP.**

AI assistants usually answer "where is `authenticate` defined and who calls it?" by opening whole files. That burns tens of thousands of context tokens on code the model never needed. ZeroToken-MCP parses your project once with [tree-sitter](https://tree-sitter.github.io/), stores the symbol graph in an embedded SQLite database, and lets the assistant ask for exactly the slice it needs.

- **Local.** Everything runs on your machine over stdio. No network, no API keys, no telemetry.
- **Zero tokens to index.** Parsing and search are plain code. You only spend tokens on the small answers the assistant asks for.
- **Always current.** `--watch` re-parses a file the moment you save it. Even without it, the server re-checks a file's mtime before returning a definition or outline.
- **Fast.** Incremental indexing (unchanged files are skipped by mtime/size/hash) and SQLite FTS5 search.

## Why it saves tokens

| Task | Naive approach | With ZeroToken |
|---|---|---|
| "What's in `auth/service.py`?" | Read the file (whole body, every method) | `get_file_outline`: signatures + line ranges only |
| "Show me `AuthService.login`" | Read the file | `get_symbol_definition`: just those lines |
| "Where is `login` used?" | grep + read each matching file | `find_callers`: `file:line [in caller]: snippet` |
| "Find the config parser" | grep, open candidates | `search_symbols "parse config"`: ranked hits |

> **About the numbers.** The headline figure for a typical inspection task is on the order of **~200 tokens vs ~30,000 tokens**: a handful of tool responses (a ranked search, one outline, one definition, one caller list, each a few dozen lines) versus reading several large source files in full. That is an *illustrative scenario*, not a universal benchmark: savings depend on file sizes and what you're doing. Measure your own repo: a rough estimate is `characters / 4`, e.g. `wc -c file.py` versus the length of the tool output.

## 30-second quickstart

```bash
git clone https://github.com/<you>/zerotoken-mcp && cd zerotoken-mcp
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e .

zerotoken index /path/to/your/project                  # optional: pre-build the index
zerotoken serve --path /path/to/your/project --watch   # what your MCP client will run
```

Then add the server to your client (below) and ask it something like *"Use zerotoken to find where `parse_config` is defined and who calls it."*

Requirements: Python 3.10+, an SQLite build with FTS5 (standard in python.org, Homebrew, Debian/Ubuntu and Windows builds).

## Client configuration

Use an **absolute path** to the executable if your client doesn't inherit your venv (find it with `which zerotoken`, or `where zerotoken` on Windows). You can also use `"command": "/abs/path/.venv/bin/python", "args": ["-m", "zerotoken", "serve", ...]`.

### Claude Desktop: `claude_desktop_config.json`

macOS: `~/Library/Application Support/Claude/claude_desktop_config.json` · Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "zerotoken": {
      "command": "/abs/path/to/.venv/bin/zerotoken",
      "args": ["serve", "--path", "/abs/path/to/your/project", "--watch"]
    }
  }
}
```

Restart Claude Desktop afterwards.

### Cursor: `.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global)

```json
{
  "mcpServers": {
    "zerotoken": {
      "command": "zerotoken",
      "args": ["serve", "--path", "${workspaceFolder}", "--watch"]
    }
  }
}
```

### VS Code: `.vscode/mcp.json`

```json
{
  "servers": {
    "zerotoken": {
      "type": "stdio",
      "command": "zerotoken",
      "args": ["serve", "--path", "${workspaceFolder}", "--watch"]
    }
  }
}
```

### Claude Code

```bash
claude mcp add zerotoken -- zerotoken serve --path . --watch
```

## Tools

| Tool | Purpose |
|---|---|
| `search_symbols(query, kind=None, limit=20)` | Fuzzy FTS search over symbol names and signatures. Splits `camelCase`/`snake_case`, matches prefixes. `kind`: `function`, `class`, `method`, `interface`, `type`, `enum`. |
| `get_symbol_definition(symbol_name, file_path=None)` | The exact source lines of one definition. Accepts `Class.method`. If the name is ambiguous it lists candidates instead of guessing. |
| `get_file_outline(file_path)` | Classes, functions and method signatures with line ranges and one-line docs, no bodies. |
| `find_callers(symbol_name)` | Call sites and references (calls, `new`, decorators, JSX components, inheritance) with the enclosing function and the source line. |

Example `get_file_outline` output:

```
pkg/auth.py (python, 120 lines, 4 symbols)
class AuthService(Base)  [10-90]  # Handles login and sessions
  def login(self, user, pw) -> bool  [20-40]
  async def refresh(self, token)  [42-60]
def hash_password(pw)  [92-100]
```

## CLI

```
zerotoken index <path> [--db FILE] [--full] [-v]
zerotoken serve [--path PATH] [--watch] [--db FILE]
```

- The database lives at `<project>/.zerotoken/zerotoken.db` by default (the folder ships its own `.gitignore`).
- `serve` always runs a background incremental sync on startup, so a stale database is repaired without blocking the MCP handshake. Tools answer immediately and note when indexing is still in progress.
- `--watch` keeps the index live using `watchdog`, with debounced, per-file re-parsing.

## What gets indexed

- **Languages:** Python (`.py`), JavaScript (`.js .jsx .mjs .cjs`), TypeScript (`.ts .tsx`).
- **Symbols:** functions, classes, methods, arrow/function-expression bindings (`const f = () => {}`), class-field arrow methods, TS interfaces, type aliases and enums. Each stores signature, docstring/JSDoc, file, start and end line.
- **Ignored:** `.git`, `node_modules`, `__pycache__`, `.venv`/`venv`, `.env` (dir or file), `dist`, `build`, caches, `*.min.js`, files over 2 MB, and anything containing NUL bytes.

## Known limitations

- `find_callers` matches **by name**, not by resolved type. Two unrelated `save()` methods share results; each hit shows its enclosing function and source line to help you tell them apart.
- Functions nested inside other functions are not indexed (their calls are still attributed to the enclosing function).
- Dynamic dispatch, `getattr`, and string-based imports are invisible to a syntax-only index.
- Only Python and JS/TS today. Adding a language means adding an extractor in `indexer.py` and an entry in `EXTENSION_LANGUAGE`.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The extractor walks tree-sitter nodes directly instead of using `Query` objects, because the query API changed across tree-sitter 0.22 to 0.25 while the node API did not. That keeps one code path working over the whole supported version range.

## Layout

```
zerotoken/
  indexer.py   tree-sitter extraction + SQLite/FTS5 store + incremental updates
  server.py    FastMCP stdio server and the four tools
  watcher.py   debounced watchdog thread for live re-indexing
  cli.py       `zerotoken index` / `zerotoken serve`
tests/         pytest suite
```

## License

MIT
