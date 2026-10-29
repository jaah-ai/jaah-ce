"""The single home for the authorization decision — the vocabulary, the principal, and the guard.

Superadmin is allow-all; a standard user is deny-all unless bound. `app/main.py::create_app` installs
`guard.authz_guard` as a global dependency deciding every request from its route's declaration; an
undeclared route is denied. `object_params` the guard enforces; `collection`/`lookup` are handler
obligations. `edit` never implies `view`.

`Principal` (frozen, never an ORM row) is the only principal crossing a seam. `None` is the agent
principal, allow-all and unfiltered — so an unverifiable cookie must become `RevokedSession`, never
`None`. Clauses are usable only via `scoped_*_select`/`can_access`. Access is ROLE BINDINGS
(`app/access.py`): org-wide keys (`Principal.org_keys`) plus (scope, key) triples, inheritance applied at
resolution. `created_by_id` gates only a task's author-only states and §"The author's own draft";
`owner_id` makes a credential or account personal.

Module map — import each name from the module that owns it (this package re-exports nothing):

- `catalog`: the key vocabulary — `FEATURES`, `ACTIONS`, `PERMISSION_CATALOG`, `OWNER_KEYS` — and the
  route tiers `Access`, `Tenancy`.
- `declarations`: `Declaration` and the decorators `feature`, `superadmin_only`, `org_admin`, `infra`,
  `always`; `declaration_of` reads a handler's `__authz__`.
- `principal`: `Principal`, `principal_for_user`, `current_principal`, `Caller`, `creates_org_rows`.
- `visibility`: the SQL `visible_*_clause` predicates, `ObjectSpec`/`OBJECT_SPECS`, `scoped_*_select`.
- `object_access`: `can_access`, `require_object_access`, `validated_ownership`, `stamp_owner_on_create`.
- `enforce`: `_enforce` and its denials (`HintedDenial`), `enforce_in_process`, the read-scope binding.
- `guard`: `authz_guard` and `ORG_SCOPE_HEADER`.
"""
