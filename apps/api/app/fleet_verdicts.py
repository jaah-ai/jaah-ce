"""CP-authored slot intents: the pure composition half of the sensor-only reconcile.

One author per fact. The node is the single prober and writer of observed state
(`POST /api/agent/fleet/observe` → `worktrees`); `stack_state`/`urls` are derived at read time
(`app/slot_stack.py`). Intents: `issue_intents` → `slot_intents`, served by `GET /api/agent/pool-tick`
and pushed over the node socket (`app/node_send_intent.py`). The holder is the CP's own dispatch
lifecycle (`worktrees.state`, `app/slot_state.py`). Policy lives in `app/fleet_decide.py` §F.

No stale-reap pass: a slot is its task's until the task is done, deleted or reset, or an operator
releases it. `app/lease_drain.py::_reap_occupied_free_slots` is the opposite sign: the row is already
`free` by decision and only the tree is cleared.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Iterable, Optional

from sqlalchemy import and_, delete, func, or_, select, text, update

from app import agent_providers, intent_rail, project_identity, secret_redact
from app.models.nodes import Node
from app.models.projects import Project
from app.models.worktrees import SlotIntent
from app.node_identity import name_expr, node_id_or_404, node_ref

if TYPE_CHECKING:  # pragma: no cover - a type-only import, kept off the runtime import graph
    from uuid import UUID

    from app.models.agent_sessions import Agent
    from app.project_env import SlotEnv

log = logging.getLogger("fleet-verdicts")

# Two of the observation's console tri-state values: `attached` is the only sighting, `not_attached` the
# only reap licence; `probe_unavailable` is neither (treating it as either makes holds immortal or
# reclaims a human's tree).
CONSOLE_ATTACHED = "attached"
CONSOLE_NOT_ATTACHED = "not_attached"

# ── The intent verbs ──────────────────────────────────────────────────────────────────────────────────

# The only home of the verb vocabulary; `routes/agent/pool_tick.py::SLOT_INTENT_VERBS` imports it. The node
# cannot import this module, so it validates permissively and answers an unknown verb with
# the retiring `unsupported_verb` outcome (a silent drop re-served every tick would loop forever). A
# node-side test parses this file to pin that every issued verb has a node executor. `free_slot` is
# retired; `release_slot` carrying the hold's `session_id` is the one ending verb, and
# `purge_unexecutable_intents` arm 2 disposes of orphaned rows.
VERB_RESET_TO_BASE = "reset_to_base"
# Take the slot and launch a runner: account dir, port band, base, task, and the lease generation that
# fences the ack.
VERB_LEASE_SLOT = "lease_slot"
# End a hold the CP has already freed; the node tears the session down and catches its record up. The
# one ending verb, carrying the ended hold's `session_id` so the node kills the session the CP meant.
VERB_RELEASE_SLOT = "release_slot"
# `redispatch_slot` is retired; do not reinstate it. A same-slot relaunch rides `lease_slot` with
# `resume: true`, composed at issue time so a bundle always exists; a second verb would be a second
# dispatch site.

# Kill the runner in this slot. The kill decision is always the CP's, since it owns every other lease
# transition.
VERB_KILL_RUNNER = "kill_runner"
# Bring this slot's compose stack down, data intact. The idle-stop decision is the CP's (all its inputs
# are on the observation); the node keeps only how (`stack_ops.stop_stack`), and the payload names the
# project's own `projects.stack_down_cmd`.
VERB_STOP_STACK = "stop_stack"
# Deliver a human's gate answer into the live runner blocked on `AskUserQuestion`, so the warm session
# continues instead of being re-dispatched. Its executor sits in `reconcile.execute._INTENT_EXECUTORS` like the
# others; a verb missing there is answered `unsupported_verb` and the answer never arrives.
VERB_DELIVER_ANSWER = "deliver_answer"
# Type a CP-composed message into a WORKING runner's pane (the merge lane's failure report). Its own verb, not
# a `deliver_answer` variant: no `Escape` first (it would abort the runner's in-flight tool call) and not bound
# to `awaiting_response`. Minted only by `app/live_runner.py::deliver_message_to_live_runner`.
VERB_DELIVER_MESSAGE = "deliver_message"
# Destroy this slot: end whatever runs in it, take its stack down with its volumes, erase the tree, and
# ack so the CP deletes the row and frees the index. Minted only by `app/slot_allocation.py` (`retire`, and
# `remint_destroy` for an unacked one); the payload is `{task_id, purge_volumes, compose_project}`.
VERB_DESTROY_SLOT = "destroy_slot"
# Erase a slot's tree that has left this node's roster: a carried-over slot whose project moved to another org
# (`app/carryover.py`). Addressed by its payload (the node's `worktree_erase` arguments), never the roster, which
# no longer names the project; the ack deletes the freed row. Minted only by `app/slot_destroy.py::enqueue_erase`.
VERB_ERASE_TREE = "erase_tree"
# Queue a live-control intent (`steer`/`interrupt`/`answer`/`close`) for an ACP run's host, which drains it with
# `runtime.control_wait`; the payload is `{session_id, task_id, intent: {id, type, payload}}`, addressed by the
# session alone (`apps/jaahd/reconcile/enact_signal.py::_enact_runtime_control`). Minted only by
# `app/routes/console_runtime.py::post_console_runtime` (`build_runtime_control_intent`).
VERB_RUNTIME_CONTROL = "runtime_control"

SLOT_INTENT_VERBS = (
    VERB_RESET_TO_BASE, VERB_LEASE_SLOT, VERB_RELEASE_SLOT,
    VERB_KILL_RUNNER, VERB_STOP_STACK, VERB_DELIVER_ANSWER, VERB_DELIVER_MESSAGE, VERB_DESTROY_SLOT,
    VERB_ERASE_TREE, VERB_RUNTIME_CONTROL,
)

# The verbs whose still-pending predecessors on the same slot a new mint retires, applied by
# `issue_intents` before its insert. Invariant: clearing a precondition produces at most one launch.
# `deliver_answer` is excluded: nothing re-mints a superseded human answer. `deliver_message` is
# included: a newer report for the slot's runner makes a still-pending older one stale (it is unstamped, so the
# hazard below cannot apply). Hazard: a duplicate
# on `uq_slot_intents_slot_verb_observed_at` is swallowed, so re-minting a stamping verb on an unchanged
# observation supersedes and inserts nothing. Unstamped mints cannot collide; `stop_stack` relies on
# `app.idle_stacks.stop_idle_stacks`' pending set. A new stamping verb here needs its own suppression.
SUPERSEDING_VERBS = frozenset({
    VERB_LEASE_SLOT, VERB_KILL_RUNNER, VERB_RELEASE_SLOT, VERB_STOP_STACK, VERB_RESET_TO_BASE,
    VERB_DESTROY_SLOT, VERB_DELIVER_MESSAGE,
})

# Verbs whose payload must carry a composed `prompt_bundle`: minted with one or not at all
# (`build_lease_intent`); `purge_unexecutable_intents` removes rows without one.
BUNDLE_BEARING_VERBS = (VERB_LEASE_SLOT,)

# ── The ack outcomes ──────────────────────────────────────────────────────────────────────────────────

# Single-homed like the verbs. The node authors outcomes and the CP 422s unknown ones, so the node's set
# must stay a subset (pinned by the same drift test). Retiring outcomes drop the row out of pool-tick;
# everything else leaves it pending, since a dry-run, refusal or partial execution must not look done.
OUTCOME_DONE = "done"
# The node launched: the intent must retire or the next tick launches a second runner.
OUTCOME_ACCEPTED = "accepted"
OUTCOME_REFUSED_STALE = "refused_stale"
OUTCOME_REFUSED_PRECONDITION = "refused_precondition"
OUTCOME_FAILED = "failed"
# The loop-breaker: the node has no executor for this verb. Retires the intent though nothing was
# enacted; the CP must read it as "still something to do about this slot".
OUTCOME_UNSUPPORTED_VERB = "unsupported_verb"
# ── The two CP-AUTHORED outcomes ────────────────────────────────────────────────────────────
# Authored by the CP about a row the node never finished with; only the issuer can retire a refusal.
# Members of `SLOT_INTENT_OUTCOMES`; the ack route validates against `NODE_AUTHORED_OUTCOMES`, which is
# what stops a node posting them.

# Replaced: a newer dispatch for this slot. Writer: `issue_intents` (via `_supersede_kwargs`).
OUTCOME_SUPERSEDED = "superseded"
# Budget spent: `REFUSAL_PARK_AFTER` identical refusals. Retires the row and raises a human gate.
# Writer: `routes/agent/slot_intents.py::ack_slot_intent`.
OUTCOME_PARKED = "parked"

SLOT_INTENT_RETIRING_OUTCOMES = (
    OUTCOME_DONE, OUTCOME_ACCEPTED, OUTCOME_UNSUPPORTED_VERB, OUTCOME_SUPERSEDED, OUTCOME_PARKED,
)
# The column's full vocabulary, node- and CP-authored, which the node's drift test compares against.
SLOT_INTENT_OUTCOMES = (
    OUTCOME_DONE, OUTCOME_ACCEPTED, OUTCOME_REFUSED_STALE, OUTCOME_REFUSED_PRECONDITION, OUTCOME_FAILED,
    OUTCOME_UNSUPPORTED_VERB, OUTCOME_SUPERSEDED, OUTCOME_PARKED,
)
# What an ack may say: strictly narrower, so the CP-authored outcomes stay evidence of the CP's own
# supersede/budget logic rather than something a node could self-assert.
NODE_AUTHORED_OUTCOMES = (
    OUTCOME_DONE, OUTCOME_ACCEPTED, OUTCOME_REFUSED_STALE, OUTCOME_REFUSED_PRECONDITION, OUTCOME_FAILED,
    OUTCOME_UNSUPPORTED_VERB,
)

# ── The retry bound on a row the node keeps refusing ───────────────────────────────────────────────────
# A refusal is re-servable by design, but each re-serve costs a full enactment attempt (a wedged
# `reset_to_base` wrote a backup ref every tick). So refusals back off, doubling from the tick interval.
REFUSAL_BACKOFF_BASE_SECS = 5.0
# Unreachable at the current threshold (the doubling peaks at 40s); a bound on the arithmetic so raising
# the threshold cannot produce a multi-day wait.
REFUSAL_BACKOFF_CAP_SECS = 300.0
# Five identical refusals, ~75s of backoff: real transients here clear in seconds. Counted on identity,
# not attempts: a changed refusal reason gets the full budget again (`refusal_key`).
REFUSAL_PARK_AFTER = 5


def refusal_key(outcome: str, reason: Optional[str]) -> str:
    """The identity of one refusal, for deciding whether the next one is the same refusal.

    Outcome and reason together: every `reset_to_base` gate reports `refused_precondition`, so outcome
    alone would park a slot progressing between different refusals. The reason is the node's own words.
    """
    return f"{outcome}\n{(reason or '').strip()}"


def split_refusal_key(key: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """`refusal_key` read back as the (outcome, reason) pair it was built from: the only spelling of the
    inverse.

    Needed because a parked row's `outcome` is rewritten to `parked`
    (`routes/agent/slot_intents.py::_account_for_refusal`), so the cause survives only in this key;
    `routes/nodes/intents.py::IntentRow` puts it back on the wire. Split on the first newline only (reasons may be
    multi-line). NULL or empty gives `(None, None)`.
    """
    if not key:
        return None, None
    outcome, _, reason = key.partition("\n")
    return (outcome.strip() or None), (reason.strip() or None)


def refusal_backoff_secs(refusals: int) -> float:
    """How long to withhold a row that has now refused `refusals` times running. Doubling, then capped.

    1-based: 1→5s, 2→10s, 3→20s, 4→40s. Zero or less is a caller bug and collapses to the base rather than
    returning 0 and hot-looping.
    """
    if refusals <= 1:
        return REFUSAL_BACKOFF_BASE_SECS
    return min(REFUSAL_BACKOFF_BASE_SECS * (2 ** (refusals - 1)), REFUSAL_BACKOFF_CAP_SECS)


# ── What of a node's refusal prose may reach a TENANT-VISIBLE surface ─────────────────────────────────
# Node refusal prose stays whole on the operator-plane `slot_intents`; for object-scoped surfaces paths, URL
# userinfo and secret shapes are removed and the sentence kept. The input is clipped
# before any pattern runs, since the producer chooses its length (8 000-char node detail, uncapped
# supersede reasons); that bounds the cost of any pattern added later.

# URL userinfo, where git puts an installation token (`https://x-access-token@github.com`). The scheme is
# a closed alternation: an open `[a-z][a-z0-9+.-]*` scheme was quadratic over dotted tokens. The bounded
# `[^\s/@]{1,256}` cannot cross `/` or `@`, so a URL without userinfo matches nothing.
_URL_USERINFO_RE = re.compile(r"(?i)\b(https?|ssh|git|ftps?|file)://[^\s/@]{1,256}@")

# The credential directory by name, for the relative spelling (`.jaah/auth/<slug>`) the path pattern
# cannot reach. Anchored on the data dir literal.
_CRED_PATH_RE = re.compile(r"(?i)\.jaah[\\/]auth[\\/][^\s'\"]*")

# A path and the URL it must not eat, as one pattern: the first branch matches a whole URL (returned
# unchanged), the second an absolute path (redacted). A lookbehind approach missed `~/…`, `file:///…`,
# `//home/…`, `label:/home/…`, `.../home/…` and Windows paths. The path branch excludes only a preceding
# word character (a host). `file` is deliberately not kept: `file:///home/…` is the disclosure.
_URL_KEEP = r"(?:https?|ssh|git|ftps?)://[^\s'\"]*"
_ABS_PATH = r"(?<!\w)(?:[A-Za-z]:)?[\\/][A-Za-z0-9._+~-][A-Za-z0-9._+~\\/-]*"
_PATH_OR_URL_RE = re.compile(rf"(?i)\b{_URL_KEEP}|{_ABS_PATH}")


def _keep_url_redact_path(match: "re.Match[str]") -> str:
    """A URL survives; anything else the pattern matched was a path. `://` discriminates because the path
    branch's class excludes `:`.
    """
    return match.group(0) if "://" in match.group(0) else "<path>"


# One timeline line's worth: `slot_intents.reason` is unbounded `Text`, and the real clone failure is
# ~140 characters.
NODE_DETAIL_MAX = 300

# How much of the producer's string is looked at: eight times what can survive, since substitutions
# shorten.
_SCAN_SLACK = 8

# Past the scan bound, longer than any token the shared pass matches: a token the bound would split is still
# matched whole, so no tail of it survives into the clip.
_TOKEN_SLACK = 512


def redact_credentials(text: str) -> str:
    """`text` with URL userinfo and every secret shape the shared pass knows (`secret_redact`) replaced; paths and
    layout kept. The credential half of `redact_node_detail`, for an operator surface that keeps paths
    (`cp_ecs.task_output`). Unbounded: the caller clips first."""
    # Userinfo first, so the token is gone before other patterns read across it.
    return secret_redact.redact(_URL_USERINFO_RE.sub(r"\1://<redacted>@", text))[0]


def redact_node_detail(text: Optional[str], *, limit: int = NODE_DETAIL_MAX) -> str:
    """A node's own refusal detail, safe to show on an object-scoped surface. "" when there is none.

    A redactor, not an escape: it removes absolute paths (they reveal the credential store's shape,
    `~/.jaah/auth/<slug>`), URL userinfo, and every secret shape the shared pass knows (`secret_redact`: a
    forge PAT, an `Authorization:` value, `?access_token=`), and keeps the sentence. Callers still owe their
    surface's untrusted-text handling.

    Clipped first (the bound, see `_SCAN_SLACK`), substituted, then collapsed to one line and clipped
    again; a NUL would be unstorable in `text`. The parameter shadows `sqlalchemy.text` deliberately.
    """
    if not text or not text.strip():
        return ""
    # The bound, before any pattern sees the string; everything below is linear in `limit`.
    head = text[: limit * _SCAN_SLACK + _TOKEN_SLACK]
    # Credentials first, so a token is gone before the path pattern reads across it.
    cleaned = _PATH_OR_URL_RE.sub(_keep_url_redact_path, redact_credentials(head))
    # After the pass above, which already replaced absolute paths; this catches only the relative spelling.
    cleaned = _CRED_PATH_RE.sub("<credential-dir>", cleaned)
    cleaned = "".join(c if c >= " " and c != "\x7f" else " " for c in cleaned)
    return " ".join(cleaned.split())[:limit]


def build_slot_intent(
    verb: str,
    project_id: int,
    project: str,
    number: int,
    *,
    reason: str = "",
    payload: Optional[dict[str, Any]] = None,
    observation: Optional[dict[str, Any]] = None,
    precondition_fields: tuple[str, ...] = ("dirty", "session", "console"),
) -> dict[str, Any]:
    """One intent row for any CP-decided verb: the general form `build_lease_intent`, `build_kill_intent`,
    `build_deliver_answer_intent` and `build_stop_stack_intent` specialise; `app/lease_drain.py::_release`
    calls it directly for `VERB_RELEASE_SLOT`.

    `payload` is the decision (applied as-is); `preconditions` is the doubt the node re-verifies and may
    refuse on. A dispatch verb passes no observation and sends `None` as its staleness token (the drain's
    observation may be a tick old); it is fenced by `generation` instead. `precondition_fields` names each
    verb's doubt; the node refuses on any carried field that moved.

    No `branch` in the default: nobody decides a task's branch, and a read-only persona's
    worktree stays `DETACHED` until its runner switches, so a branch precondition would refuse it.
    """
    if verb not in SLOT_INTENT_VERBS:
        raise ValueError(f"unknown slot intent verb {verb!r}: expected one of {list(SLOT_INTENT_VERBS)}")
    obs = observation or {}
    return {
        "project_id": int(project_id),
        "project": project,
        "number": int(number),
        "verb": verb,
        "decided_on_version": None,
        "decided_on_observed_at": obs.get("observed_at") or None,
        "preconditions": {name: obs.get(name) for name in precondition_fields},
        "payload": dict(payload or {}),
        "reason": reason or None,
    }


def _env_vars_payload(env_vars: Optional["SlotEnv"], *, resume: bool) -> dict[str, Any]:
    """The `env_vars` key of a `lease_slot` payload, or no key at all. The pinned C4/C5 seam contract.

    When present: `{"values": {NAME: value}, "omit": [NAME, ...]}`, `omit` sorted. Absent or null means the
    node never rebuilds (it also withdraws an earlier overlay, `jaahd/worktree_mechanics/managed_env.py`).
    Absent when: `resume` (the resume leg never calls `claim_mechanics`, which would reset the tree; the
    carve-out lives here for every caller), `None` (nothing resolved), or an empty catalogue (so a new CP
    with nothing to say matches an old one). An empty `values` with a non-empty `omit` is still sent: `omit`
    removes lines whose credential no longer resolves (R9).
    """
    if resume or env_vars is None:
        return {}
    if not env_vars.values and not env_vars.omit:
        return {}
    # Copied, not aliased, so a caller holding the `SlotEnv` cannot mutate an issued decision. `omit` is
    # sorted because jsonb preserves array order; `values` is not, because jsonb canonicalises object key
    # order on storage anyway.
    return {"env_vars": {"values": dict(env_vars.values), "omit": sorted(env_vars.omit)}}


def build_lease_intent(
    project_id: int,
    project: str,
    number: int,
    *,
    task_id: object,
    base: Optional[str],
    holder: str,
    generation: int,
    dispatch_id: str,
    config_dir: Optional[str] = None,
    securestorage_dir: Optional[str] = None,
    api_key_account: str = "",
    provider: str = agent_providers.DEFAULT_PROVIDER,
    git_author: Optional[dict[str, str]] = None,
    ports: Optional[dict[str, int]] = None,
    prompt: Optional[dict[str, Any]] = None,
    env_vars: Optional["SlotEnv"] = None,
    resume: bool = False,
    reason: str = "",
    bedrock: Optional[dict[str, Any]] = None,
    has_repo: Optional[bool] = None,
    memory: Optional[dict[str, Any]] = None,
    launch: Optional[dict[str, Any]] = None,
    guard: Optional[dict[str, Any]] = None,
    runtime: str = "legacy",
    handoff: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """The `lease_slot` intent: every decision the node used to make for one dispatch, in one row.

    Fields: `config_dir` (account pick), `ports` (the slot's port band; the node keeps `apply_port_band`), `base`
    (`integration_branch`), `generation`/`dispatch_id` (fence the ack), `prompt_bundle` (composed at issue
    time). No branch: the runner creates its own `{branch_prefix}fleet/task-<id>`.

    `securestorage_dir`: a shared account's credential store (`CLAUDE_SECURESTORAGE_CONFIG_DIR`), independent
    of `config_dir`. `api_key_account`: an account slug, never the key; the node fetches the secret over its
    own socket (`app/account_auth.py::node_credential_name`). Both always spelled (`None`/`""` included):
    `apps/jaahd` ships independently. `resume`: the tree holds this task's previous attempt; do
    not reset it. `env_vars` (`_env_vars_payload`) is the one key carrying plaintext secrets: scrubbed on ack
    and by purge arm 5, never logged, never on `routes/nodes/intents.py::IntentRow`. `memory`: the project-memory
    ids for an ON task (`project_memory/select.py::lease_memory`, §4.4) — never unit text. `launch`: an SDK
    agent's `agent_launch(...)`; it replaces `prompt_bundle` (an agent has no prompt), and None leaves a
    model dispatch's intent byte-for-byte what it was. `guard`: `{"project_id"}` when the Architectural Guard
    is available for the project's org (`governance/guard_runtime.py::lease_stanza`), else None. `runtime`: `acp` or
    `legacy`, the operator's runtime switch resolved for this dispatch's account (`app/acp_runtime.py`).
    `handoff`: `{"to", "native_session_ref", "from_session_id"}` on a console view switch's resume lease
    (`routes/console_runtime.py::post_console_handoff`): the node closes the old front, then resumes the engine's
    own session on the other one. None on every other dispatch.
    """
    return build_slot_intent(
        VERB_LEASE_SLOT, project_id, project, number,
        reason=reason or (
            f"resume task {task_id} in place" if resume
            else f"dispatch task {task_id}"
        ),
        payload={
            "resume": bool(resume),
            "task_id": task_id,
            "base": base,
            "holder": holder,
            "generation": int(generation),
            "dispatch_id": dispatch_id,
            "config_dir": config_dir,
            "securestorage_dir": securestorage_dir,
            # Always spelled, `""` included, for `securestorage_dir`'s skew reason.
            "api_key_account": api_key_account or "",
            # Which vendor's CLI this dispatch is for, resolved off the leased account's `accounts.provider`
            # (`lease_drain._resolve_account`); without it every node launched the default vendor. Always
            # spelled for the skew reason; on the node, absent or empty must keep meaning `claude`. `or
            # DEFAULT_PROVIDER` mirrors the column's server default; an unregistered provider still raises in
            # the node's adapter registry.
            "provider": str(provider or agent_providers.DEFAULT_PROVIDER),
            # Who the runner commits as (`git_access.commit_identity_for_task`), or `None`. Always spelled;
            # the node exports GIT_AUTHOR_*/GIT_COMMITTER_* and treats absent or malformed as "leave it to git
            # config" (`apps/jaahd/git_env.py`).
            "git_author": dict(git_author) if git_author else None,
            "ports": dict(ports or {}),
            # An agent launch carries no prompt at all; a model launch always spells its bundle.
            **({"launch": dict(launch)} if launch else {"prompt_bundle": dict(prompt or {})}),
            # The one conditional key (see `_env_vars_payload`): spread rather than spelled, because here an
            # old CP and a new one with nothing to say should reach the node identically.
            **_env_vars_payload(env_vars, resume=resume),
            # The Jaah-account block (K2, `app/jaah_routing/routing.py::plan_block`), only on such a dispatch:
            # spread like `env_vars`, so an own-accounts intent is byte-for-byte what it was.
            **({"account_ref": f"bedrock:{bedrock['session_id']}", "bedrock": dict(bedrock)} if bedrock else {}),
            # The leased SLOT's kind (`Worktree.has_repo`): false makes the node create an empty folder
            # and run no git. Spread like `bedrock`: None (a repo project's git worktree) leaves the intent byte
            # for byte what it was, and the node reads an absent key as true (`reconcile.tick_wire.wire_has_repo`).
            **({"has_repo": bool(has_repo)} if has_repo is not None else {}),
            # Project memory (§4.4), only for an ON task: spread like `bedrock`, so an OFF intent is byte-for-byte
            # what it was, and the node launches exactly as before (`apps/jaahd/project_memory_bundle.py`).
            **({"memory": dict(memory)} if memory else {}),
            # The Architectural Guard (§5.1), only where it is available: spread like `memory`, so an org without
            # it gets an intent byte-for-byte what it was, and its node fetches no bundle.
            **({"guard": dict(guard)} if guard else {}),
            # Which runtime the node starts (`app/acp_runtime.py`), fixed for this attempt. Spread like `guard`: a
            # legacy dispatch is byte-for-byte what it was, and the node reads an absent key as legacy (as an older
            # node reads every intent).
            **({"runtime": runtime} if runtime == "acp" else {}),
            # A console view switch (D-CON-1), only on that resume lease: spread like `runtime`, so an ordinary
            # dispatch is byte-for-byte what it was.
            **({"handoff": dict(handoff)} if handoff else {}),
        },
    )


#: `launch.kind` for an SDK agent run (`app/agent_resolution.py::Sdk`): the node runs the agent under
#: `jaah-acp-host` (architecture §3.4, §4.4). No account, no config dir.
LAUNCH_KIND_AGENT = "agent"
#: Not a `launch.kind` but a node capability beside it: the node runs a manifest 2 agent's launch (one carrying
#: `command`) under `jaah-acp-host`. Placement offers such a run only to a node reporting both.
LAUNCH_KIND_AGENT_ACP = "agent_acp"
#: A capability too: the node's host installs an ACP registry agent's pin (a launch carrying `registry`). A node
#: that predates it reports `agent_acp` alone and would refuse that launch, so placement asks for this as well.
LAUNCH_KIND_AGENT_ACP_REGISTRY = "agent_acp_registry"


def agent_launch(
    agent: "Agent", *, org_id: object, input_artifacts: list, input_artifacts_dir: Optional[str], prompt: str = "",
    key_account: str = "",
) -> dict[str, Any]:
    """The `launch` block of an SDK agent's lease intent — the ONE builder, shared by the static lease
    (`lease_drain._dispatch_agent`) and the MicroVM launch (`microvm_placement.admit`), exactly §3.4's keys.

    Fields come off the `agents` row as it stands at dispatch (the bundle is the row's `resolved_commit`, which
    the same transaction stamps as the task's `run_agent_commit`); `input_artifacts` from
    `dispatch_compose.input_staging.stage_input_artifacts`, as a model stage gets them. No URL rides it: the node
    reads the bundle from `GET /api/agent/tasks/{id}/agent-bundle`; `env` the row's own variables, set on the
    agent's process. `org_id` is the RUNNING task's org (not the agent's owner, for a connector).

    An SDK v2 agent's launch adds `command` (run by the node under `jaah-acp-host`) and `prompt`, the task as its
    first ACP prompt. An ACP registry install has no bundle and no command: its launch adds `prompt` and `registry`
    (the pin the host installs, under its slug), `profile` and `key_delivery` as `acp_registry.row_fields` stored
    them, and `key_account` (the bound API-key account's slug, never its key) when one is bound (`language` is its
    distribution kind). An SDK v1 row never reaches here: `agent_resolution.resolve_agent` refuses it.
    The node never re-reads the registry."""
    from app import agents

    manifest = agent.manifest or {}
    launch = {
        "kind": LAUNCH_KIND_AGENT,
        "agent_id": str(agent.id),
        "display_name": agent.display_name,
        "version": agent.version,
        "commit": agent.resolved_commit,
        "language": agent.language,
        "subpath": agent.subpath or "",
        "bundle_sha256": agent.bundle_sha256,
        "bundle_bytes": agent.bundle_bytes,
        "input_artifacts": list(input_artifacts),
        "input_artifacts_dir": input_artifacts_dir,
        "org_id": str(org_id) if org_id is not None else "",
        "env": dict(agent.env or {}),
    }
    if agents.is_acp_manifest(manifest):
        launch.update(command=list(manifest.get("command") or []), prompt=prompt)
    elif agents.is_registry_manifest(manifest):
        launch.update(
            prompt=prompt,
            registry={"slug": agent.slug, "pin": manifest.get("pin") or {}, "agent": manifest.get("agent") or {}},
            profile=manifest.get("profile") or "", key_delivery=manifest.get("key_delivery") or "",
            **({"key_account": key_account} if key_account else {}),
        )
    return launch


def build_kill_intent(
    project_id: int,
    project: str,
    number: int,
    *,
    reason: str,
    task_id: object = None,
    session_id: Optional[str] = None,
    pid: object = None,
    generation: Optional[int] = None,
    observation: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """The `kill_runner` intent: stop the runner in this slot.

    The decision is the CP's (`VERB_KILL_RUNNER`). Two fences, both read by
    `reconcile.enact_signal._enact_kill_runner`: `task_id` (the projection must still name this task, held
    `task`) and `generation` — the hold's `worktrees.generation` at mint, bumped per lease and redispatch, so
    a kill minted for a predecessor launch is retired stale instead of killing a successor relaunched into the
    same slot. Not `session_id`: `slot_state.redispatch` keeps `launch_session_id` across a relaunch.
    `generation=None` (a hold with no generation) falls back to the task fence alone. `session_id`/`pid` travel
    for the record only. The key is always present on the wire, even when None.
    """
    return build_slot_intent(
        VERB_KILL_RUNNER, project_id, project, number,
        reason=reason,
        payload={"task_id": task_id, "session_id": session_id, "pid": pid, "generation": generation},
        observation=observation,
    )


# The doubt a stack-stop re-verifies: `session` (the safety property; carried as `False` so the node's
# fail-closed rule refuses when the probe could not answer), `console` (a human in the tree), and
# `stack_up` (relevance). `branch`/`dirty` are deliberately not here.
STOP_STACK_PRECONDITIONS = ("session", "console", "stack_up")

# A `deliver_answer` (and a `deliver_message`) re-verifies only that something is live in the slot; the real
# fence is the node's session-identity check against the payload.
DELIVER_ANSWER_PRECONDITIONS = ("session",)


def deliver_answer_reason(task_id: int) -> str:
    """The `reason` a `deliver_answer` intent carries, shown on the Slot Intents page. Names the event, not
    an operator (the answerer may be a tenant admin; `app/tenancy/copy.py`). Censused by
    `tests/test_tenant_system_rows.py`.
    """
    return f"task {task_id}'s question was answered — deliver to the live runner"


def deliver_message_reason(task_id: int) -> str:
    """`deliver_answer_reason`'s twin for a `deliver_message` intent: the event, never an actor."""
    return f"a message for task {task_id}'s live runner — deliver it to the pane"


def dispatch_reason(task_id: int, *, own_tree: bool = False) -> str:
    """The `reason` a dispatch's lease carries, read on the Worktrees page and the intent, in the slot's own
    org. Says "dispatch", not the operator console's "fleet" (`app/tenancy/copy.py`). Censused by
    `tests/test_tenant_system_rows.py`.
    """
    suffix = " onto its own existing tree" if own_tree else ""
    return f"dispatch of task {task_id}{suffix}"


def console_release_reason(*, teardown: bool) -> str:
    """The `reason` a slot release from the console's Worktrees page carries, on the hold it ends and the
    teardown intent. Names the act and its origin, never an operator; the audit row says who. Censused by
    `tests/test_tenant_system_rows.py`.
    """
    base = "released from the console"
    return f"{base} — end the runner and clear the tree" if teardown else base


def console_delete_reason() -> str:
    """`console_release_reason`'s twin for the slot DELETE, which always tears the tree down."""
    return "deleted from the console — end the runner and clear the tree"


def task_stopped_reason(task_id: int) -> str:
    """The `reason` the kill a Stop mints carries: the event; the audit row names who pressed Stop.
    Censused by `tests/test_tenant_system_rows.py`.
    """
    return f"task {task_id} was stopped from the console — stop that runner, keep its worktree"


def epic_stopped_reason(child_id: int, epic_id: int) -> str:
    """`task_stopped_reason`'s twin for the running children a stopped epic cascades to: the event, never
    the actor.
    """
    return (
        f"task {child_id}'s epic {epic_id} was stopped from the console — stop that runner, "
        "keep its worktree"
    )


def build_deliver_answer_intent(
    project_id: int,
    project: str,
    number: int,
    *,
    task_id: int,
    question: str,
    answer: str,
    session_id: str,
    reason: str,
    attachments: Optional[list[dict[str, Any]]] = None,
    attachments_dir: Optional[str] = None,
    sender_id: Optional[UUID] = None,
    question_ids: Optional[list[int]] = None,
    bare: bool = False,
) -> dict[str, Any]:
    """The `deliver_answer` intent: hand a human's gate answer to the live runner blocked in this slot.

    `session_id` is the load-bearing field: a slot outlives its session, and writing the answer into a new
    runner would inject one task's answer as a trusted prompt into another. The node refuses any mismatch,
    fail-closed. No lease or task state changes here: the CP flips `awaiting_response -> active` only once
    the node acks `done`. `question` lets the delivered text name what it answers.

    `attachments`/`attachments_dir` are the task's input files and the workdir the dispatch prompt named
    (`_task_attachments_meta`, `dispatch_compose.input_staging._attachment_workdir`): the node stages any it lacks before
    writing the answer, so a file attached to the answer is on disk at the path the answer names. Absent
    on an older CP; an older node ignores them.

    `sender_id` is the answering principal, for the task page's message list (the node ignores it); absent
    when the delivery carries no single answerer (a batch of rounds answered by several people).

    `question_ids` are the rounds the answer resolves, which `supersede_pending_answer_delivery` re-arms (the
    node ignores them). `bare` asks the node to send `answer` as the whole message, with no `Answer: ` frame
    (the ignored-round wake word); an older node frames it, which still names no question.
    """
    return build_slot_intent(
        VERB_DELIVER_ANSWER, project_id, project, number,
        reason=reason,
        payload={
            "task_id": int(task_id),
            "question": question,
            "answer": answer,
            "session_id": session_id,
            **({"sender_id": str(sender_id)} if sender_id is not None else {}),
            **({"question_ids": [int(qid) for qid in question_ids]} if question_ids else {}),
            **({"bare": True} if bare else {}),
            **({"attachments": attachments, "attachments_dir": attachments_dir}
               if attachments and attachments_dir else {}),
        },
        precondition_fields=DELIVER_ANSWER_PRECONDITIONS,
    )


def build_deliver_message_intent(
    project_id: int,
    project: str,
    number: int,
    *,
    task_id: int,
    session_id: str,
    text: str,
    sender: str,
) -> dict[str, Any]:
    """The `deliver_message` intent: type TEXT into the live runner of this slot WITHOUT `Escape` and
    whatever the task's state — the recipient is WORKING, not parked on a question.

    `session_id` is the fence exactly as in `build_deliver_answer_intent`; the node refuses any mismatch.
    `text` arrives already scrubbed and labelled (`app/live_runner.py`). The merge lane's failure frame:

        [control plane] Merge lane: PR #<n> failed on <tree> — <tests…>
        <excerpt>
        Fix it, push, and report the new head with pull_request_enqueue.

    `sender` names the CP part speaking, for the task page's message list; the node ignores it.
    """
    return build_slot_intent(
        VERB_DELIVER_MESSAGE, project_id, project, number,
        reason=deliver_message_reason(task_id),
        payload={"task_id": int(task_id), "session_id": session_id, "text": text, "sender": sender},
        precondition_fields=DELIVER_ANSWER_PRECONDITIONS,
    )


def runtime_control_reason(task_id: int, control_type: str) -> str:
    """The `reason` a `runtime_control` intent carries, shown on the Slot Intents page: the control's type and
    the task, never the operator's words or name. Censused by `tests/test_tenant_system_rows.py`."""
    return f"runtime_control: {control_type} for task {task_id}"


def build_runtime_control_intent(
    project_id: int,
    project: str,
    number: int,
    *,
    task_id: int,
    session_id: str,
    control_id: str,
    control_type: str,
    control_payload: dict[str, Any],
) -> dict[str, Any]:
    """The `runtime_control` intent: queue one live control (`steer`/`interrupt`/`answer`/`close`) on the ACP
    session SESSION_ID, which the node addresses by that id alone (`_enact_runtime_control`).

    `session_id` is the fence as in `build_deliver_answer_intent`, carried both in the payload (the address) and
    as the `session` precondition. `control_id` is the intent's own id, which the host echoes in its event
    stream; `control_payload` is `{text}` for steer/answer and `{}` otherwise, already scrubbed by the route.
    Not superseding: two steers sent in a row are two prompts, both owed.
    """
    return build_slot_intent(
        VERB_RUNTIME_CONTROL, project_id, project, number,
        reason=runtime_control_reason(task_id, control_type),
        payload={
            "session_id": session_id,
            "task_id": int(task_id),
            "intent": {"id": control_id, "type": control_type, "payload": dict(control_payload)},
        },
        observation={"session": session_id},
        precondition_fields=DELIVER_ANSWER_PRECONDITIONS,
    )


def build_stop_stack_intent(
    project_id: int,
    project: str,
    number: int,
    *,
    reason: str,
    stack_down_cmd: str,
    observation: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """The `stop_stack` intent: bring one slot's compose stack down without touching its data.

    The caller has already decided whether (`VERB_STOP_STACK`); no threshold here. The payload carries what
    to run, `projects.stack_down_cmd`, since the node has no down command of its own; the node never
    re-derives it. `STOP_STACK_PRECONDITIONS` remains the doubt it re-verifies.
    """
    return build_slot_intent(
        VERB_STOP_STACK, project_id, project, number,
        reason=reason,
        payload={"stack_down_cmd": stack_down_cmd},
        observation=observation,
        precondition_fields=STOP_STACK_PRECONDITIONS,
    )


# ── DB: the intent channel ────────────────────────────────────────────────────────────────────────────

# Keys an intent dict carries for the caller, not the table: only `project`, the display name
# (`slot_intents` keys on `project_id`). Declared once so adding a display-only key is a visible edit,
# not a 500 on the next dispatch.
DISPLAY_ONLY_INTENT_KEYS = ("project",)


def intent_columns(intent: dict[str, Any]) -> dict[str, Any]:
    """One intent dict, narrowed to the keys that are actually `slot_intents` columns.

    One strip for one writer (`issue_intents`); an unstripped intent raises on the unknown attribute.
    """
    return {k: v for k, v in intent.items() if k not in DISPLAY_ONLY_INTENT_KEYS}


def _settle(session, commit: bool) -> None:
    """Commit, or merely flush when the caller owns the transaction: `app/intent_rail.py::settle`.

    Kept under this name because comments here cite it; the rule lives in the rail.
    """
    intent_rail.settle(session, commit)


def push_frame_intent(intent: dict[str, Any], persisted: dict[str, Any]) -> dict[str, Any]:
    """One newly-inserted intent, in the shape the node's parser already knows.

    Exactly `routes/agent/pool_tick.py::PoolTickIntent`'s field set (single-homed there; not imported, to keep the
    dependency one-way), so pushed and polled intents share one parser, executor table and ack path.
    Timestamps come off the persisted row: `id` and `created_at` exist only after insert, and
    `decided_on_observed_at` is read back as the node will compare it.
    """
    return {
        "id": persisted["id"],
        "project_id": intent["project_id"],
        # The display name, not a table column, but the node names the directory and tmux window from it.
        "project": intent.get("project") or "",
        "number": intent["number"],
        "verb": intent["verb"],
        "decided_on_version": intent.get("decided_on_version"),
        "decided_on_observed_at": intent_rail.iso(persisted.get("decided_on_observed_at")),
        "preconditions": intent.get("preconditions") or {},
        "payload": intent.get("payload") or {},
        "reason": intent.get("reason"),
        "created_at": intent_rail.iso(persisted.get("created_at")),
    }


def _persist_kwargs(resolved_node) -> dict[str, Any]:
    """Everything rail-specific about a `slot_intents` INSERT, as kwargs for `app/intent_rail.py::persist`:
    the `ON CONFLICT` target, the `RETURNING` columns, duplicate logging, and the org derivation.

    One home for `issue_intents` and `_persist_intents`, so production and seeding cannot diverge. The table
    has two uniques and a statement gets one `ON CONFLICT` (`uq_slot_intents_slot_verb_version`); the
    observation-stamp tuple raises and the rail's SAVEPOINT reports it as already recorded. `org_id` is a
    scalar subquery on the node (`SlotIntent` is `Derived(parent=Node)`), since most minters run fleet-wide
    with no bound org; a separate read would also desync `tests/test_fleet_verdicts_reap_notify.py`'s
    `_FakeSession`.
    """
    def _duplicate(intent: dict[str, Any]) -> None:
        log.info(
            "fleet-verdicts: intent %s #%s/%s already recorded for this observation — not re-decided",
            intent.get("project"), intent.get("number"), intent.get("verb"),
        )

    return {
        "model": SlotIntent,
        "fixed": {
            "node_id": resolved_node,
            "org_id": select(Node.org_id).where(Node.id == resolved_node).scalar_subquery(),
        },
        "columns": intent_columns,
        "conflict_constraint": "uq_slot_intents_slot_verb_version",
        "returning": (SlotIntent.id, SlotIntent.created_at, SlotIntent.decided_on_observed_at),
        "on_duplicate": _duplicate,
    }


def _persist_intents(
    session, resolved_node, intents: Iterable[dict[str, Any]], *, commit: bool
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """The persist step on its own: `app/intent_rail.py::persist` with `_persist_kwargs`' configuration.

    Not the mint's path (`issue_intents` uses the rail's `issue`); used by `record_intents` for seeding.
    """
    return intent_rail.persist(
        session, intents=intents, commit=commit, **_persist_kwargs(resolved_node)
    )


def record_intents(session, node_id: str, intents: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Persist newly-decided intents, idempotently. Returns the ones that were actually inserted.

    No production caller: this is the test seeding entry point (resolves a node uuid and commits). New
    production call sites belong on `issue_intents`; this skips the supersede and the push.

    `node_id` is the node uuid, resolved eagerly here: a `node_ref` subquery yielding NULL would
    raise an `IntegrityError` that `_persist_intents`' SAVEPOINT reads as a duplicate, silently dropping an
    unregistered node's intents. A dispatch must fail closed and loudly (contract rule 6).
    """
    resolved_node = node_id_or_404(session, node_id)
    return [intent for intent, _persisted in _persist_intents(
        session, resolved_node, intents, commit=True
    )]


# The mint's result (`app/intent_rail.py`'s class), aliased so `isinstance` holds across rails and there
# is one `.send()`. A `commit=False` minter must push after its own commit; `.send()` makes that ordering
# an action (see `app/node_send_intent.py`).
IssuedIntents = intent_rail.IssuedIntents


# Payload keys worth naming in a supersession reason, in post-mortem order. The strings are the
# diagnostic; a supersession appends them to the node's last word.
_SUPERSESSION_DETAIL_KEYS = ("task_id", "generation", "dispatch_id")


def _supersession_reason(intent: dict[str, Any]) -> str:
    """Why the predecessors of this intent are being retired, in one operator-readable line."""
    payload = intent.get("payload") or {}
    detail = ", ".join(
        f"{key} {payload[key]}" for key in _SUPERSESSION_DETAIL_KEYS if payload.get(key) is not None
    )
    # Named, not id'd, falling back to the id only without a display name.
    slot = f"{intent.get('project') or intent['project_id']}#{intent['number']}"
    return (
        f"superseded by a newer {intent['verb']} on {slot}"
        + (f" ({detail})" if detail else "")
        + (f": {intent['reason']}" if intent.get("reason") else "")
    )


def issue_intents(
    session, node_id: str, intents: Iterable[dict[str, Any]], *, commit: bool = True
) -> IssuedIntents:
    """The one way a slot intent comes into existence: supersede → persist → send.

    The mechanism is `app/intent_rail.py::issue`; this function decides which verbs supersede
    (`SUPERSEDING_VERBS`) and which rows count as predecessors (pending rows of the same verb on the same
    `(node, project, slot)`). Predecessors are superseded before the insert, the retry budget is
    carried forward, then rows go in, then frames go out.

    `node_id` is the node uuid (a name resolves nothing: names repeat across orgs), resolved here once; the
    uuid is both the table's key and `app/node_bus.py`'s registry key. `commit=True` commits then sends; `commit=False` only flushes and sends
    nothing, and the caller commits and calls `.send()` (`routes/tasks/slots.py::_mint_kill_runner_intents`).
    """
    resolved_node = node_id_or_404(session, node_id)

    def _supersede_for(intent: dict[str, Any]) -> Optional[dict[str, Any]]:
        if intent.get("verb") not in SUPERSEDING_VERBS:
            return None
        return _supersede_kwargs(
            session,
            node_id=node_id,
            project_id=intent["project_id"],
            number=intent["number"],
            verbs=(intent["verb"],),
            reason=_supersession_reason(intent),
        )

    def _frame(intent: dict[str, Any], row: dict[str, Any]) -> Optional[dict[str, Any]]:
        # An intent with no display name is not pushed (as `pool-tick` does); it stays pending until the name
        # resolves.
        return push_frame_intent(intent, row) if intent.get("project") else None

    return intent_rail.issue(
        session,
        node_id=str(resolved_node),
        intents=list(intents),
        supersede_for=_supersede_for,
        frame=_frame,
        commit=commit,
        **_persist_kwargs(resolved_node),
    )


# The one "still in the node's executable set" predicate, shared here, by
# `app.idle_stacks.stop_idle_stacks` and `app.lease_drain._release_finished`, which must agree with
# pool-tick (`routes/tasks/`'s kill suppression deliberately differs). `outcome IS NULL` is explicit
# because `NOT IN` over NULL is NULL.
def still_pending():
    # `app/intent_rail.py::still_pending` holds the shape; the retiring vocabulary stays here and is passed
    # in.
    return intent_rail.still_pending(SlotIntent, SLOT_INTENT_RETIRING_OUTCOMES)


def own_unanswered_lease_since():
    """`created_at` of a `task` hold's OWN `lease_slot` order while it is still queued and its node has never
    answered it, else NULL — a scalar subquery correlated to `Worktree`, matched on the dispatch identity the
    order carries. Read by the redispatch gate (`routes/fleet.py`, `fleet_decide.dispatch_is_redispatchable`)
    and by the account cap (`account_runners`), which must agree that such a hold is still in flight."""
    from app.models.worktrees import Worktree

    return (
        select(func.min(SlotIntent.created_at))
        .where(
            SlotIntent.node_id == Worktree.node_id,
            SlotIntent.project_id == Worktree.project_id,
            SlotIntent.number == Worktree.number,
            SlotIntent.verb == VERB_LEASE_SLOT,
            SlotIntent.acked_at.is_(None),
            still_pending(),
            SlotIntent.payload["dispatch_id"].astext == Worktree.dispatch_id,
        )
        .correlate(Worktree)
        .scalar_subquery()
    )


def predecessor_gone():
    """Has the node reported a `task` hold's predecessor runner gone since it was stopped? A boolean expression
    correlated to `Worktree`, read by the redispatch gate (`routes/fleet.py`,
    `fleet_decide.dispatch_is_redispatchable`) so a relaunch into the task's own slot waits (uncharged) until
    it is (a relaunch beside a dying predecessor read as "vanished").

    True when the slot has no `kill_runner` order; else only when none is still pending AND the slot's own
    `observed_at` is newer than every one AND it reports no `session`. No new timeout: a kill refused forever
    retires by the refusal park, and a silent node is its staleness handling's. Compares the CP's
    `created_at` with the node's own `observed_at`, so clock skew shifts the wait by the skew. Read again
    at the take (`lease_drain._take_for_dispatch`) for a stop landing after the plan."""
    from app.models.worktrees import Worktree

    blocking_kill = (
        select(SlotIntent.id)
        .where(
            SlotIntent.node_id == Worktree.node_id,
            SlotIntent.project_id == Worktree.project_id,
            SlotIntent.number == Worktree.number,
            SlotIntent.verb == VERB_KILL_RUNNER,
            or_(still_pending(), Worktree.observed_at.is_(None), SlotIntent.created_at >= Worktree.observed_at),
        )
        .correlate(Worktree)
        .exists()
    )
    return and_(~blocking_kill, Worktree.session.isnot(True))


def dispatch_runtime_for_task(session, task_id: int) -> Optional[str]:
    """The runtime (`acp`/`legacy`) the `lease_slot` order now holding `task_id`'s slot chose, or None when no
    such order is found (a VM task holds no slot). Read off the order itself — `build_lease_intent` spells
    `runtime` only for `acp` — so a session that dies before its first event still names the runtime it was
    dispatched on. Display only: written to `sessions.dispatched_runtime`, never `runtime`."""
    from app import slot_state
    from app.models.worktrees import Worktree

    row = session.execute(
        select(SlotIntent.payload["runtime"].astext, SlotIntent.payload["launch"]["command"].isnot(None))
        .join(Worktree, and_(
            SlotIntent.node_id == Worktree.node_id,
            SlotIntent.project_id == Worktree.project_id,
            SlotIntent.number == Worktree.number,
            SlotIntent.payload["dispatch_id"].astext == Worktree.dispatch_id,
        ))
        .where(Worktree.task_id == task_id, Worktree.state == slot_state.TASK,
               SlotIntent.verb == VERB_LEASE_SLOT)
        .order_by(SlotIntent.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    # A manifest 2 SDK agent's launch (`agent_launch`, one carrying `command`) runs under `jaah-acp-host` too.
    return "acp" if row[0] == "acp" or row[1] else "legacy"


def slots_awaiting_a_kill(session) -> set[tuple[int, int, str]]:
    """Every `(project_id, slot number, task id)` whose `kill_runner` the node has not finished with: the
    set a slot release must wait on.

    The node's kill executor is task-binding fenced, so a release landing first unbinds the slot and the
    kill sweeps nothing while the runner keeps working. Order: kill, then release.

    The wait ends by: the refusal park (identical refusals only; alternating reasons never accumulate),
    `purge_unexecutable_intents`' age arm (the hard bound), or supersession by the next kill on that slot.
    An unreachable node keeps its slot, correctly; `routes/pool_slots.py::release_slot` is the escape
    hatch. Assumes no kill for a different task lands on a bound slot. `end_attempt` marks its terminal
    kill fresh so the cooldown cannot swallow it. One query per pass.
    """
    return {
        (int(project_id), int(number), str(int(task_key)))
        for project_id, number, task_key in session.execute(
            select(SlotIntent.project_id, SlotIntent.number, SlotIntent.payload["task_id"].astext)
            .where(SlotIntent.verb == VERB_KILL_RUNNER, still_pending())
        ).all()
        # Normalised through `int`: a JSON `null` or malformed `task_id` matches no hold and is dropped rather
        # than raising and taking the drain pass down.
        if task_key is not None and task_key.lstrip("-").isdigit()
    }


def retire_released_lease(
    session, *, node_id: Any, project_id: int, number: int, dispatch_id: Optional[str], reason: str,
) -> int:
    """Retire a released hold's own still-pending `lease_slot` order. Does NOT commit; returns how many went.

    Supersession otherwise happens only at a mint, so a hold released by an operator edge (Reset, Delete, a
    move) and never re-leased left its order queued: the node later enacted it into a slot that was no longer
    its, and the refusal landed on a task that had since moved project and started elsewhere.
    Matched on the order's own `dispatch_id`, so it can never retire a successor's lease."""
    if not dispatch_id:
        return 0
    kwargs = _supersede_kwargs(
        session, node_id=str(node_id), project_id=project_id, number=number,
        verbs=(VERB_LEASE_SLOT,), reason=reason,
    )
    kwargs["doomed"] = (*kwargs["doomed"], SlotIntent.payload["dispatch_id"].astext == str(dispatch_id))
    changed, _ = intent_rail.supersede(session, commit=False, **kwargs)
    return changed


def _supersede_kwargs(
    session, *, node_id: str, project_id: int, number: int, verbs: tuple[str, ...], reason: str,
) -> dict[str, Any]:
    """The static rail's contribution to a supersession, as kwargs for `app/intent_rail.py::supersede`: the
    doomed predicate, the outcome, and the retirement's name.

    A dict so `issue_intents` can pass it to the rail's `issue`.
    """
    doomed = (
        SlotIntent.node_id == node_ref(node_id),
        SlotIntent.project_id == int(project_id),
        SlotIntent.number == int(number),
        SlotIntent.verb.in_(verbs),
        still_pending(),
    )

    def _describe(superseded: list[tuple[Any, str, Optional[str]]], changed: int) -> None:
        # The slot named, not id'd; one bulk read so the rail's select keeps its row multiplicity.
        named = project_identity.names_by_id(session, [int(project_id)]).get(int(project_id), "")
        log.warning(
            "fleet-verdicts: superseded %d pending intent(s) on %s#%s (node %s) — %s. Retired: %s",
            changed, named or project_id, number, node_id, reason,
            ", ".join(f"{i} ({verb}, was {outcome!r})" for i, verb, outcome in superseded),
        )

    return {
        "model": SlotIntent,
        "doomed": doomed,
        "reason": reason,
        "superseded_outcome": OUTCOME_SUPERSEDED,
        "describe": _describe,
    }


# How long a pending, refused-or-failed intent may keep being re-served before the CP retires it as
# unexecutable; without a bound a never-clearing condition is re-served every 5s forever.

# One hour: ~720 attempts at a 5s tick, far past any transient here, yet a wedged row is gone within a
# working hour.
UNEXECUTABLE_INTENT_MAX_AGE_SECS = 3600.0


def purge_unexecutable_intents(session) -> int:
    """DELETE every pending intent that can never execute, whatever the reason. Returns how many went.

    The only scheduled pruner of this table. Arms:
      1. a bundle-bearing verb with no bundle and no `launch` (un-acked);
      2. a verb this CP no longer issues (un-acked);
      3. a row refused past the age bound (`acked_at` proves it was tried; `created_at` measures age);
      4. a CP-retired (`superseded`/`parked`) row past the same bound, or the table and its partial index
         would never shrink.
    Logged with the slot named. Arm 5 deletes nothing: it scrubs the resolved environment off never-acked
    `lease_slot` rows, logged rather than counted in the return (`lease_drain`'s `purged`).
    """
    bundle = SlotIntent.payload["prompt_bundle"]
    bundleless = and_(
        SlotIntent.verb.in_(BUNDLE_BEARING_VERBS),
        SlotIntent.acked_at.is_(None),
        # An agent launch (`agent_launch`) carries no bundle by design and is executable.
        SlotIntent.payload["launch"].astext.is_(None),
        or_(
            bundle["system_prompt"].astext.is_(None),
            bundle["system_prompt"].astext == "",
            bundle["task_prompt"].astext.is_(None),
            bundle["task_prompt"].astext == "",
        ),
    )
    retired_verb = and_(
        SlotIntent.verb.notin_(SLOT_INTENT_VERBS),
        SlotIntent.acked_at.is_(None),
    )
    # Arm 3: age from `created_at`, not `acked_at`, because every refusal re-stamps `acked_at`, so that bound
    # never arrived. `routes/agent/slot_intents.py::_may_park` uses the same column and points here.
    #
    # `acked_at IS NOT NULL` stays as the attempt bound: proof the node answered, so a fresh order not yet
    # drained is never deleted. It is the CP's own record, preferred over the wire-supplied `outcome`.
    stuck_refusing = and_(
        SlotIntent.acked_at.is_not(None),
        SlotIntent.created_at < func.now() - text(
            f"make_interval(secs => {UNEXECUTABLE_INTENT_MAX_AGE_SECS})"
        ),
        SlotIntent.outcome.is_not(None),
        SlotIntent.outcome.notin_(SLOT_INTENT_RETIRING_OUTCOMES),
    )
    # Arm 4: CP-retired rows sit outside arm 3 but inside `ix_slot_intents_pending` (`outcome IS DISTINCT FROM
    # 'done'`), which would otherwise grow with every superseded dispatch.
    retired_by_the_cp = and_(
        SlotIntent.outcome.in_((OUTCOME_SUPERSEDED, OUTCOME_PARKED)),
        SlotIntent.created_at < func.now() - text(
            f"make_interval(secs => {UNEXECUTABLE_INTENT_MAX_AGE_SECS})"
        ),
    )
    doomed = or_(bundleless, retired_verb, stuck_refusing, retired_by_the_cp)
    # Read before delete for the log; the DELETE's WHERE is authoritative, so a race can only over-log.
    for row in session.execute(
        # The node as its name for the log (`nodes.id` is a uuid); `name_expr` avoids another join.
        select(SlotIntent.id, SlotIntent.verb, Project.name.label("project"), SlotIntent.number,
               name_expr(SlotIntent.node_id).label("node_id"), SlotIntent.outcome)
        .join(Project, Project.id == SlotIntent.project_id)
        .where(doomed)
    ).all():
        log.warning(
            "fleet-verdicts: retiring unexecutable intent %s (%s on %s#%s, node %s, last outcome %r) — "
            "it can never execute and was being re-served on every pool tick",
            row.id, row.verb, row.project, row.number, row.node_id, row.outcome,
        )
    removed = session.execute(delete(SlotIntent).where(doomed)).rowcount
    # ── ARM 5 — SCRUB, NEVER DELETE: a `lease_slot` row NOBODY EVER ACKED, past the same age bound ──────
    # Resolved environment is plaintext on the payload until an ack scrubs it; a valid never-acked
    # `lease_slot` falls through arms 1-4. Deleting it would cancel a real dispatch to a node that is merely
    # offline, so one key is nulled instead and the claim degrades to a no-op. `->>` is NULL for absent and
    # JSON null alike, so scrubbed rows stop matching. Age is `created_at`. `superseded`/`parked` rows have no
    # possible consumer and are scrubbed immediately rather than after the hour.
    scrubbable = and_(
        SlotIntent.verb == VERB_LEASE_SLOT,
        SlotIntent.acked_at.is_(None),
        SlotIntent.payload["env_vars"].astext.is_not(None),
        or_(
            SlotIntent.created_at < func.now() - text(
                f"make_interval(secs => {UNEXECUTABLE_INTENT_MAX_AGE_SECS})"
            ),
            SlotIntent.outcome.in_((OUTCOME_SUPERSEDED, OUTCOME_PARKED)),
        ),
    )
    scrubbed = session.execute(
        update(SlotIntent)
        .where(scrubbable)
        # Pinned, not `"auto"`: the criterion is unevaluatable in Python, so the ORM would use `"fetch"` with
        # an extra `RETURNING`; nothing here needs syncing and the commit expires everything.
        .execution_options(synchronize_session=False)
        # `jsonb_set` in one statement, touching nothing else. Set to JSON `null` rather than removed: the
        # same to the node, and a record that the row once carried an environment.
        .values(payload=func.jsonb_set(
            SlotIntent.payload, text("'{env_vars}'::text[]"), text("'null'::jsonb")
        ))
    ).rowcount
    if scrubbed:
        # Names nothing from the payload, not even variable names.
        log.warning(
            "fleet-verdicts: scrubbed the resolved environment off %d never-acked lease_slot intent(s) "
            "older than %ss — a dispatch no node ever answered on must not keep its plaintext, and the "
            "rows are left serviceable so the lease is not cancelled",
            scrubbed, UNEXECUTABLE_INTENT_MAX_AGE_SECS,
        )
    session.commit()
    return removed or 0


