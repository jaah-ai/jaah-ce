"""The node half of a Jaah-account launch: Claude Code on the platform Bedrock account.

The control plane decides everything and hands this node K2 — the `bedrock` block on a `lease_slot` intent
or a console create frame (`app/jaah_routing/routing.py::plan_block`):

    {"session_id": "<jaah_session uuid>", "region": "us-east-1",
     "models": {"fable": "<ARN>|null", "opus": ..., "sonnet": ..., "haiku": ...},
     "subagent_model": "<ARN>", "model_arg": "<ARN>|null",
     "credential_name": "bedrock_session:<jaah_session uuid>"}

`parse_plan` is the ONE reading of it (both transports). What a launch does with it:

* `env_lines` — the per-runner exports: Bedrock on, the region, one `ANTHROPIC_DEFAULT_<FAMILY>_MODEL` per
  family the tenant has a profile for, the subagent model. PER RUNNER, NEVER NODE-WIDE: a variable set on
  the tmux server reaches every pane.
  It also clears every ambient AWS credential source the SDK would otherwise prefer or fall back on —
  an inherited key, a profile, a bearer token, or a host's instance role — so the ONLY credentials this
  runner can hold are the session's own.
* `settings` — `awsCredentialExport`: Claude Code runs `jaah-credential bedrock_session:<id>` and reads
  K3's `{"Credentials": {...}}` from its stdout. NO AWS SECRET IS EVER EXPORTED, WRITTEN OR LOGGED HERE
  (fence C2): the launch files name the credential, the broker (`cred_broker`) serves its value.
* `preflight_lines` — run the export once before the CLI starts and stop the pane loudly if the broker
  serves nothing, the `api_key_preflight_lines` shape.

Every value is `shq`'d by the caller's quoting function: an ARN is CP data, and launch text is a shell.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

import account_auth
import environment

#: The account reference a Jaah dispatch carries (`app/jaah_routing/routing.py::REF_PREFIX`).
REF_PREFIX = "bedrock:"
#: The broker name K3 is asked under (`app/jaah_routing/routing.py::CREDENTIAL_PREFIX`).
CREDENTIAL_PREFIX = "bedrock_session:"
#: The vendor whose models the Jaah account serves (`Capabilities.vendor`).
VENDOR = "anthropic"


def admits(caps) -> bool:
    """May a provider with these capability groups (`agent_providers.Capabilities`, None for an unknown value) be
    launched on the Jaah account? Only Claude Code itself (`account_layer` "omc") on `VENDOR`'s models: the plan's
    exports are Claude Code's settings, so another CLI of the same vendor is refused too."""
    return caps is not None and caps.vendor == VENDOR and caps.account_layer == "omc"
#: K2's model slots, one per catalogue family, in the order the exports are written.
SLOTS: tuple[str, ...] = ("fable", "opus", "sonnet", "haiku")

# A region and a model reference are CP data that lands in a shell: shape-checked here, quoted at the use.
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d{1,2}\Z")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:/._-]{0,255}\Z")

# Every ambient credential source the AWS SDK consults before, or instead of, `awsCredentialExport`. Unset
# so a node's own identity — an operator's key in the daemon env, an EC2 instance role — can never stand in
# for the tenant's session (D7), and a failed export fails closed instead of falling back.
CLEARED_AWS_VARS: tuple[str, ...] = (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
    "AWS_BEARER_TOKEN_BEDROCK", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_DEFAULT_PROFILE", "AWS_CONTAINER_AUTHORIZATION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "AWS_SDK_LOAD_CONFIG", "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
)


@dataclass(frozen=True)
class BedrockPlan:
    """K2, validated. `models` holds only the families with a profile."""

    session_id: str
    region: str
    models: dict[str, str]
    subagent_model: str
    model_arg: str

    @property
    def credential_name(self) -> str:
        return f"{CREDENTIAL_PREFIX}{self.session_id}"

    @property
    def account_ref(self) -> str:
        return f"{REF_PREFIX}{self.session_id}"

    def wire(self) -> dict:
        """The K2 shape back, for a hop that re-sends it (`console.py` → `clauded_wt.py`)."""
        return {
            "session_id": self.session_id, "region": self.region,
            "models": {slot: self.models.get(slot) for slot in SLOTS},
            "subagent_model": self.subagent_model, "model_arg": self.model_arg,
            "credential_name": self.credential_name,
        }


class PlanRefused(ValueError):
    """K2 that cannot be launched. The message names the field, never a value worth hiding (there is none)."""


def _model(value: object, field: str) -> str:
    text = value if isinstance(value, str) else ""
    if not _MODEL_RE.match(text):
        raise PlanRefused(f"the Jaah block's {field} is not a model reference")
    return text


def parse_plan(raw: object) -> BedrockPlan:
    """THE ONE READING of K2. Raises `PlanRefused` for anything that is not a launchable block: a launch
    on half a block would open a runner Bedrock refuses, or one on the wrong model."""
    if not isinstance(raw, dict):
        raise PlanRefused("the Jaah block is not an object")
    try:
        session_id = str(uuid.UUID(str(raw.get("session_id") or "")))
    except ValueError:
        raise PlanRefused("the Jaah block's session_id is not a uuid") from None
    region = raw.get("region")
    if not isinstance(region, str) or not _REGION_RE.match(region):
        raise PlanRefused("the Jaah block's region is not an AWS region")
    raw_models = raw.get("models")
    if not isinstance(raw_models, dict):
        raise PlanRefused("the Jaah block carries no models")
    models = {slot: _model(raw_models[slot], f"models.{slot}")
              for slot in SLOTS if raw_models.get(slot) is not None}
    subagent = _model(raw.get("subagent_model"), "subagent_model")
    # `model_arg` may be null on the wire; the session then opens on its subagent model, which the CP
    # guarantees has a profile (`plan_block`), never on the CLI's own default — a foundation id the
    # tenant's session policy refuses.
    model_arg = _model(raw.get("model_arg") or subagent, "model_arg")
    expected = f"{CREDENTIAL_PREFIX}{session_id}"
    if raw.get("credential_name") != expected:
        raise PlanRefused("the Jaah block's credential_name does not name its own session")
    return BedrockPlan(session_id=session_id, region=region, models=models,
                       subagent_model=subagent, model_arg=model_arg)


def optional_plan(raw: object) -> Optional[BedrockPlan]:
    """`parse_plan`, with absence (an own-accounts launch) as None. A PRESENT but malformed block still
    raises: it is never read as "no block", which would launch on whatever account the rest names."""
    if raw is None:
        return None
    return parse_plan(raw)


#: The Jaah home's basename. The control plane reads a config dir's basename as an account slug
#: (`app/node_clone.py::account_slug_from_dir`); a dot is outside the slug grammar, so this one names no
#: account, ever — a Bedrock break or session can never be stamped onto a tenant's own account.
CONFIG_DIR_NAME = "jaah.bedrock"


def config_dir() -> str:
    """THIS node's Claude config dir for Jaah-account sessions: `<pool root>/jaah.bedrock`, or `""` when the
    node has no pool root. It holds the managed layer (plugins, hooks, the control MCP server) and the
    sessions' transcripts, never a credential. Outside `auth/`, so no roster sweep reads it as an account."""
    root = environment.pool_root_or_empty().rstrip("/")
    return f"{root}/{CONFIG_DIR_NAME}" if root.startswith("/") else ""


def export_command(plan: BedrockPlan, quote: Callable[[str], str]) -> str:
    """The `awsCredentialExport` command: the broker client asking for THIS session's K3 answer."""
    return f"{quote(account_auth.CREDENTIAL_HELPER)} {quote(plan.credential_name)}"


def env_pairs(plan: BedrockPlan) -> list[tuple[str, str]]:
    """`[(name, value)]` a Jaah session runs under, in a stable order. No secret, by construction."""
    pairs = [("CLAUDE_CODE_USE_BEDROCK", "1"), ("AWS_REGION", plan.region),
             # IMDS off: on an EC2 node an expired or refused export must fail, not fall back to the
             # instance role.
             ("AWS_EC2_METADATA_DISABLED", "true"),
             # The SDK's file sources pointed at nothing: unset, they default to the session user's own
             # `~/.aws`, which any runner on the node can write.
             ("AWS_SHARED_CREDENTIALS_FILE", "/dev/null"), ("AWS_CONFIG_FILE", "/dev/null")]
    pairs.extend((f"ANTHROPIC_DEFAULT_{slot.upper()}_MODEL", plan.models[slot])
                 for slot in SLOTS if slot in plan.models)
    pairs.append(("CLAUDE_CODE_SUBAGENT_MODEL", plan.subagent_model))
    return pairs


def env_lines(plan: BedrockPlan, quote: Callable[[str], str]) -> list[str]:
    """`launch.sh` lines: the ambient AWS sources unset, then `env_pairs` exported, each value quoted."""
    return [f"unset {' '.join(CLEARED_AWS_VARS)}",
            *(f"export {name}={quote(value)}" for name, value in env_pairs(plan))]


def settings(plan: BedrockPlan, quote: Callable[[str], str]) -> dict:
    """The keys merged into the per-dispatch `--settings` file. `skipDangerousModePermissionPrompt` for
    the API-key reason (`agents/claude.py::api_key_settings`): this home may not be converged yet, and the
    bypass-mode prompt parks an unattended runner forever."""
    return {"awsCredentialExport": export_command(plan, quote), "skipDangerousModePermissionPrompt": True}


#: The pre-flight's bound: the pane's registry entry (the broker's proof) is written just AFTER the pane
#: spawns, so a first ask can precede it.
PREFLIGHT_TRIES = 30
PREFLIGHT_SLEEP = "0.5"


def preflight_lines(plan: BedrockPlan, quote: Callable[[str], str]) -> list[str]:
    """Ask the broker before the CLI starts — bounded, for the race above — and stop the pane, loudly, if
    it never serves (the balance is at the floor, the platform account is down, the session is over). The
    answer goes to /dev/null: it is a secret."""
    return [
        "n=0",
        f"until {export_command(plan, quote)} >/dev/null 2>&1; do",
        "  n=$((n+1))",
        f"  if [ $n -ge {PREFLIGHT_TRIES} ]; then",
        "    printf 'launch.sh: the credential broker served no Bedrock credentials for this Jaah session - "
        "refusing to launch a runner that would start unauthenticated\\n' >&2",
        "    exit 1",
        "  fi",
        f"  sleep {PREFLIGHT_SLEEP}",
        "done",
    ]


def role_session_name(session_id: str) -> str:
    """The STS role session name the CP mints this session's credentials under
    (`app/jaah_routing/credentials.py`): K5's fallback key hashes it."""
    return f"jaah-{uuid.UUID(session_id).hex}"


def ensure_config_dir() -> str:
    """`config_dir()`, created as the session user at the account mode, or `""` (logged by the caller)."""
    path = config_dir()
    if not path:
        return ""
    import accounts  # noqa: PLC0415 — lazy: `accounts` → `commands` → the daemon core
    import session_user  # noqa: PLC0415

    if session_user.ensure_dir_as_session_user(path, mode=accounts.ACCOUNT_DIR_MODE):
        return ""
    return path
