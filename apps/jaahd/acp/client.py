"""The ACP client half: initialize, open a session, prompt, cancel, close — typed calls over `jsonrpc.Connection`.

We advertise NO filesystem and NO terminal capability: the engine runs inside the worktree with its own
tools, and serving `fs/*`/`terminal/*` would make the host a second, unfenced file and process API.
`elicitation` (form mode) is advertised because a human question is the one thing only the client can carry;
an agent that checks for `elicitation.form` asks nothing without it.
`_meta["jaah.ai"]` hands the agent-side adapter its run identity (task, session, provider, mode); a provider's
own `_meta` keys (`AcpLaunch.session_meta`, its adapter's options namespace) sit beside it at the top level, where
the agent reads them. A charter goes as the session's `systemPrompt` (append) when the agent advertises
`sessionCapabilities.systemPrompt`; otherwise the host sends it as the first prompt's head.

A run whose key route is ACP `authenticate` signs in between `initialize` and the session: the method is the first
of the agent's `authMethods` that takes an API key — one whose `_meta` carries an `api-key` entry (the spelling
ACP agents use for it, `API_KEY_META`) — unless the launch names the method (`authenticate.method_id`) — and the
key rides that request's `_meta`, never the environment.

What the agent says about itself is kept for the session-start record: `initialize`'s `agentInfo` (its name and
version — the engine's label, never the wrapper the launch runs it through) and the models a session opened with,
sensed from `configOptions` (the option whose category or id is `model`) where the agent advertises them, else the
unstable `models` field (`model_choice`).

Opening a session with a prior native ref follows D-ACP-1: `session/resume` (if the agent advertises it) ->
`session/load` (if `loadSession`) -> `session/new`. A refused resume/load is not fatal — the run continues
cold, and `resume_supported` records that it did. The native ref is always what the agent returned (or the
ref it accepted), never derived from a transcript filename.
"""
from __future__ import annotations

import logging

from acp.jsonrpc import Connection, JsonRpcError

log = logging.getLogger("jaahd.acp.client")

PROTOCOL_VERSION = 1
CLIENT_CAPABILITIES = {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False,
                       "elicitation": {"form": {}}}
CLIENT_NAME = "jaah-acp-host"
META_KEY = "jaah.ai"
#: The `_meta` key that marks an auth method as taking an API key, and carries the key in `authenticate`.
API_KEY_META = "api-key"


def agent_info(result: object) -> dict:
    """`initialize`'s `agentInfo` as `{name, version}` (title when no name); {} when the agent sent none."""
    info = result.get("agentInfo") if isinstance(result, dict) else None
    if not isinstance(info, dict):
        return {}
    name = next((info[k] for k in ("name", "title") if isinstance(info.get(k), str) and info[k].strip()), "")
    version = info.get("version") if isinstance(info.get("version"), str) else ""
    return {"name": name, "version": version} if name else {}


def _option_values(options: object) -> list[str]:
    """A select option list's values, flattening ACP's grouped form (`{group, options: [...]}`)."""
    values: list[str] = []
    for opt in options if isinstance(options, list) else ():
        if not isinstance(opt, dict):
            continue
        if isinstance(opt.get("options"), list):
            values.extend(_option_values(opt["options"]))
        elif isinstance(opt.get("value"), str) and opt["value"]:
            values.append(opt["value"])
    return values


def model_choice(result: object) -> dict:
    """The models a `session/new` (or resume/load) result offers, as `{current, available, source}`: its
    `configOptions` model selector where advertised (`source: "config_options"`), else the unstable
    `models.availableModels` (`source: "models"`); {} when it names neither."""
    if not isinstance(result, dict):
        return {}
    for opt in result.get("configOptions") or ():
        if not isinstance(opt, dict) or "model" not in (opt.get("category"), opt.get("id")):
            continue
        available = _option_values(opt.get("options"))
        current = opt.get("currentValue") if isinstance(opt.get("currentValue"), str) else ""
        if available or current:
            return {"current": current, "available": available, "source": "config_options"}
    models = result.get("models")
    if isinstance(models, dict):
        available = [m["modelId"] for m in models.get("availableModels") or ()
                     if isinstance(m, dict) and isinstance(m.get("modelId"), str) and m["modelId"]]
        current = models.get("currentModelId") if isinstance(models.get("currentModelId"), str) else ""
        if available or current:
            return {"current": current, "available": available, "source": "models"}
    return {}


def api_key_method(auth_methods: list) -> str:
    """The id of the first auth method that takes an API key (`_meta` holds `API_KEY_META`), or ""."""
    for method in auth_methods:
        meta = method.get("_meta") if isinstance(method, dict) else None
        method_id = method.get("id") if isinstance(method, dict) else None
        if isinstance(meta, dict) and API_KEY_META in meta and isinstance(method_id, str) and method_id:
            return method_id
    return ""


class AcpClient:
    def __init__(self, conn: Connection, *, client_version: str = "unknown") -> None:
        self.conn = conn
        self.client_version = client_version
        self.agent_capabilities: dict = {}
        #: `initialize`'s `authMethods`: what the agent accepts in `authenticate`.
        self.auth_methods: list = []
        self.resume_supported = True
        #: `initialize`'s `agentInfo` (`agent_info`), and the opened session's models (`model_choice`).
        self.agent_info: dict = {}
        self.models: dict = {}

    async def initialize(self, *, jaah_meta: dict) -> dict:
        result = await self.conn.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {**CLIENT_CAPABILITIES, "_meta": {META_KEY: jaah_meta}},
            "clientInfo": {"name": CLIENT_NAME, "version": self.client_version},
        })
        result = result if isinstance(result, dict) else {}
        caps = result.get("agentCapabilities")
        self.agent_capabilities = caps if isinstance(caps, dict) else {}
        methods = result.get("authMethods")
        self.auth_methods = [m for m in methods if isinstance(m, dict)] if isinstance(methods, list) else []
        self.agent_info = agent_info(result)
        return result

    async def authenticate(self, method_id: str, api_key: str, *, key_meta: str = API_KEY_META) -> None:
        """ACP `authenticate` with `method_id`, the key in `_meta[key_meta]` (a LIVE KEY: never logged, never kept).
        A dotted `key_meta` nests it: `api-key.apiKey` sends `{"api-key": {"apiKey": key}}`, for an engine that reads
        the key from an object (the adapter's `ACP_KEY_SIGN_IN` names which)."""
        entry, _, field = key_meta.partition(".")
        await self.conn.request("authenticate", {"methodId": method_id,
                                                 "_meta": {entry: {field: api_key} if field else api_key}})

    def supports(self, path: str) -> bool:
        """A dotted capability path is advertised: present and not false/null (ACP marks many with `{}`)."""
        node: object = self.agent_capabilities
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return node is not None and node is not False

    def takes_system_prompt(self) -> bool:
        """The agent applies a client `systemPrompt` on session setup (the systemPrompt RFD)."""
        return self.supports("sessionCapabilities.systemPrompt")

    async def open_session(self, *, cwd: str, mcp_servers: list[dict], additional_dirs: list[str], meta: dict,
                           resume_ref: str = "", agent_meta: dict | None = None,
                           system_prompt: str = "") -> tuple[str, str]:
        base: dict = {"cwd": cwd, "mcpServers": mcp_servers}
        if additional_dirs:
            base["additionalDirectories"] = additional_dirs
        if system_prompt:
            base["systemPrompt"] = {"mode": "append", "content": [{"type": "text", "text": system_prompt}]}
        base["_meta"] = {**(agent_meta or {}), META_KEY: meta}
        if resume_ref:
            for method, how, cap in (("session/resume", "resume", "sessionCapabilities.resume"),
                                     ("session/load", "load", "loadSession")):
                if not self.supports(cap):
                    continue
                try:
                    result = await self.conn.request(method, {**base, "sessionId": resume_ref})
                except JsonRpcError as exc:
                    log.info("acp %s refused (%s); falling back", method, exc.message)
                    continue
                ref = result.get("sessionId") if isinstance(result, dict) else None
                self.models = model_choice(result)
                return (ref if isinstance(ref, str) and ref else resume_ref), how
            self.resume_supported = False
        result = await self.conn.request("session/new", base)
        ref = result.get("sessionId") if isinstance(result, dict) else None
        self.models = model_choice(result)
        if not isinstance(ref, str) or not ref:
            raise JsonRpcError(-32603, "session/new returned no sessionId")
        return ref, "new"

    async def prompt(self, native_ref: str, text: str, *, guidance: str = "") -> dict:
        """`session/prompt` with `text`, headed by a separate `guidance` text block when there is any (the host's
        hook context, `acp/host.py::Host._drain_hook_context`)."""
        blocks = ([{"type": "text", "text": guidance}] if guidance else []) + [{"type": "text", "text": text}]
        result = await self.conn.request("session/prompt", {"sessionId": native_ref, "prompt": blocks})
        return result if isinstance(result, dict) else {}

    async def cancel(self, native_ref: str) -> None:
        await self.conn.notify("session/cancel", {"sessionId": native_ref})

    async def close(self, native_ref: str) -> None:
        if self.supports("sessionCapabilities.close"):
            await self.conn.request("session/close", {"sessionId": native_ref})
