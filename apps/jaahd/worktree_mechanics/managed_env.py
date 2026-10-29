"""CP-managed `.env` values — the marker-tracked assignments the control plane owns: apply, withdraw, read back."""
from __future__ import annotations

import logging
import os
import re

from worktree_mechanics import env_file, ports

log = logging.getLogger("jaahd.worktree_mechanics.managed_env")


def _env_raw_value(text: str, name: str) -> str:
    """NAME's right-hand side in TEXT, VERBATIM — first `^\\s*NAME=` line, everything after the `=`, the
    line terminator removed and NOTHING else touched. "" when the key is absent.

    NOT `_env_port`, and the difference is the reason this exists. That reader runs its answer through
    `_clean_env_value`, which cuts at the first `#` and strips every quote and space ANYWHERE in the
    value — correct for a port, and silent corruption for anything else: `NODE_ID="node a"` comes back as
    `nodea`. This is the CAPTURE side of a rebuild, where a value is read only to be written back
    unchanged, so any normalisation at all is a value the slot did not have before.

    The GRAMMAR is `_apply_env_assignments`'s narrow one, deliberately unlike `_remove_env_keys`'s: what
    is captured here is re-injected by that function, so a form it cannot rewrite in place is a form this
    must not capture — it would be appended as a duplicate instead."""
    pat = re.compile(r"^\s*" + re.escape(name) + r"=(.*?)\r?$")
    for line in text.splitlines():
        match = pat.match(line)
        if match:
            return match.group(1)
    return ""


def _managed_env_value(value: object) -> "str | None":
    """One managed value as the TEXT its `.env` line carries — SINGLE-QUOTED — or None when it cannot be
    rendered at all.

    Quoting is this renderer's assigned job (`apps/api/app/models/projects.py::ProjectEnvVar`): a compose env file
    interpolates unquoted and double-quoted values, so `LEAK=${NODE_TOKEN}` would read another variable.

    Refused (None, caller warns BY NAME): a non-`str` (`str()` could leak a repr); any `_ENV_LINE_BREAKERS`
    character (a value reading back as two lines could forge the `# jaah-managed:` marker that decides
    what a mapless claim DELETES); a single quote (dotenv single quotes cannot represent one)."""
    if not isinstance(value, str):
        return None
    if any(ch in value for ch in _ENV_LINE_BREAKERS):
        return None
    if "'" in value:
        return None
    return "'" + value + "'"


# ── The managed-key marker: the node's memory of what the last overlay laid down ───────────────────────
# When a project deletes its LAST tab row the CP sends NO `env_vars` key — indistinguishable from never
# opting in — so without a record the previous claim's resolved secrets would stay in `.env` forever. The
# CP keeps no record (`routes/agent/slot_intents.py` nulls `env_vars` at ack), so the rebuild writes the applied names
# here and a mapless claim withdraws exactly those. A `.env` with no marker is never touched.
_MANAGED_MARKER_PREFIX = "# jaah-managed:"
# Bounds a marker that is not this node's (a deletion list somebody else chose).
_MAX_MARKER_NAMES = 512
# Every `splitlines` line break, plus NUL; a `.env` line ends at `\n` alone.
_ENV_LINE_BREAKERS = "\n\r\x00\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"


def _env_physical_lines(text: str) -> "list[str]":
    """TEXT's physical `.env` lines, terminators dropped — `split("\n")`, NOT `splitlines()`.

    A security distinction: `splitlines` also breaks on `\x0b \x0c \x1c \x1d \x1e \x85 \u2028 \u2029`,
    so a managed value carrying one would read back as a forged `# jaah-managed:` line (a deletion list
    for every slot). Rejoin with `"\n".join(...)`, which round-trips exactly."""
    return text.split("\n")


def _managed_marker_line(names: "list[str] | tuple[str, ...]") -> str:
    """The one marker line, SORTED — a comment, so every `.env` consumer already ignores it.

    Sorted: same configuration → byte-identical file, whatever key order `jsonb` gives."""
    return f"{_MANAGED_MARKER_PREFIX} {','.join(sorted(names))}\n"


_ESCAPE_PAIR_RX = re.compile(r"\\.")


def _mask_escapes(text: str, quote: str) -> str:
    """TEXT with every backslash-escaped pair blanked, so a `\\"` cannot be read as a quote — but only
    for the DOUBLE-quoted form, because a dotenv single-quoted string has no escapes at all (which is why
    `_managed_env_value` refuses a value containing `'` outright rather than splicing one).

    Without it a multi-line JSON value reads as closed on its first line, and an interior
    `# jaah-managed:` line would be deleted from the middle of it. Length-preserving, so indexes hold."""
    return _ESCAPE_PAIR_RX.sub("__", text) if quote == '"' else text


def _marker_line_indexes(lines: "list[str]") -> "list[int]":
    """The indexes of LINES that are marker lines — and ONLY those OUTSIDE an open quoted value.

    A quoted value can span physical lines (`_managed_value_span`); a marker-shaped line inside a
    certificate must neither be read as ours nor deleted from the value. The walk tracks the open quote;
    a marker counts only between assignments, where `_managed_marker_line` writes it."""
    open_quote: "str | None" = None
    found: "list[int]" = []
    for i, line in enumerate(lines):
        if open_quote is not None:
            if open_quote in _mask_escapes(line, open_quote):
                open_quote = None
            continue
        if line.startswith(_MANAGED_MARKER_PREFIX):
            found.append(i)
            continue
        match = env_file._ENV_KV_RX.match(line)
        if match:
            value = match.group(2)
            quote = value[:1]
            if quote in ("'", '"') and _mask_escapes(value, quote).count(quote) < 2:
                open_quote = quote
    return found


def _managed_marker_names(text: str) -> "list[str] | None":
    """The names TEXT's marker records, or None when it carries NO marker at all.

    `None` = never written by an overlay (leave alone); `[]` = our marker naming nothing (still ours to
    clear). Column 0 only; each name held to `_port_var_name_ok` (the file is hand-editable)."""
    lines = _env_physical_lines(text)
    indexes = _marker_line_indexes(lines)
    if not indexes:
        return None
    found: "list[str]" = []
    seen: set[str] = set()
    for i in indexes:
        for raw in lines[i][len(_MANAGED_MARKER_PREFIX):].split(","):
            name = raw.strip()
            if not name or name in seen or not ports._port_var_name_ok(name):
                continue
            if len(seen) >= _MAX_MARKER_NAMES:
                # Over the ceiling: not this node's marker. Refused whole (`None`, the no-op answer).
                log.warning(
                    "apply_managed_env: a .env marker names more than %d variables — refusing to "
                    "withdraw off a line this node cannot have written", _MAX_MARKER_NAMES,
                )
                return None
            seen.add(name)
            found.append(name)
    return found


def _strip_managed_marker(text: str) -> str:
    """TEXT with every marker line removed — the same quote-aware set `_managed_marker_names` reads, so
    the two can never disagree about which physical lines are markers."""
    lines = _env_physical_lines(text)
    drop = set(_marker_line_indexes(lines))
    return "\n".join(line for i, line in enumerate(lines) if i not in drop)


def _applicator_owned_names(
    port_vars: "list[str] | tuple[str, ...]", band: "dict[str, int] | None",
) -> "set[str]":
    """Every name an APPLICATOR writes after `apply_managed_env` returns, and therefore every name the
    marker must NOT record — DERIVED from the same two inputs, never a maintained list.

    `apply_port_band`, `apply_stack_identity` and `apply_node_identity_env` run afterwards; withdrawing
    one of their names later would delete a line an applicator owns (a warm stack's tier-2 port, or a
    banded line whose `localhost:<old>` rewrite would lose its old value). Not `captured`: a declared
    tier-2 var blank or absent in `.env.example` is not captured and would slip into the marker."""
    owned = {str(v).strip() for v in (port_vars or []) if str(v).strip()}
    owned |= {str(v).strip() for v in (band or {}) if str(v).strip()}
    owned.add("NODE_ID")
    owned.add("COMPOSE_PROJECT_NAME")
    return owned


def _withdraw_managed_env(wt: str) -> None:
    """A claim carrying NO map, on a slot a previous overlay DID write: remove exactly the names the
    marker records, and the marker — and nothing else.

    Not a rebuild: nothing reads `.env.example`, no line this node did not write is disturbed.
    Removal, never blanking (`_remove_env_keys`). Refuses no `.env` and an unreadable/over-cap file.
    Fires on every mapless claim of a marked slot: the node cannot tell "catalogue emptied" from a claim
    with no intent behind it, and stripping is the safe reading — the alternative leaves a previous task's
    resolved secrets for the next occupant. The next fleet claim rebuilds them."""
    env_path = os.path.join(wt, ".env")
    if not os.path.isfile(env_path):
        return
    # A marker in `.env.example` (seeded verbatim) is not this node's record: it disarms the withdrawal —
    # fail SAFE (a stale value is recoverable, a deleted one is not) and say so.
    example_path = os.path.join(wt, ".env.example")
    try:
        if os.path.isfile(example_path) and _managed_marker_names(env_file.env_text(example_path)) is not None:
            log.warning(
                "apply_managed_env: %s ships a %s line in its .env.example — every slot is seeded with "
                "it, so a marker in .env is not this node's record of what it applied. Withdrawing "
                "NOTHING; remove that line from the example", wt, _MANAGED_MARKER_PREFIX,
            )
            return
    except OSError as exc:
        log.warning("apply_managed_env: %s could not be read (%s) — withdrawing nothing", example_path, exc)
        return
    try:
        current_text = env_file._env_text_for_rewrite(env_path)
    except OSError as exc:
        log.warning(
            "apply_managed_env: %s could not be read whole (%s) — leaving it exactly as it is rather "
            "than withdrawing managed variables from a text this pass could not capture", env_path, exc,
        )
        return
    names = _managed_marker_names(current_text)
    if names is None:
        # NO MARKER ⇒ NOTHING AT ALL. The never-managed slot, every claim with no intent behind it — all
        # unchanged, and this return is what says so.
        return
    text = _strip_managed_marker(env_file._remove_env_keys(current_text, names))
    if current_text == text:
        # BELT AND BRACES, not a reachable case: `names is not None` means a marker line was found, and
        # `_strip_managed_marker` always removes one. Kept as the mirror of `_set_env_vars`' real "a pass
        # that changes nothing writes nothing" promise, so a future reader of the two need not check
        # which of them can actually fire.
        return
    try:
        env_file._env_write_text(env_path, text)
    except OSError as exc:
        log.error("apply_managed_env: could not withdraw managed variables from %s (%s) — %s left in place",
                  env_path, exc, names)
        raise
    log.info(
        "apply_managed_env: %s was claimed with no managed map and its .env named %s as managed — "
        "removed those lines and the marker, and nothing else. The project's catalogue is empty",
        wt, names,
    )


def apply_managed_env(
    wt: str, env_vars: "dict | None", *, n: "int | str" = 1,
    port_vars: "list[str] | tuple[str, ...]" = (), band: "dict[str, int] | None" = None,
) -> None:
    """REBUILD a pool slot's `.env` from `.env.example` and lay the control plane's configured variables
    over it, preserving the values a bare re-copy would strand. `.env` is DERIVED on every claim.

    `env_vars` is `{"values": {NAME: value}, "omit": [NAME, ...]}` (the lease intent's pinned shape).
    ABSENT (`None`) NEVER REBUILDS — a claim can also arrive with no intent,
    where a rebuild would wipe managed values. Absent
    hands off to `_withdraw_managed_env` (marker-only). The order:
      1. CAPTURE the values the rebuild would strand (the preserve set below).
      2. REBUILD: the file becomes `.env.example`, verbatim.
      3. OVERLAY `values`, then REMOVE every `omit` name (`_remove_env_keys`: removal, not blanking).
      4. RE-INJECT the captured values LAST, so they beat a tab value (a tab value on a tier-2 port var
         would otherwise move a port a warm stack is bound to — worktree-pool-policy.md §Ports).
      5. RECORD `# jaah-managed: NAME,…` — the names left holding a TAB value.
    Runs before the applicators (`apply_port_band` etc.), so band ports, `COMPOSE_PROJECT_NAME` and
    `NODE_ID` still win. `NODE_TOKEN`/`NODE_NAME` are not re-derived, so names are held to
    `_port_var_name_ok` (the node is the last line).

    Preserve set, DERIVED: declared `port_vars` the band does not name, plus `NODE_ID` (handed no id,
    `apply_node_identity_env` leaves the key alone, so a rebuild would otherwise keep a blank seed).
    `DEV_NODE_ID`/`DEV_NODE_TOKEN` deliberately not preserved (re-run `./dc seed --dev`).

    Every refusal leaves `.env` exactly as it was: slot 0 (the main clone, an operator's file); no
    `.env.example`; no `.env` (the writer would create it `0664` under the umask); `values`/`omit` of
    the wrong type (unparseable is not an empty catalogue); an unreadable or over-cap current `.env`;
    and a rebuilt text over the read cap (`_set_env_vars` would then refuse the band). Names logged,
    values never."""
    if str(n) == "0":
        # Slot 0 is the main clone — the operator's own `.env`. First, so it covers the withdrawal too.
        return
    # `None` only, not `if not env_vars`: an emptied catalogue sends `{"values": {}, "omit": []}`, which
    # IS a rebuild with an empty overlay (a removed tab variable reverts to `.env.example`).
    if env_vars is None:
        _withdraw_managed_env(wt)
        return
    if not isinstance(env_vars, dict):
        # Unparseable is not an empty catalogue — nor a withdrawal (that would strip values fleet-wide).
        log.warning(
            "apply_managed_env: %s got `env_vars` as %s, not an object — doing nothing at all. An "
            "unparseable payload is not an emptied catalogue and must not withdraw",
            wt, type(env_vars).__name__,
        )
        return
    env_path = os.path.join(wt, ".env")
    example_path = os.path.join(wt, ".env.example")
    if not os.path.isfile(example_path):
        log.warning(
            "apply_managed_env: %s ships no .env.example — the CP's managed variables were NOT applied. "
            "The rebuild has no source, and truncating .env instead would strand the slot (the repo "
            "must ship one)", wt,
        )
        return
    if not os.path.isfile(env_path):
        log.warning(
            "apply_managed_env: %s has no .env to rebuild — the CP's managed variables were NOT applied", wt,
        )
        return

    raw_values = env_vars.get("values")
    raw_omit = env_vars.get("omit")
    if not isinstance(raw_values, dict) and raw_values is not None:
        log.warning(
            "apply_managed_env: %s got `values` as %s, not an object — SKIPPING the rebuild entirely. A "
            "shape this node cannot parse is not an empty catalogue, and rebuilding on one would hand "
            "back a slot with every managed secret blank", wt, type(raw_values).__name__,
        )
        return
    if not isinstance(raw_omit, (list, tuple)) and raw_omit is not None:
        log.warning(
            "apply_managed_env: %s got `omit` as %s, not a list — SKIPPING the rebuild entirely rather "
            "than turning R9's hard negative into a no-op that ships the example's blanks",
            wt, type(raw_omit).__name__,
        )
        return
    values: dict[str, str] = {}
    for key, value in (raw_values or {}).items():
        name = str(key or "").strip()
        # THE NAME, held to the same predicate the declared-port path uses — charset (a name carrying a
        # newline or an `=` would inject an arbitrary second assignment) AND the three names that would
        # SHADOW this node's own identity or credential. See the docstring for why those three are not
        # covered by "the applicators run after this and win".
        if not name or not ports._port_var_name_ok(name):
            log.warning(
                "apply_managed_env: %s skipping managed variable %r — a .env KEY must be an env "
                "identifier and must not be one of %s, which ./dc force-reads out of .env into every "
                "stack and no applicator here rewrites", wt, name or key, sorted(ports._PORT_VAR_RESERVED_NAMES),
            )
            continue
        rendered = _managed_env_value(value)
        if rendered is None:
            # BY NAME, NEVER BY VALUE — and this is the log line a resolved secret would otherwise reach.
            log.warning(
                "apply_managed_env: %s skipping managed variable %s — its value is not a string, or "
                "carries a newline/NUL, or contains a single quote, and a single-quoted .env line "
                "cannot hold any of those", wt, name,
            )
            continue
        values[name] = rendered
    # THE SAME SHAPE FILTER ON `omit`, for symmetry and for the reserved names: `omit: ["NODE_TOKEN"]`
    # would DELETE that line, and `./dc`'s guard says an unlisted var "keeps whatever ambient value a
    # reused shell carried in" — a removal is a way to shadow it too. A name that is not an identifier
    # can never match a `.env` line anyway, so filtering costs nothing and is said out loud.
    omit: list[str] = []
    for raw_name in (raw_omit or []):
        name = str(raw_name or "").strip()
        if not name or not ports._port_var_name_ok(name):
            log.warning("apply_managed_env: %s skipping omit entry %r — see the name rule above",
                        wt, name or raw_name)
            continue
        omit.append(name)
    omit = sorted(set(omit))

    # ── 1. CAPTURE, off the file as it stands ──────────────────────────────────────────────────────────
    # ONE READ, and an unreadable one is a REFUSAL rather than a reason to press on: `env_text` truncates
    # silently at `_MAX_ENV_BYTES` while `_env_text_for_rewrite` raises, and capturing from a short read
    # then writing the whole file is how a preserved value past the cap would be destroyed by the very
    # pass that exists to preserve it.
    try:
        current_text = env_file._env_text_for_rewrite(env_path)
    except OSError as exc:
        log.warning(
            "apply_managed_env: %s could not be read whole (%s) — leaving it exactly as it is rather "
            "than rebuilding over values this pass could not capture", env_path, exc,
        )
        return
    # DERIVED, and derived from the same two inputs `apply_port_band` derives its own tier-2 split from,
    # so the two cannot drift: declared `port_vars` minus the names the CP's band covers.
    declared = [str(v).strip() for v in (port_vars or []) if str(v).strip()]
    banded = {str(v).strip() for v in (band or {})}
    preserve = [v for v in dict.fromkeys(declared) if v not in banded]
    preserve.append("NODE_ID")
    captured: dict[str, str] = {}
    for name in sorted(dict.fromkeys(preserve)):
        # EMPTY IS NOT CAPTURED. A blank here is the example's own seed surviving from a slot that has
        # never been banded or never been given a node identity — re-injecting it would only write the
        # value the rebuild is about to lay down anyway, and for a port var it would look to
        # `apply_auto_ports` like a declared var holding a NON-PORT value and earn a spurious warning.
        current = _env_raw_value(current_text, name)
        if current:
            captured[name] = current

    # ── 2/3/4. REBUILD, OVERLAY + REMOVE, RE-INJECT — one text, ONE write ──────────────────────────────
    # One `_env_write_text` for all steps: each write is a delegated round trip.
    try:
        # `_env_text_for_rewrite` for its refusal of a truncated read. Marker STRIPPED: the example's
        # marker is not this node's memory and would union into step 5's list permanently.
        text = _strip_managed_marker(env_file._env_text_for_rewrite(example_path))
    except OSError as exc:
        log.warning(
            "apply_managed_env: %s is unreadable (%s) — .env left exactly as it was rather than rebuilt "
            "from a source this pass could not read whole", example_path, exc,
        )
        return
    # Before any assignment: an appended key would glue onto an unterminated last line.
    if text and not text.endswith("\n"):
        text += "\n"
    # Sorted at every step: idempotence rests on this order plus a tracked rebuild source.
    text = env_file._apply_env_assignments(text, {k: values[k] for k in sorted(values)})
    # After the overlay: `omit` is a HARD negative.
    text = env_file._remove_env_keys(text, omit)
    # LAST — see the docstring on why the captured values must beat the overlay.
    text = env_file._apply_env_assignments(text, {k: captured[k] for k in sorted(captured)})
    # The marker, always the last line, MINUS every name an applicator writes afterwards (argued at
    # `_applicator_owned_names`). No names ⇒ no marker.
    marker_keys = set(values) - _applicator_owned_names(port_vars, band)
    if marker_keys:
        text = (text if text.endswith("\n") or not text else text + "\n") + _managed_marker_line(marker_keys)
    if len(text) >= env_file._MAX_ENV_BYTES:
        # THE NEXT LINE IN `claim_mechanics` IS `apply_port_band`, and `_set_env_vars` WARNS AND RETURNS
        # on a file this size rather than raising. So writing this would not merely make an awkward
        # `.env` — it would silently disarm the band, and `_set_env_vars`'s own docstring names that
        # outcome: "every slot claimed in this window binds the SAME host ports".
        log.warning(
            "apply_managed_env: %s would rebuild to %d bytes, at or past the %d-byte cap this module can "
            "still rewrite — refusing, and leaving .env as it is. Shrink the managed values", wt,
            len(text), env_file._MAX_ENV_BYTES,
        )
        return
    if current_text == text:
        # A pass that changes nothing writes nothing, exactly as `_set_env_vars` promises — which is what
        # makes a second claim of an already-converged slot free.
        return
    try:
        env_file._env_write_text(env_path, text)
    except OSError as exc:
        # RAISED, for `_set_env_vars`'s reason: a whole-file write that failed may already have reached
        # the disk, and a half-rebuilt `.env` is a slot whose stack starts on a mixture of two files.
        log.error("apply_managed_env: could not rebuild %s (%s) — %s not applied",
                  env_path, exc, sorted(values))
        raise
    log.info(
        "apply_managed_env: %s rebuilt .env from .env.example — set %s, omitted %s, preserved %s",
        wt, sorted(values), omit, sorted(captured),
    )
