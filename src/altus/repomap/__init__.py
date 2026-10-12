"""The repo map: what is in this codebase, answered exactly rather than approximately.

Turn one of a session on an unfamiliar repository is currently a cold grep. The
model knows the root directory and nothing else, so it spends its first few
tool calls discovering that there is a ``src/`` and that the tests are in
``tests/`` --- facts that are the same every time and could have been read from
an index built once.

That is the whole of what this package does, and the restraint is deliberate.
It does not embed anything. The questions people actually ask when onboarding
--- where is this defined, who imports it, what does this package do, where is
the code that changes most --- are *structural*, and a symbol table answers
them exactly, at a cost that does not include shipping the source tree to a
provider to be vectorised. Similarity search answers a different question, and
only one of the questions above is that question.

The three pieces:

``index``   walks a workspace, parses what it recognises, and reconciles the
            result against what it stored last time. Keyed by content hash, so
            an unchanged file is never re-parsed and a changed one can never
            serve a stale answer.
``store``   SQLite under the data directory --- never inside the tree Altus is
            also editing --- one database per repository root.
``languages`` the extractors. Python through the standard library's ``ast``,
            which ships with us and is exact; everything else through
            tree-sitter, which is an optional extra, so a missing grammar is a
            visible absence rather than an import error at call time.
"""

from __future__ import annotations

from altus.repomap.index import reconcile
from altus.repomap.languages import available_languages, language_of, missing_languages
from altus.repomap.models import FileFacts, IndexStats, Symbol
from altus.repomap.store import MapStore, store_for

__all__ = [
    "FileFacts",
    "IndexStats",
    "MapStore",
    "Symbol",
    "available_languages",
    "language_of",
    "missing_languages",
    "reconcile",
    "store_for",
]
