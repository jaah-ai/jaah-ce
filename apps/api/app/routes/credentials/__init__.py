"""Credentials CRUD — Directory > Secrets, one domain per module (each with its own router, `main.py`):

- `credential_wire` — schemas, body validators, `_validated_value`, `_scrubbed_write`, `_audit`, `_credential_out`.
- `guards` — the route keys, `_scoped_row_or_404`, `_may_edit` and every `_refuse_*` refusal.
- `reads` — the roster, `/types`, `/owners`, the seal key, NodeVault state and one row.
- `roster` — the roster's query builder: filters, scope, the capped total and keyset pages.
- `writes` — create, update, folder move, delete.
- `bulk` — delete, move to folder and change org over a picked or matching set.
- `project_pool` — attach/detach to a project, and a credential's access.
- `connection_test` — `POST /test` (`app/credentials/testers.py`).
- `connect` — the control-plane OAuth2 Connect flow and its callback.

The type registry is `app/credentials/registry.py`, a different package.

No `require_superadmin`: the `credentials:*` key reaches granted rows; any session also gets
in under `own_credential`, narrowed to rows it owns (`owner_id`, `authz.owned_credential_clause`).
All access goes through `authz.scoped_credential_select_for`, `guards._scoped_row_or_404` or
`roster.roster_scope` (read it before adding a scope); that scoping is the entire enforcement. Not widened:
`POST /test` and `/owners`, `/orgs`; connect needs `credentials:edit` for row-supplied token endpoints.

Security properties (about the decrypted `config`): (1) `_credential_out`, the one serializer, omits
secret keys; (2) pydantic validates no body value (a 422 echoes input), 400s name the key only; (3)
never calls `emit_console_event`, but does `record_write`; (4) DB errors on writes become scrubbed 500s."""
