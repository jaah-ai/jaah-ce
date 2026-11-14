"""Project MCP links: which remote MCP servers a project's sessions reach (`project_mcp_links`).

An MCP connection is a credential: an `mcp_server` row (a custom server) or a row of a catalog vendor whose spec
has an `[mcp]` block (`app/integrations/catalog.py`). Being visible to a project is not enough - an org-shared
credential is visible to every project - so a project opts in explicitly with a link (D5), and only a linked
connection reaches a session.

At dispatch, `connections_for_task` turns the task's project links into the launch contract's
`mcp_connections` (`routes/fleet.py::McpConnectionOut`): `{name, credential_path, url, transport}` per linked
connection the task's own credential roster grants (`routes/agent/task_secrets.py::_granted_credentials`, so the
grant rule is the one `credential_get` applies). No secret: the node's bridge fetches the token by
`credential_path` (`apps/jaahd/mcp_bridge.py`). Routes: `routes/credentials/mcp_links.py`.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations import catalog
from app.mcp_client import TRANSPORTS
from app.models.credentials import Credential, ProjectMcpLink

log = logging.getLogger(__name__)

#: The custom MCP server type (`credentials/registry.py`).
MCP_SERVER_TYPE = "mcp_server"

#: A connection's name on the wire: the engine sees `mcp_<name>` (`apps/jaahd/agents/base.py::McpConnection`).
NAME_PATTERN = re.compile(r"^[a-z0-9_]{1,40}$")


def is_mcp_type(type_slug: str) -> bool:
    """Whether a credential of `type_slug` is an MCP connection a project may link."""
    if type_slug == MCP_SERVER_TYPE:
        return True
    spec = catalog.spec_for_type(type_slug)
    return spec is not None and spec.mcp is not None


def connection_url_problem(url: str) -> Optional[str]:
    """Why `url` cannot ride the launch contract, or None: https with a host, and no userinfo, query or fragment
    (the node refuses the whole launch otherwise - `apps/jaahd/mcp_bridge.py::check_url`)."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return "must be https with a host"
    if parts.username or parts.password or parts.query or parts.fragment:
        return "must carry no userinfo, query or fragment"
    return None


def server_of(row: Credential) -> Optional[tuple[str, str]]:
    """`(url, transport)` of an MCP connection row, or None when it names none usable. A custom row states both; a
    catalog vendor's come from its spec, the row's own URL winning where the vendor lets it be edited."""
    config = row.config or {}
    if row.type == MCP_SERVER_TYPE:
        url = str(config.get("url") or "").strip()
        transport = str(config.get("transport") or "streamable_http").strip()
    else:
        spec = catalog.spec_for_type(row.type)
        if spec is None or spec.mcp is None:
            return None
        resolved, _problem = spec.mcp.server_url(config)
        if resolved is None:
            return None
        url, transport = resolved, spec.mcp.transport
    if transport not in TRANSPORTS or connection_url_problem(url) is not None:
        return None
    return url, transport


def connection_name(credential_name: str, taken: set[str]) -> str:
    """The wire name for a connection called `credential_name`: lowercase `[a-z0-9_]`, at most 40 characters, made
    unique among `taken` with a numeric suffix (two links may share a leaf name in different folders)."""
    base = re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", credential_name.lower())).strip("_")[:40] or "mcp"
    name, n = base, 1
    while name in taken:
        n += 1
        suffix = f"_{n}"
        name = f"{base[:40 - len(suffix)]}{suffix}"
    return name


def linked_ids(session: Session, project_id: int) -> set[Any]:
    return set(session.scalars(select(ProjectMcpLink.credential_id).where(ProjectMcpLink.project_id == project_id)))


def connections_for_task(session: Session, task: Any) -> list[dict[str, str]]:
    """The launch contract's `mcp_connections` for `task`: every connection its project linked that the task's
    roster grants, in name order. `[]` when the project links nothing (no roster is read then). A linked row whose
    server cannot ride the contract is left out and logged by id, never sent half-formed."""
    project_id = getattr(task, "project_id", None)
    if project_id is None:
        return []
    links = linked_ids(session, project_id)
    if not links:
        return []
    from app.routes.agent.task_secrets import _granted_credentials  # the one grant rule; lazy: routes import cycle

    out: list[dict[str, str]] = []
    taken: set[str] = set()
    for path, row in sorted(_granted_credentials(session, task), key=lambda pair: pair[0]):
        if row.id not in links or row.system or not is_mcp_type(row.type):
            continue
        server = server_of(row)
        if server is None:
            log.warning("mcp links: task %s skips linked credential %s - it names no usable server URL",
                        getattr(task, "id", None), row.id)
            continue
        name = connection_name(row.name, taken)
        taken.add(name)
        out.append({"name": name, "credential_path": path, "url": server[0], "transport": server[1]})
    return sorted(out, key=lambda item: item["name"])
