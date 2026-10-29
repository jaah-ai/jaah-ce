"""Node roster CRUD + live-presence merge — one module per domain, each with its own `APIRouter` (`app/main.py`
includes them in a fixed order).

Membership is declared here: `POST /api/nodes` is the only thing that creates a row; self-report paths
are update-only (`app/node_identity.is_registered_node`). Presence (`app/node_presence.py`: a live
socket row, written by the peer holding the socket) is liveness, independent on purpose: `GET /api/nodes`
unions roster and presence (keeping `{node_id, status, last_seen}` for `useNodeStatus`), so an
unregistered heartbeating node shows as present but not a member. The tri-state is derived in
`jaah_discovery.status_and_last_seen`: `online` is a live socket, `offline` a row the fleet has seen
that holds no socket now, `unknown` a row never seen on the bus.

Grantable routes are `nodes:view/edit/delete` (`app/authz/`); the four that mint a credential or
enrol a machine are `@org_admin(Tenancy.CALLER_PLANE)` (G4): an Org Owner/Admin manages
their own org's nodes, the operator every org's. Generation and host-metrics
fields are derived (`node_liveness.place_generation`, `app/node_host_metrics.py`), never re-derived.

Module map (imports run top to bottom; nothing imports upward):
- `node_wire.py` — the shared wire: roster schemas, `_node_out`, `_narrow_for_tenant`, the launch-error mapping.
- `intents.py` — `GET /api/nodes/intents`.
- `reads.py` — `GET /api/nodes`, `/api/nodes/page`, `/api/nodes/{node_id}`; presence and count lookups.
- `health.py` — `GET /api/nodes/{node_id}/health`.
- `journal.py` — `GET /api/nodes/{node_id}/journal`: the node's shipped journal (operator only).
- `metrics.py` — `GET /api/nodes/{node_id}/metrics` and `/disk-events`.
- `update.py` — `PUT /api/nodes/{node_id}`, its field guards, and lifting a transfer fence.
- `register.py` — `POST /api/nodes`, `_registered_node`, token re-issue, tailnet re-enrolment.
- `cloud.py` — `POST /api/nodes/cloud` and `/api/nodes/self-hosted`; the Mac Full Disk Access profile.
- `lifecycle.py` — `POST /api/nodes/{node_id}/cloud/stop|start`, `PUT …/cloud/instance-type|disk` (Your Cloud).
- `delete.py` — `DELETE /api/nodes/{node_id}` and `delete_node_row`."""
