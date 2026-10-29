"""Which edition this node runs in: the cloud fleet or the single-user community edition. Stdlib only.

A deliberate second copy of `apps/api/app/edition.py` (minus the sign-up URL): the node refuses over-cap work
on its own, independent of the control plane, so it cannot import the API's constants. A literal, never a
knob — only the mirror build sets `EDITION` (`tools/community_edition/build_tree.py`, from the manifest's
`[mirror] edition`). Guard a cloud-only feature by its name (`edition_features.py`), never by `IS_CE`. Read it at
call time (`import edition` then `edition.IS_CE`), never `from edition import IS_CE`
(a repo test forbids it: a from-import binds the value at import, so a test's monkeypatch never reaches it).
"""

EDITION: str = "ce"
IS_CE: bool = EDITION == "ce"

CE_MAX_ACCOUNTS = 3
CE_MAX_AGENTS = 3
