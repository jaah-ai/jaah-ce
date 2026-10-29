"""The console relay — the service that carries interactive console bytes between a browser and a node.

THE SPLIT. The API sets a console session up and then steps out of its data path; this
package's server (`server.py`, run as its own ECS service by `main.py`) carries the bytes. That is what
keeps terminal echo off the API's event loop (an API under CPU load no longer delays a keystroke) and
what lets an API deploy roll without dropping a single open console.

  1. SETUP — the API (`app/routes/console.py::console_open`, `POST /api/console/{project_id}/{number}/open`):
     auth, the read-only/editable tier, the create budget, the account-key lease, `console_open` over the
     node bus, and the slot claim. It mints one single-use ticket PER LEG into a `console_tickets` row
     (`tickets.mint`) and answers the browser with the relay URL carrying the browser's ticket.
  2. THE NODE LEG — on `console_open` the node dials `NODE_PATH` with the node ticket, its `NODE_TOKEN`
     and its id (`apps/jaahd/console.py::_dial_cp`). On ECS the ALB routes that path to the relay; on a
     local stack the frame's `stream_url` names the relay container (`CONSOLE_RELAY_NODE_URL`).
  3. THE BROWSER LEG — the pane dials `BROWSER_PATH?ticket=…` (`apps/web/src/components/ConsolePane.tsx`).
  4. PAIRING — each leg CLAIMS its half of the row in one UPDATE (`tickets.claim`): single-use, before
     the deadline, node-bound for the node leg. The first claim names the relay task that will hold the
     rendezvous (`owner_address`); a leg that lands on any other relay task is proxied there over
     `OWNER_LEG_PATH`, close codes included. The relay runs ONE task by default, so a proxy is the
     exception — it exists for when autoscaling has added tasks.
  5. THE SESSION — the owner pumps bytes both ways (`server._pump`), discarding browser frames when the
     row is `read_only`. Every `tickets.SEEN_INTERVAL_SECONDS` it touches the row and reads `revoked`,
     which the API's sweep sets when the viewer's login session ends (`tickets.sweep`); a revoked viewer
     is closed with 4401. The row is deleted when the session ends.

The relay's only state is the `console_tickets` table. It also READS the node credential tables, to check
the node leg's `NODE_TOKEN` (`app/node_bus.py::resolve_caller_async`).

Close codes the browser keys its reconnect on: 1000 = the session itself ended (the node closed 4000),
4401 = the viewer's session was revoked, 4503 = the relay's own transport broke — reconnect. A relay
restart closes every leg with uvicorn's 1012, which the pane reads as recoverable and re-opens through
the API. No hop negotiates WebSocket compression: PTY bytes do not compress profitably, and a stateful
deflate context serializes every frame.
"""
