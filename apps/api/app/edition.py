"""Which edition this tree is: the cloud console (this monorepo) or the single-user community edition.

A literal, never a knob: no env var, setting, build arg or flag selects the edition or moves a cap (PRD §7).
Only the mirror build sets it — `tools/community_edition/build_tree.py` writes the manifest's `[mirror] edition`
into the `EDITION` line below in the public tree; it is one of `packages/editions/features.toml`'s `editions`.
Guard a cloud-only feature by its name (`app/edition_features.py`), never by `IS_CE`, which marks the
single-user package's edition-wide traits. Read it at call time (`from app import edition` then
`edition.IS_CE`), never `from app.edition import IS_CE`, so a test can monkeypatch it; the community-edition
tests forbid the from-import. Import-free: it is a rewrite target, so an `app.*` import here would widen the
public tree's import closure.
"""

EDITION: str = "ce"
IS_CE: bool = EDITION == "ce"

#: The community edition's caps: AI provider accounts in total (any state or provider), concurrent agent sessions.
CE_MAX_ACCOUNTS = 3
CE_MAX_AGENTS = 3

#: Where a CE user is pointed to for the cloud product (single home of this URL on the API side).
CLOUD_SIGNUP_URL = "https://app.jaah.ai/signup"
