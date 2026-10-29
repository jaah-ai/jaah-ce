"""node_liveness.py — assert that every node the control plane believes in is actually reporting.

Invariant: an `enabled` node reports within `STALE_AFTER_SECONDS`, or it is a delivered alarm. Four
conditions, one dedup key and hidden `adhoc` Task each: SILENT (`last_seen`), NOT OBSERVING
(`observed_at`, on-bus nodes only), STALE RELEASE (provably behind `GET /api/agent/release-desired`
past `RELEASE_DESIRED_GRACE_SECONDS`; absent generations are only reported) and CONFIG DRIFT
(`config_snapshot` vs `config_desired`). Alarms file as `draft` with a re-arm cooldown, auto-resolve
per key, and fail loudly when undelivered. Reads the control plane only; ages use the DB's `now()`.

Runs in-process only, as the `node-liveness` cron (`app/node_liveness_job.py`, which injects every
seam). Exit codes: 0 healthy, 1 invariant violated, 3 could not verify.
"""

from __future__ import annotations

import json
import sys
from typing import Callable, NamedTuple, Optional


# ── The bound ─────────────────────────────────────────────────────────────────────
# Mirrors `apps/api/app/jaah_discovery.py`'s `_LAST_SEEN_REFRESH_INTERVAL` (the fastest `last_seen` can
# advance); a test asserts the two agree.
REPORT_INTERVAL_SECONDS = 60.0
# Ten missed reports (600s). Generous on purpose: this alarm's failure direction is noise. No env override.
STALE_REPORT_MULTIPLE = 10
STALE_AFTER_SECONDS = REPORT_INTERVAL_SECONDS * STALE_REPORT_MULTIPLE

# The release grace window: the node's `jaahd-update.timer` bounds convergence at 30min + up to 300s
# jitter; 40 minutes adds margin. A BEHIND node inside it is reported, not alarmed.
RELEASE_DESIRED_GRACE_SECONDS = 2400.0

# ── Verdicts ─────────────────────────────────────────────────────────────────────
VERDICT_LIVE = "live"
VERDICT_SILENT = "silent"
VERDICT_NOT_OBSERVING = "not-observing"
VERDICT_STALE_RELEASE = "stale-release"
VERDICT_CONFIG_DRIFT = "config-drift"
VERDICT_DISABLED = "disabled"

# ── Generation placement ──────────────────────────────────────────────────────────
# Where a node's reported release sits relative to the published one. Only BEHIND alarms.
GENERATION_CURRENT = "current"
GENERATION_BEHIND = "behind"
GENERATION_AHEAD = "ahead"         # newer than the desired release: mid-rollback, or a local publish
GENERATION_UNKNOWN = "unknown"     # not reported, unparseable, or the desired release is unreadable
# A virtual node whose release lags: never a rollout target itself (`app/vnodes.py::retarget_rollout`),
# so no verdict fires; its host row carries BEHIND instead.
GENERATION_VIA_HOST = "via-host"
# The release every node is judged against: `GET /api/agent/release-desired`, which answers with the
# control plane's own version and is the same endpoint nodes converge on. Unreadable ⇒ every placement
# is `unknown` ⇒ reported, never alarmed.
RELEASE_DESIRED_PATH = "/api/agent/release-desired"

# ── Alarm delivery ───────────────────────────────────────────────────────────────
# `adhoc` + an explicit dedup_key: the shape for a producer with no dedicated system persona
# (`app/routes/agent/task_reads.py::create_system_task` 422s on `adhoc` without one). The project is not named here: the
# seams file into the operator setting "Alarm project" (`app/cron_alarms.py::alarm_project_id`).
ALARM_TASK_PERSONA = "adhoc"

# First guard: `draft` is the one non-dispatchable initial system-Task state
# (`task_lifecycle._SYSTEM_TASK_INITIAL_STATES`), still dedup-matchable and resolvable — so nothing
# dispatches or closes it and each pass refreshes the one card.
ALARM_TASK_STATE = "draft"

# Second guard, for a human closing the card while the condition holds (a terminal row frees the key).
# One hour: shorter re-files a just-dismissed card; longer mutes a genuine new occurrence (accepted —
# see `_rearm_suppressed`).
ALARM_REARM_COOLDOWN_SECONDS = 3600.0
# The prefix `_resolve_cleared_alarm` writes; the cooldown uses it to tell this sweep's own clear (re-arm
# at once) from somebody else's close.
CLEARED_SUMMARY_PREFIX = "condition cleared"

# One key per condition: `create_system_task` body-refreshes a dedup match, so a shared key would let
# conditions overwrite each other's alarm.
SILENT_DEDUP_KEY = "fleet-node-liveness-silent"
# The operator's runbook titles: every alarm files into the Alarm project's own org, never a tenant's.
SILENT_TITLE = "🚨 An enabled node has stopped reporting"
NOT_OBSERVING_DEDUP_KEY = "fleet-node-liveness-not-observing"
NOT_OBSERVING_TITLE = "🚨 An enabled node connects but is not observing"
STALE_RELEASE_DEDUP_KEY = "fleet-node-liveness-stale-release"
STALE_RELEASE_TITLE = "🚨 An enabled node is running a release behind the published one"
CONFIG_DRIFT_DEDUP_KEY = "fleet-node-liveness-config-drift"
CONFIG_DRIFT_TITLE = "🚨 An enabled node's live config does not match its declaration"

# A contract read by `node_liveness_job` and the cron wrapper (`app/cron_system_activities.py`).
EXIT_OK = 0
EXIT_INVARIANT_VIOLATED = 1
EXIT_COULD_NOT_VERIFY = 3

def warn(msg: str) -> None:
    print(msg, file=sys.stderr)


class DBUnavailable(RuntimeError):
    """The control-plane DB could not be read. Maps to EXIT_COULD_NOT_VERIFY — never to a quiet all-clear."""


class NodeRow(NamedTuple):
    """One `nodes` row as this sweep needs it. Ages are seconds-since, or None when the stamp is NULL.

    NULL and very old fail the bound alike, but the report words them differently ("never" vs
    "stopped"). `generation` is `config_snapshot->>'version'`; `placement` is stamped by
    `place_generations` and defaults to GENERATION_UNKNOWN, so stale-release fires only on positive proof.
    `slots` / `worktree_rows` are evidence only, never asserted on.
    """
    node_id: str
    enabled: bool
    last_seen_age: Optional[float]
    observed_at_age: Optional[float]
    worktrees_updated_age: Optional[float]
    last_seen: Optional[str]
    observed_at: Optional[str]
    slots: int
    worktree_rows: int
    generation: Optional[str] = None
    generation_reported_age: Optional[float] = None
    placement: str = GENERATION_UNKNOWN
    # Every declared knob whose reported value differs, as `(key, want, got)` — from `config_drift`.
    # Empty with no declaration or no reported snapshot.
    drift: tuple[tuple[str, str, Optional[str]], ...] = ()
    # `nodes.id` as text — the identity (`node_id` is the name, for display and command text, and repeats
    # across orgs). `app/vnodes.py::retarget_rollout` keys on it. None from an older CP.
    node_uuid: Optional[str] = None


# ── Pure classification (no DB, no network, no clock) ────────────────────────────

def is_stale(age: Optional[float], bound: float = STALE_AFTER_SECONDS) -> bool:
    """True when a stamp's age fails the bound. A NULL stamp (never reported) is stale."""
    return age is None or age > bound


def normalize_config_value(value: object) -> str:
    """One env value as the readers that decide on it see it: `.strip().lower()` plus quote-stripping
    (`apps/jaahd/main.py`, the node's update script), so `"ON"` is not reported as drift."""
    return str(value).strip().strip("'").strip('"').lower()


def config_drift(desired: object, reported: object) -> tuple[tuple[str, str, Optional[str]], ...]:
    """Every declared knob whose live value does not match, as `(key, want, got)` in declaration order.

    Only declared keys are asserted (`{}` asserts nothing). A node that never reported a snapshot is
    not accused. A reported payload missing a declared key yields `got=None` — unset on the node, or
    absent from the daemon's `CONFIG_SNAPSHOT_ALLOWLIST`.
    """
    if not isinstance(desired, dict) or not desired:
        return ()
    if not isinstance(reported, dict):
        return ()
    drifted: list[tuple[str, str, Optional[str]]] = []
    for key, want in desired.items():
        if key not in reported:
            drifted.append((str(key), str(want), None))
            continue
        got = reported[key]
        if normalize_config_value(got) != normalize_config_value(want):
            drifted.append((str(key), str(want), str(got)))
    return tuple(drifted)


def release_desired_in_grace(
    published_updated_age: Optional[float], grace: float = RELEASE_DESIRED_GRACE_SECONDS
) -> bool:
    """True when `release_desired` last changed inside `grace` — a BEHIND node may still be mid-rollout.
    A None age is not in grace."""
    return published_updated_age is not None and published_updated_age < grace


def verdicts_for(
    row: NodeRow, bound: float = STALE_AFTER_SECONDS, release_grace_active: bool = False
) -> list[str]:
    """Every condition `row` violates, in report order. Empty-but-for-LIVE means healthy.

    A disabled node yields exactly [VERDICT_DISABLED] and is never alarmed (disabling is a remedy).
    A draining node keeps every alarm: it is still in service, and a silent node mid-drain is exactly
    the stall to surface (`app/models/nodes.py::Node.draining`).
    """
    if not row.enabled:
        return [VERDICT_DISABLED]
    found: list[str] = []
    silent = is_stale(row.last_seen_age, bound)
    if silent:
        found.append(VERDICT_SILENT)
    elif is_stale(row.observed_at_age, bound):
        # Only meaningful for a node on the bus.
        found.append(VERDICT_NOT_OBSERVING)
    if row.placement == GENERATION_BEHIND and not release_grace_active:
        # Positive proof only: an absent or unplaceable generation, or a BEHIND one inside the release
        # grace window, is reported but never accused.
        found.append(VERDICT_STALE_RELEASE)
    if row.drift:
        # Live config contradicts the declared `config_desired` — independent of every condition above.
        found.append(VERDICT_CONFIG_DRIFT)
    return found or [VERDICT_LIVE]


def classify(
    rows: list[NodeRow], bound: float = STALE_AFTER_SECONDS, release_grace_active: bool = False
) -> dict[str, list[NodeRow]]:
    """Group `rows` by verdict: {verdict -> [rows]}. A row can appear under more than one alarm."""
    grouped: dict[str, list[NodeRow]] = {}
    for row in rows:
        for verdict in verdicts_for(row, bound, release_grace_active):
            grouped.setdefault(verdict, []).append(row)
    return grouped


def parse_rows(raw: str) -> list[NodeRow]:
    """Parse the roster `routes/agent/node_config.py::node_liveness` serves (as JSON) into NodeRows. Raises DBUnavailable on anything
    unparseable, so a broken read never looks like an empty (healthy) fleet.
    """
    try:
        payload = json.loads(raw.strip() or "null")
    except json.JSONDecodeError as exc:
        raise DBUnavailable(f"control-plane DB returned unparseable JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise DBUnavailable(f"control-plane DB returned a non-list body: {type(payload).__name__}")
    rows: list[NodeRow] = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("node_id"):
            raise DBUnavailable(f"control-plane DB returned a row without a node_id: {item!r}")
        rows.append(NodeRow(
            node_id=str(item["node_id"]),
            enabled=bool(item.get("enabled")),
            last_seen_age=_as_float(item.get("last_seen_age")),
            observed_at_age=_as_float(item.get("observed_at_age")),
            worktrees_updated_age=_as_float(item.get("worktrees_updated_age")),
            last_seen=item.get("last_seen") or None,
            observed_at=item.get("observed_at") or None,
            slots=int(item.get("slots") or 0),
            worktree_rows=int(item.get("worktree_rows") or 0),
            generation=(str(item["generation"]).strip() or None) if item.get("generation") else None,
            generation_reported_age=_as_float(item.get("generation_reported_age")),
            # Computed here, not in SQL, so the rule is hermetically testable. An older CP serving
            # neither key yields no drift.
            drift=config_drift(item.get("config_desired"), item.get("config_reported")),
            node_uuid=str(item["node_uuid"]) if item.get("node_uuid") else None,
        ))
    return rows


def _as_float(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _age_text(age: Optional[float], stamp: Optional[str], never: str) -> str:
    """Human evidence for one stamp: "never …", "<duration> ago (<stamp>)", or — when the caller has no
    stamp text to show, only an age — plain "<duration> ago"."""
    if age is None:
        return never
    if age >= 86400:
        span = f"{age / 86400:.1f}d"
    elif age >= 3600:
        span = f"{age / 3600:.1f}h"
    else:
        span = f"{age:.0f}s"
    return f"{span} ago ({stamp})" if stamp else f"{span} ago"


# ── Generation placement (pure, plus one read of the desired-release row) ─────────

def parse_version(text: Optional[str]) -> Optional[tuple[int, ...]]:
    """A comparable tuple for a MAJOR.MINOR.PATCH release label, or None when it isn't one.

    Never a partial parse: an unreadable label is `unknown` (reported), not "probably behind"."""
    if not text:
        return None
    parts = text.strip().split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


class PublishedRelease(NamedTuple):
    """The desired release plus how long ago it last changed, both from `GET /api/agent/release-desired`.
    `updated_age` is None exactly when `version` is None."""
    version: Optional[str]
    updated_age: Optional[float] = None


def place_generation(generation: Optional[str], published: Optional[str]) -> str:
    """Where `generation` sits relative to `published`: current / behind / ahead / unknown.

    "behind" needs positive proof (both parse and the node's is strictly lower). "ahead" is reported,
    never alarmed. Pure: callers read `published` and pass it in."""
    if not generation or not published:
        return GENERATION_UNKNOWN
    if generation == published:
        return GENERATION_CURRENT
    node_version, published_version = parse_version(generation), parse_version(published)
    if node_version is None or published_version is None:
        return GENERATION_UNKNOWN
    # Zero-padded before comparing: unpadded `(0, 2) < (0, 2, 0)` would call the same release BEHIND.
    width = max(len(node_version), len(published_version))
    node_version += (0,) * (width - len(node_version))
    published_version += (0,) * (width - len(published_version))
    if node_version < published_version:
        return GENERATION_BEHIND
    if node_version > published_version:
        return GENERATION_AHEAD
    return GENERATION_CURRENT      # equal tuples, different spelling (e.g. "0.2" vs "0.2.0")


def any_generation_reported(rows: list[NodeRow]) -> bool:
    """True when at least one row reports a generation — callers skip the desired-release read
    otherwise."""
    return any(row.generation for row in rows)


def place_generations(rows: list[NodeRow], published: Optional[str]) -> list[NodeRow]:
    """Stamp every row's `placement` against `published`. Pure — the read happens in the caller."""
    return [row._replace(placement=place_generation(row.generation, published)) for row in rows]


def _generation_text(row: NodeRow) -> str:
    """Human evidence for one node's generation: the version, where it sits, and how old the claim is."""
    if not row.generation:
        return "NOT REPORTED (config_snapshot->>'version' is NULL)"
    age = _age_text(row.generation_reported_age, None, "at an unknown time")
    return f"v{row.generation} ({row.placement}), reported {age}"


# ── Reports (one per condition — one remedy, one body, each) ──────────────────────

def _bound_sentence() -> str:
    return (
        f"The bound is `{STALE_REPORT_MULTIPLE} x {REPORT_INTERVAL_SECONDS:.0f}s = "
        f"{STALE_AFTER_SECONDS:.0f}s`: {STALE_REPORT_MULTIPLE} missed reports at the 60s cadence "
        "`jaah_discovery._LAST_SEEN_REFRESH_INTERVAL` debounces the `last_seen` write to — the "
        "fastest the column this bound is measured over can advance."
    )


def format_silent_report(rows: list[NodeRow]) -> str:
    lines = [
        "The control plane's `node-liveness` job (`apps/api/app/node_liveness.py`) found a violated "
        "invariant:",
        "",
        "> **An `enabled` node must have reported inside a bounded multiple of its expected reporting "
        "interval.**",
        "",
        "`nodes.last_seen` is written only for nodes the presence registry reports ONLINE, so a stale "
        "`last_seen` on an `enabled` row means the node is not on the bus — while the control plane still "
        "counts it as dispatchable capacity. Nothing else emits a comment, a notification or a failing "
        "check when that happens: the row simply stops advancing (this is the aws1/aws2 outage, "
        "2026-07-28 — two RUNNING instances silent for six days, `enabled = t` throughout).",
        "",
        _bound_sentence(),
        "",
    ]
    lines += [
        "**DO NOT RESTART THE NODE AS A FIRST ACTION. MEASURE FIRST.** A silent node mid-migration may be "
        "STALE rather than dead, and a restart re-registers it and CLEARS this symptom — so \"the remedy "
        "worked\" is not evidence the node was the fault. Reviving a node whose checkout is behind "
        "`develop` is the MORE DANGEROUS outcome: it puts old code back into service past a coordinator "
        "gate. This nearly happened on 2026-07-28 with two nodes ~800 commits behind.",
        "",
    ]
    for row in rows:
        lines += [
            f"- **{row.node_id}** — last seen "
            f"{_age_text(row.last_seen_age, row.last_seen, 'NEVER (last_seen is NULL)')}",
            f"  - observations: "
            f"{_age_text(row.observed_at_age, row.observed_at, 'NEVER (observed_at is NULL)')}; "
            f"dispatchable pool slots: {row.slots}",
            f"  - **generation: {_generation_text(row)}** — the measurement to take. Is this node's "
            f"reported generation at or past the current `develop` tip? If it is BEHIND or NOT REPORTED, "
            f"STOP: hand it to the coordinator instead of reviving it.",
            "  - if the generation is current AND it should be contributing: then bring the node's "
            "`jaahd` back and confirm `last_seen` advances.",
            "  - if it should NOT be counted: set `enabled = f` on its `nodes` row — always safe, and it "
            "stops the roster asserting capacity that does not exist.",
        ]
    lines += [
        "",
        "Resolve this Task once every enabled node is reporting inside the bound; the next sweep "
        "re-checks and re-raises if it is not.",
    ]
    return "\n".join(lines) + "\n"


def format_not_observing_report(rows: list[NodeRow]) -> str:
    lines = [
        "The control plane's `node-liveness` job (`apps/api/app/node_liveness.py`) found a violated "
        "invariant:",
        "",
        "> **An `enabled` node that reaches the bus must also be OBSERVING.**",
        "",
        "These nodes are on the bus (`last_seen` is fresh) but their observation passes are not landing, "
        "so they contribute nothing while the control plane counts them. A different condition from "
        "silence, with a different remedy: the heartbeat path is healthy and the observation/ingest path "
        "is not.",
        "",
        _bound_sentence(),
        "",
    ]
    for row in rows:
        lines += [
            f"- **{row.node_id}** — last observation "
            f"{_age_text(row.observed_at_age, row.observed_at, 'NEVER (observed_at is NULL)')}, "
            f"but last seen {_age_text(row.last_seen_age, row.last_seen, 'never')}",
            f"  - complete slot enumeration (`worktrees_updated_at`, the clock "
            f"`apps/api/app/pool_state.py` gates DISPATCH on — evidence, not this alarm's subject): "
            f"{_age_text(row.worktrees_updated_age, None, 'NEVER')}",
            "  - if it SHOULD be observing: check the node's observation POST path (its `jaahd` is "
            "running, since the heartbeat arrives — the failure is downstream of presence).",
            "  - if it should NOT: set `enabled = f` on its `nodes` row.",
        ]
    lines += [
        "",
        "Resolve this Task once every enabled, connected node is observing inside the bound.",
    ]
    return "\n".join(lines) + "\n"


def format_stale_release_report(rows: list[NodeRow]) -> str:
    lines = [
        "The control plane's `node-liveness` job (`apps/api/app/node_liveness.py`) found a violated "
        "invariant:",
        "",
        "> **An `enabled` node must be running the published jaahd release.**",
        "",
        "These nodes are heartbeating — this is NOT a liveness alarm — but the release they report "
        "(`config_snapshot->>'version'`) is strictly OLDER than the desired release the control plane "
        "serves them (`GET /api/agent/release-desired` — the version the control plane is ITSELF "
        "running, since CP and .deb ship from one commit), so "
        "they are running old code while the control plane counts them as current capacity. This is the "
        "failure class that ran undetected for 6.4 days: `aws1`/`aws2` heartbeated perfectly while frozen "
        "796 commits back, because their self-update had failed every ~640s since it was armed. A "
        "stale node is not a dead node, and it is the more dangerous of the two mid-migration.",
        "",
    ]
    for row in rows:
        lines += [
            f"- **{row.node_id}** — generation {_generation_text(row)}",
            "  - THERE IS NO PUSH TO RETRY — every node self-converges, cloud and desktop by the identical "
            "path. The daemon asks this control plane what it should be running on "
            "each node-bus reconnect and drops `/run/jaah/requests/upgrade`; the root "
            "`jaahd-upgrade.path` unit sees the marker and runs `jaahd-update.service`, with "
            "`jaahd-update.timer` as a ~30min fallback poll (`apps/jaahd/upgrade_request.py`, "
            "`/usr/libexec/jaahd-update`).",
            "  - so DIAGNOSE ON THE NODE ITSELF, over Tailscale SSH: `systemctl status "
            "jaahd-update.timer jaahd-upgrade.path jaahd-update.service`, then `journalctl -u "
            "jaahd-update.service -n 200` for the `jaahd-update:` lines "
            "— every pass that skips names "
            "its own reason (auto-update off for the node in Settings, no `/etc/jaah/jaahd.env`, the CP "
            "unreachable or answering an HTTP status, or the version not yet published to the apt repo). "
            "A `jaahd-upgrade.path` that is not enabled is the silent one: the node then converges only "
            "on the 30min timer, and nothing else anywhere says so.",
            "  - the remedy is a DEPLOY, not a restart: a restart relaunches the same installed package. "
            "The desired version is this control plane's OWN version and has no writer to move, so a node "
            "that still will not catch up is an apt, credential or unit problem ON THAT NODE — not a "
            f"pointer to re-push at it. `apt-get install jaah=<version>` on `{row.node_id}` is the "
            "manual last resort; dpkg's postinst restarts the daemon.",
            "  - if it must not serve work until then: set `enabled = f` on its `nodes` row.",
        ]
    lines += [
        "",
        "Resolve this Task once every enabled node reports the published release.",
    ]
    return "\n".join(lines) + "\n"


def format_config_drift_report(rows: list[NodeRow]) -> str:
    lines = [
        "The control plane's `node-liveness` job (`apps/api/app/node_liveness.py`) found a violated "
        "invariant:",
        "",
        "> **An `enabled` node's live config must match the declaration on its `nodes` row.**",
        "",
        "These nodes report a `config_snapshot` that contradicts their own `nodes.config_desired` — the "
        "roster's statement of what the node's fleet-critical knobs SHOULD be. They are not necessarily "
        "unhealthy: a node can be fresh, observing, enrolled and current while running with its sweep "
        "or its auto-update switched off underneath it, which is a fleet quietly doing less than "
        "the roster says it does. That is the condition this names.",
        "",
        "`config_desired` is DATA on the row — `PUT /api/nodes/{node_id}` with "
        "`{\"config_desired\": {...}}`, no deploy and no commit.",
        "",
    ]
    for row in rows:
        lines.append(f"- **{row.node_id}**")
        for key, want, got in row.drift:
            if got is None:
                lines.append(
                    f"  - `{key}` — declared `{want}`, NOT REPORTED. Either it is unset on the node, or "
                    "the key is absent from the daemon's `CONFIG_SNAPSHOT_ALLOWLIST` "
                    "(`apps/jaahd/main.py`) and so can never be seen from here — check the allowlist "
                    "before changing anything on the node."
                )
            else:
                lines.append(f"  - `{key}` — declared `{want}`, reported `{got}`")
        lines += [
            f"  - repair: ON {row.node_id} itself, `sudo python3 set_node_env.py --set KEY=VALUE "
            "--restart` — the script is `infra/jaah/set_node_env.py` in a acme checkout, stdlib-only, "
            "and no node package ships it, so copy it over first.",
            "  - if the DECLARATION is what is wrong, fix the row instead: `config_desired` on the "
            "node's `nodes` row is the authority, and a value nobody decided should not be in it.",
        ]
    lines += [
        "",
        "Resolve this Task once every enabled node reports what its row declares.",
    ]
    return "\n".join(lines) + "\n"


# ── Delivery ─────────────────────────────────────────────────────────────────────

def _rearm_suppressed(
    dedup_key: str,
    recent_settled_task: Optional[Callable[..., Optional[dict]]],
) -> bool:
    """True when this key's card was closed by somebody else inside the cooldown — do not re-file it.

    Fail-open: an uninjected seam or failed lookup suppresses nothing.
    A closure carrying `CLEARED_SUMMARY_PREFIX` is this sweep's own clear and re-arms at once.

    Accepted cost: a dismissal followed by a clear leaves the dismissal as newest, so a new incident
    inside the window is suppressed up to an hour late — still reported on stderr and in the rc.
    `awaiting_response` would evade both guards, but a `draft` row is never dispatched, so no runner can
    park it there.
    """
    if recent_settled_task is None:
        return False
    try:
        settled = recent_settled_task(
            persona=ALARM_TASK_PERSONA,
            dedup_key=dedup_key,
            within_seconds=ALARM_REARM_COOLDOWN_SECONDS,
            cleared_prefix=CLEARED_SUMMARY_PREFIX,
        )
    except Exception as exc:  # noqa: BLE001 — see FAIL-OPEN above; never costs an alarm
        warn(f"  ·  could not check the re-arm cooldown for {dedup_key} ({exc}) — filing the alarm anyway")
        return False
    if settled is None:
        return False
    # A boolean, not the close line: the row's text is the control plane's. The prefix test is a prefix,
    # not a `LIKE`, so typed `%`/`_` cannot become wildcards.
    if settled.get("cleared_by_sweep"):
        return False
    # A machine close suppresses nothing — any registered node's token may resolve, so honouring it
    # would let a node mute the alarm about itself. Unattributed falls through to filing.
    if not settled.get("closed_by_human"):
        return False
    warn(
        f"  ·  NOT re-filing the {dedup_key} alarm — Task {settled.get('id')} was closed "
        f"{float(settled.get('age_seconds') or 0.0) / 60.0:.0f}m ago while the condition still holds. "
        f"The condition IS violated; the card re-arms after "
        f"{ALARM_REARM_COOLDOWN_SECONDS / 3600.0:.0f}h, or at once if it clears first."
    )
    return True


def _open_alarm_task(
    title: str,
    dedup_key: str,
    body: str,
    create_task: Callable[..., dict],
    recent_settled_task: Optional[Callable[..., Optional[dict]]] = None,
) -> bool:
    """Open (or refresh) one deduped hidden console alarm Task. True if delivered.

    `body` is the operator's runbook; the row files into the Alarm project as that project's org's, whichever
    tenant's nodes it names (`routes/agent/task_reads.py::create_system_task`). Each caller passes its own
    title and dedup_key.

    The cooldown is checked first (`_rearm_suppressed`). A suppressed pass returns True because nothing
    failed — do not read it as "a card is open".
    """
    if _rearm_suppressed(dedup_key, recent_settled_task):
        return True
    try:
        result = create_task(
            persona=ALARM_TASK_PERSONA,
            title=title,
            body=body,
            state=ALARM_TASK_STATE,
            dedup_open=True,
            dedup_key=dedup_key,
        )
    except Exception as exc:  # noqa: BLE001 — any delivery failure (API down, no token, HTTP error)
        # Never silent: the full report goes to stderr and the caller keeps the alarm exit code.
        warn(f"  🚨 ALARM NOT DELIVERED — could not open the alarm Task ({exc})")
        warn("       The invariant IS violated. Full report follows, unread-log-only — act on it now:")
        for line in body.splitlines():
            warn(f"       {line}")
        return False
    verb = "opened" if result.get("created") else "refreshed"
    warn(f"  🚨 alarm delivered: {verb} Task {result.get('id')} (persona {ALARM_TASK_PERSONA}, "
         f"dedup_key {dedup_key}) — visible on /tasks")
    return True


_ALARM_CONDITIONS = (
    (VERDICT_SILENT, SILENT_TITLE, SILENT_DEDUP_KEY, format_silent_report),
    (VERDICT_NOT_OBSERVING, NOT_OBSERVING_TITLE, NOT_OBSERVING_DEDUP_KEY,
     format_not_observing_report),
    (VERDICT_STALE_RELEASE, STALE_RELEASE_TITLE, STALE_RELEASE_DEDUP_KEY,
     format_stale_release_report),
    (VERDICT_CONFIG_DRIFT, CONFIG_DRIFT_TITLE, CONFIG_DRIFT_DEDUP_KEY, format_config_drift_report),
)


def _resolve_cleared_alarm(
    dedup_key: str,
    find_task: Callable[..., Optional[int]],
    resolve_task: Callable[..., dict],
) -> bool:
    """Close the Task this condition opened, now that the condition is gone. True if one was resolved.

    Keyed on `dedup_key`, matched exactly by `GET /api/agent/tasks/system/open`. Best effort: an
    unresolved cleared alarm is stale UI, so it warns rather than failing the sweep.
    """
    try:
        task_id = find_task(persona=ALARM_TASK_PERSONA, dedup_key=dedup_key)
        if task_id is None:
            return False
        # The prefix is load-bearing: `_rearm_suppressed` reads it.
        resolve_task(task_id, outcome_summary=f"{CLEARED_SUMMARY_PREFIX} — {dedup_key}")
    except Exception as exc:  # noqa: BLE001 — see the docstring; stale UI, never a failed sweep
        warn(f"  ·  could not resolve the cleared alarm for {dedup_key} ({exc}) — it stays open; "
             f"the condition itself is clear")
        return False
    warn(f"  ✓  resolved Task {task_id} — the {dedup_key} condition is clear")
    return True


def deliver_alarms(
    grouped: dict[str, list[NodeRow]],
    *,
    create_task: Callable[..., dict],
    find_task: Callable[..., Optional[int]],
    resolve_task: Callable[..., dict],
    recent_settled_task: Optional[Callable[..., Optional[dict]]] = None,
) -> int:
    """Deliver one Task per violated condition, and resolve the Task of every condition that is not.
    Returns the number of conditions delivered (resolutions are not counted).

    `create_task` / `find_task` / `resolve_task` are required. The suite passes
    recorders and must never create a real Task or reach the network. `recent_settled_task` is
    optional (see `_rearm_suppressed`).
    """
    delivered = 0
    for verdict, title, dedup_key, formatter in _ALARM_CONDITIONS:
        rows = grouped.get(verdict) or []
        if not rows:
            _resolve_cleared_alarm(dedup_key, find_task, resolve_task)
            continue
        _open_alarm_task(title, dedup_key, formatter(rows), create_task,
                         recent_settled_task=recent_settled_task)
        delivered += 1
    return delivered


# ── Report + sweep ───────────────────────────────────────────────────────────────

def _report_node(row: NodeRow, verdicts: list[str]) -> None:
    """Print one node's verdicts. The alarms are deliberately the loudest thing this module emits."""
    if verdicts == [VERDICT_DISABLED]:
        warn(f"  ·  {row.node_id} — disabled (enabled=f); not asserted on")
        return
    if verdicts == [VERDICT_LIVE]:
        warn(
            f"  ·  {row.node_id} — LIVE (seen "
            f"{_age_text(row.last_seen_age, row.last_seen, 'never')}, observed "
            f"{_age_text(row.observed_at_age, row.observed_at, 'never')}, {row.slots} slot(s), "
            f"generation {_generation_text(row)})"
        )
        return
    for verdict in verdicts:
        if verdict == VERDICT_SILENT:
            warn(
                f"  🚨 {row.node_id} — ENABLED BUT SILENT: last seen "
                f"{_age_text(row.last_seen_age, row.last_seen, 'NEVER (last_seen is NULL)')}, "
                f"bound {STALE_AFTER_SECONDS:.0f}s"
            )
            warn("       Invariant violated: an enabled node must have reported inside the bound. The "
                 "control plane counts it as dispatchable capacity that does not exist.")
            warn(f"       MEASURE FIRST, do not restart: generation {_generation_text(row)}. A restart "
                 "clears this symptom even when the node is STALE, not dead — reviving old code past a "
                 "coordinator gate is the worse outcome. Setting enabled=f is always safe.")
        elif verdict == VERDICT_NOT_OBSERVING:
            warn(
                f"  🚨 {row.node_id} — ENABLED AND ON THE BUS BUT NOT OBSERVING: last observation "
                f"{_age_text(row.observed_at_age, row.observed_at, 'NEVER (observed_at is NULL)')}, "
                f"bound {STALE_AFTER_SECONDS:.0f}s"
            )
            warn("       A different fault from silence: presence is healthy, the observation path is "
                 "not, and the node contributes nothing while being counted.")
        elif verdict == VERDICT_STALE_RELEASE:
            warn(
                f"  🚨 {row.node_id} — ENABLED BUT RUNNING A STALE RELEASE: generation "
                f"{_generation_text(row)} is older than the desired release ({RELEASE_DESIRED_PATH})"
            )
            warn("       Heartbeating, and running old code. The remedy is a DEPLOY (a restart relaunches "
                 "the same pinned release) — or enabled=f until it is deployed.")
        elif verdict == VERDICT_CONFIG_DRIFT:
            drifted = ", ".join(
                f"{key}: declared {want}, reported {got if got is not None else 'NOT REPORTED'}"
                for key, want, got in row.drift
            )
            warn(f"  🚨 {row.node_id} — LIVE CONFIG CONTRADICTS ITS DECLARATION: {drifted}")
            warn("       Not a liveness fault: the node reports fine and runs knobs its own `nodes` row "
                 "says it should not. Repair with infra/jaah/set_node_env.py run on the node, or fix the "
                 "declaration (`nodes.config_desired`) if that is the half that is wrong.")


def sweep(
    rows: list[NodeRow],
    *,
    create_task: Callable[..., dict],
    find_task: Callable[..., Optional[int]],
    resolve_task: Callable[..., dict],
    bound: float = STALE_AFTER_SECONDS,
    release_grace_active: bool = False,
    recent_settled_task: Optional[Callable[..., Optional[dict]]] = None,
) -> int:
    """Assert the invariant over `rows`, report, deliver. Returns EXIT_OK or EXIT_INVARIANT_VIOLATED.

    Pure apart from the delivery seams. All three delivery seams are forwarded, since the resolve half
    runs on every pass. `release_grace_active` is pre-computed by the caller
    (`release_desired_in_grace`), keeping `sweep` clock-free.
    """
    warn(
        f"node-liveness: asserting every enabled node reported within {STALE_AFTER_SECONDS:.0f}s "
        f"({STALE_REPORT_MULTIPLE} x {REPORT_INTERVAL_SECONDS:.0f}s last_seen write cadence)"
    )
    grouped = classify(rows, bound, release_grace_active)
    for row in rows:
        _report_node(row, verdicts_for(row, bound, release_grace_active))

    enabled = [r for r in rows if r.enabled]
    if not rows:
        warn("  (the nodes roster is empty — nothing to assert)")
    elif not enabled:
        warn("  (no enabled nodes — nothing to assert)")

    # Reported, not alarmed. Also the promotion signal: at zero, absent generation could become a hard
    # alarm.
    unplaceable = [r for r in enabled if r.placement == GENERATION_UNKNOWN]
    if unplaceable:
        warn(
            f"  ·  {len(unplaceable)} enabled node(s) report no placeable generation "
            f"({', '.join(r.node_id for r in unplaceable)}) — NOT an alarm, because it would also fire "
            "when the desired release itself is simply unreadable. Reported so the day this reaches 0 is "
            "observable, at which point absent should become a hard alarm."
        )

    # Likewise reported without alarming; printed only when the grace window is excusing a BEHIND row.
    behind_in_grace = [r for r in enabled if r.placement == GENERATION_BEHIND] if release_grace_active else []
    if behind_in_grace:
        warn(
            f"  ·  {len(behind_in_grace)} enabled node(s) report BEHIND "
            f"({', '.join(r.node_id for r in behind_in_grace)}) but `release_desired` changed inside its "
            f"{RELEASE_DESIRED_GRACE_SECONDS:.0f}s grace window — NOT an alarm: these nodes may still be "
            "converging (a node mid-update reads behind until its timer fires). Still alarms once the release "
            "has been stable past the grace window and a node is still behind."
        )

    alarm_verdicts = [
        v for v in (VERDICT_SILENT, VERDICT_NOT_OBSERVING, VERDICT_STALE_RELEASE,
                    VERDICT_CONFIG_DRIFT)
        if grouped.get(v)
    ]
    if not alarm_verdicts and enabled:
        warn(
            f"  (all {len(enabled)} enabled node(s) verified reporting within "
            f"{STALE_AFTER_SECONDS:.0f}s, observing, not behind the desired release, and matching "
            f"what their rows declare)"
        )
    for verdict in alarm_verdicts:
        if verdict == VERDICT_CONFIG_DRIFT:
            # Not the capacity sentence: a drifted node is present; it lacks its declared configuration.
            warn(
                f"node-liveness: {len(grouped[verdict])} node(s) CONFIG-DRIFT (🚨 above) — the control "
                "plane's declaration of what these nodes should run is not what they report running."
            )
            continue
        warn(
            f"node-liveness: {len(grouped[verdict])} node(s) {verdict.upper()} (🚨 above) — the control "
            "plane believed in capacity it does not have, and until now nothing said so."
        )
    # One delivery pass either way: `deliver_alarms` also resolves cleared conditions, so a clean pass
    # must reach it.
    deliver_alarms(grouped, create_task=create_task, find_task=find_task, resolve_task=resolve_task,
                   recent_settled_task=recent_settled_task)
    return EXIT_INVARIANT_VIOLATED if alarm_verdicts else EXIT_OK

