"""Tests for extraction and indexing. Require the tree-sitter grammars to be installed."""

import time

import pytest

pytest.importorskip("tree_sitter")

from zerotoken.indexer import ProjectIndex, parse_source  # noqa: E402

PY_SRC = '''\
import os


@decorator
def top_level(a, b=1) -> int:
    """Add things.

    Longer text.
    """
    return helper(a) + b


class Greeter(Base):
    """Says hello."""

    def hello(self, name):
        return self.format(name)

    async def ahello(self):
        pass


def helper(x):
    return x
'''

JS_SRC = '''\
/** Adds numbers. */
export function add(a, b) {
  return a + b;
}

export const double = (x) => x * 2;

class Counter extends Base {
  inc() { return add(1, 2); }
  handler = () => { this.inc(); };
}

function App() {
  return <Widget name="x" />;
}
'''

TS_SRC = '''\
export interface User { id: number }
export type Id = string | number;
export enum Color { Red, Green }

export abstract class Repo<T> implements Store {
  /** Fetch one. */
  async get(id: Id): Promise<T> { return load(id); }
}

export const make = (n: number): number => n + 1;
'''


def by_name(parsed):
    return {s.qualified_name: s for s in parsed.symbols}


def test_python_symbols():
    p = parse_source("python", PY_SRC.encode())
    s = by_name(p)
    assert set(s) == {"top_level", "Greeter", "Greeter.hello", "Greeter.ahello", "helper"}
    assert s["top_level"].kind == "function"
    assert s["top_level"].signature == "def top_level(a, b=1) -> int"
    assert s["top_level"].docstring.startswith("Add things.")
    assert s["top_level"].start_line == 4  # includes the decorator line
    assert s["Greeter"].kind == "class" and s["Greeter"].signature == "class Greeter(Base)"
    assert s["Greeter.hello"].kind == "method" and s["Greeter.hello"].parent == "Greeter"
    assert s["Greeter.ahello"].signature == "async def ahello(self)"
    assert s["helper"].end_line == len(PY_SRC.rstrip("\n").split("\n"))


def test_python_refs_and_callers():
    p = parse_source("python", PY_SRC.encode())
    calls = {(r.name, r.kind, r.caller) for r in p.refs}
    assert ("helper", "call", "top_level") in calls
    assert ("format", "call", "Greeter.hello") in calls
    assert ("decorator", "decorator", "top_level") in calls
    assert ("Base", "inherit", "Greeter") in calls


def test_javascript_symbols_and_refs():
    p = parse_source("javascript", JS_SRC.encode())
    s = by_name(p)
    assert {"add", "double", "Counter", "Counter.inc", "Counter.handler", "App"} <= set(s)
    assert s["add"].docstring == "Adds numbers."
    assert s["add"].signature == "function add(a, b)"
    assert s["double"].signature == "const double = (x) =>"
    assert s["Counter.inc"].kind == "method"
    refs = {(r.name, r.kind) for r in p.refs}
    assert ("add", "call") in refs and ("Base", "inherit") in refs


def test_tsx_jsx_component_reference():
    p = parse_source("tsx", JS_SRC.encode())
    assert ("Widget", "jsx") in {(r.name, r.kind) for r in p.refs}


def test_typescript_symbols():
    p = parse_source("typescript", TS_SRC.encode())
    s = by_name(p)
    assert s["User"].kind == "interface"
    assert s["Id"].kind == "type"
    assert s["Color"].kind == "enum"
    assert s["Repo"].kind == "class" and s["Repo.get"].kind == "method"
    assert s["Repo.get"].docstring == "Fetch one."
    assert s["make"].kind == "function"
    assert ("Store", "inherit") in {(r.name, r.kind) for r in p.refs}


@pytest.fixture()
def project(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(PY_SRC)
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "app.ts").write_text(TS_SRC)
    (tmp_path / "node_modules" / "dep").mkdir(parents=True)
    (tmp_path / "node_modules" / "dep" / "x.js").write_text("function vendored() {}\n")
    (tmp_path / ".env").write_text("SECRET=1\n")
    return tmp_path


def test_index_skips_ignored_and_searches(project):
    idx = ProjectIndex(project)
    stats = idx.index_directory()
    assert stats["indexed"] == 2
    assert idx.search_symbols("vendored") == []
    top = idx.search_symbols("greeter hello")[0]
    assert top["qualified_name"] == "Greeter.hello"
    assert idx.search_symbols("get", kind="method")[0]["qualified_name"] == "Repo.get"
    idx.close()


def test_definition_slice_and_outline(project):
    idx = ProjectIndex(project)
    idx.index_directory()
    (m,) = idx.find_symbols("Greeter.hello")
    lines = idx.read_lines(m["file_path"], m["start_line"], m["end_line"])
    assert lines[0].strip() == "def hello(self, name):"
    frow, syms = idx.get_outline("pkg/mod.py")
    assert [s["qualified_name"] for s in syms] == [
        "top_level", "Greeter", "Greeter.hello", "Greeter.ahello", "helper",
    ]
    idx.close()


def test_find_references(project):
    idx = ProjectIndex(project)
    idx.index_directory()
    rows, total = idx.find_references("helper")
    assert total == 1 and rows[0]["caller"] == "top_level"
    idx.close()


def test_incremental_update_and_delete(project):
    idx = ProjectIndex(project)
    idx.index_directory()
    f = project / "pkg" / "mod.py"
    time.sleep(0.01)
    f.write_text("def only_one():\n    pass\n")
    assert idx.update_file(f) is True
    assert idx.search_symbols("helper") == []
    assert idx.search_symbols("only_one")[0]["name"] == "only_one"
    f.unlink()
    assert idx.update_file(f) is True
    assert idx.search_symbols("only_one") == []
    idx.close()


def test_stale_file_is_refreshed_on_lookup(project):
    idx = ProjectIndex(project)
    idx.index_directory()
    f = project / "pkg" / "mod.py"
    time.sleep(0.01)
    f.write_text("# pad\n# pad\n" + PY_SRC)
    (m,) = idx.find_symbols("Greeter.hello")
    assert m["start_line"] == 18  # shifted by the two padding lines
    idx.close()


def test_path_escape_rejected(project):
    idx = ProjectIndex(project)
    with pytest.raises(ValueError):
        idx.resolve_rel("../outside.py")
    idx.close()
