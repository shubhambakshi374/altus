"""The tree-sitter extractors, behind the ``repomap`` extra.

Skipped as a whole when the grammars are not installed --- the same shape as
the Kubernetes and cloud suites, for the same reason: a missing extra is a
configuration, not a failure. What must *not* be skipped is the absence
itself, so ``test_repomap.py`` covers the no-extractor path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from altus.repomap.index import reconcile
from altus.repomap.languages import EXTENSIONS, extractor_for, language_of
from altus.repomap.store import store_for
from altus.workspace import Workspace

pytest.importorskip("tree_sitter", reason="the repomap extra is not installed")

GO = """package main

import (
\t"fmt"
\t"github.com/foo/bar"
)

// Hello greets somebody.
func Hello(name string) error { return nil }

type Server struct{ port int }

func (s *Server) Start() error { return nil }

func unexported() {}
"""

TYPESCRIPT = """import { thing } from "./util";
import fs from "node:fs";

export function makeWidget(size: number): Widget {
  return null;
}

export class Store {
  get(key: string) { return 1; }
}

interface Shape { area(): number }
"""

RUST = """use std::collections::HashMap;
use crate::engine::Driver;

pub fn run(count: usize) -> Result<(), Error> { Ok(()) }

pub struct Config { pub name: String }

impl Config {
    pub fn new() -> Self { Config { name: String::new() } }
}

pub trait Sink { fn emit(&self); }
"""

RUBY = """require "sinatra"
require_relative "./helper"

# The store.
class Store
  def fetch(key)
  end
end

module Util
  def self.slug(s); end
end
"""


def _symbols(language: str, text: str, path: str) -> dict[str, tuple[str, int]]:
    found = extractor_for(language)
    assert found is not None, f"no extractor for {language}"
    extracted = found.extract(text, path)
    return {symbol.name: (symbol.kind, symbol.line) for symbol in extracted.symbols}


def _imports(language: str, text: str, path: str) -> list[tuple[str, int]]:
    found = extractor_for(language)
    assert found is not None
    return [(imp.target, imp.line) for imp in found.extract(text, path).imports]


def test_every_extension_has_an_extractor() -> None:
    """With the extra installed, nothing we claim to recognise is unparseable."""
    for language in set(EXTENSIONS.values()):
        assert extractor_for(language) is not None, language


# ------------------------------------------------------------------------- go


def test_go_methods_are_named_after_the_method_not_the_receiver() -> None:
    """``func (s *Server) Start()`` --- the receiver type comes first in the
    source, so pairing captures by containment would call this one Server."""
    symbols = _symbols("go", GO, "main.go")
    assert symbols["Start"] == ("method", 13)
    assert symbols["Server"] == ("type", 11)


def test_go_capitalisation_decides_exported() -> None:
    found = extractor_for("go")
    assert found is not None
    exported = {s.name: s.exported for s in found.extract(GO, "main.go").symbols}
    assert exported["Hello"] is True
    assert exported["unexported"] is False


def test_signatures_stop_at_the_body() -> None:
    found = extractor_for("go")
    assert found is not None
    hello = next(s for s in found.extract(GO, "main.go").symbols if s.name == "Hello")
    assert hello.signature == "func Hello(name string) error"


def test_the_comment_above_a_definition_becomes_its_doc() -> None:
    found = extractor_for("go")
    assert found is not None
    hello = next(s for s in found.extract(GO, "main.go").symbols if s.name == "Hello")
    assert hello.doc == "Hello greets somebody."


def test_go_imports_are_recorded_once_despite_nesting() -> None:
    """A Go import block matches as both declaration and spec; one import is
    one edge, or fan-in counts every grouped import twice."""
    assert _imports("go", GO, "main.go") == [("fmt", 3), ("github.com/foo/bar", 3)]


# ----------------------------------------------------------------- typescript


def test_typescript_needs_the_javascript_query_too() -> None:
    """TypeScript's own tags.scm only adds the constructs TypeScript adds, so
    alone it finds no ordinary function or class --- a silently empty map."""
    symbols = _symbols("typescript", TYPESCRIPT, "a.ts")
    assert symbols["makeWidget"][0] == "function"
    assert symbols["Store"][0] == "class"
    assert symbols["get"][0] == "method"
    assert symbols["Shape"][0] == "interface"


def test_tsx_is_its_own_dialect() -> None:
    symbols = _symbols("tsx", "export const App = () => <div/>;\n", "App.tsx")
    assert language_of("App.tsx") == "tsx"
    assert symbols  # the grammar parsed JSX rather than failing on it


def test_relative_typescript_imports_resolve_in_repo(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "util.ts").write_text("export const thing = 1;\n")
    (root / "src" / "app.ts").write_text('import { thing } from "./util";\n')

    with store_for(root) as db:
        reconcile(Workspace(root=root), store=db)
        assert db.importers_of("src/util.ts") == ["src/app.ts"]
        assert dict(db.imports_of("src/app.ts"))["./util"] == "src/util.ts"


def test_an_index_file_resolves_as_its_directory(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "index.ts").write_text("export const x = 1;\n")
    (root / "app.ts").write_text('import { x } from "./lib";\n')

    with store_for(root) as db:
        reconcile(Workspace(root=root), store=db)
        assert db.importers_of("lib/index.ts") == ["app.ts"]


# ----------------------------------------------------------------------- rust


def test_a_definition_matching_two_patterns_is_recorded_once() -> None:
    """Rust's query matches an impl function as method *and* function."""
    found = extractor_for("rust")
    assert found is not None
    rows = [s for s in found.extract(RUST, "lib.rs").symbols if s.name == "new"]
    assert len(rows) == 1
    assert rows[0].kind == "method"


def test_rust_kinds_map_onto_the_shared_vocabulary() -> None:
    symbols = _symbols("rust", RUST, "lib.rs")
    assert symbols["Config"][0] == "class"
    assert symbols["Sink"][0] == "interface"
    assert symbols["run"][0] == "function"


def test_rust_paths_are_not_rewritten_to_look_resolvable() -> None:
    assert _imports("rust", RUST, "lib.rs") == [
        ("std::collections::HashMap", 1),
        ("crate::engine::Driver", 2),
    ]


# ----------------------------------------------------------------------- ruby


def test_require_is_an_import_and_other_calls_are_not() -> None:
    assert _imports("ruby", RUBY, "app.rb") == [("sinatra", 1), ("./helper", 2)]


def test_a_bare_method_call_is_not_mistaken_for_an_import() -> None:
    assert _imports("ruby", 'puts "hello"\nrender "index"\n', "x.rb") == []


# ------------------------------------------------------------------ java, mix


def test_java_imports_come_from_scoped_identifiers() -> None:
    text = "package com.example;\n\nimport java.util.List;\n\nclass A {}\n"
    assert _imports("java", text, "A.java") == [("java.util.List", 3)]


def test_a_mixed_repository_indexes_every_language(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.go").write_text(GO)
    (root / "app.ts").write_text(TYPESCRIPT)
    (root / "lib.rs").write_text(RUST)
    (root / "svc.py").write_text("class Service:\n    pass\n")

    with store_for(root) as db:
        stats = reconcile(Workspace(root=root), store=db)
        assert stats.parsed == 4
        assert not stats.unsupported
        assert dict(db.languages()) == {"go": 1, "typescript": 1, "rust": 1, "python": 1}
        assert db.find("Start", exact=True)[0][0] == "main.go"
        assert db.find("Service", exact=True)[0][0] == "svc.py"


def test_a_file_the_grammar_cannot_parse_still_yields_what_it_found() -> None:
    """tree-sitter recovers from errors rather than raising, which is the
    behaviour wanted here: a half-written file still has definitions above
    the point where it broke."""
    found = extractor_for("go")
    assert found is not None
    extracted = found.extract("package main\n\nfunc Good() {}\n\nfunc Bad( {\n", "m.go")
    assert "Good" in {symbol.name for symbol in extracted.symbols}
