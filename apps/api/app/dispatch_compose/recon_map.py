"""The fenced RECON MAP section: a prerequisite stage's recon rendered for the next runner."""
from __future__ import annotations

import json
import secrets

from app.dispatch_compose.fencing import defang_fence_markers

# Anti-runaway cap for the fenced recon_map section (mirrors `_PREREQ_OUTPUT_MAX_CHARS`, app/routes/
# tasks.py) — a defensive guard against a pathological artifact, not a content limiter.
_RECON_MAP_MAX_CHARS = 8000

#: The section header. Still "RECON MAP" though the content is a file footprint: the column, settle
#: argument and projection key keep that name.
_RECON_MAP_HEADER = "--- RECON MAP (PRIOR STAGE) ---"

#: Bounds the work of the drop loop below (which re-serialises per unit), distinct from
#: `_RECON_MAP_MAX_CHARS`'s output bound. Units past it are counted as dropped unrendered.
_RECON_MAP_MAX_UNITS = 2000


def _recon_footprint_files(inner: object) -> list | None:
    """The droppable file entries of a footprint-shaped inner map, or None when this map is not one.

    None is the legacy arm: a prose-shaped map from an older prerequisite passes through, not discarded."""
    if not isinstance(inner, dict):
        return None
    files = inner.get("files")
    if not isinstance(files, list) or not files:
        return None
    # Any footprint-looking entry makes this a footprint (not `all(...)`: one malformed element must not
    # demote the whole map). `recon_map` is free-shape runner JSONB. Requiring one recognisable entry keeps
    # a prose map with an unrelated `files` key in the legacy arm.
    if not any(isinstance(entry, dict) and isinstance(entry.get("path"), str) for entry in files):
        return None
    # Every element is a droppable unit, malformed ones included; an unserialisable one is priced and
    # dropped like any oversized unit.
    return files


def _recon_wrappers(recon_map: object) -> list[tuple[str, object]]:
    """`(persona_label, inner_map)` pairs, in prerequisite order.

    Input is a list from `routes/tasks/task_wire.py::_task_prerequisite_recon_map` (a dict would drop personas that
    repeat). A bare dict (a stage's own `Task.recon_map`) is accepted and renders unlabelled."""
    if isinstance(recon_map, list):
        pairs: list[tuple[str, object]] = []
        for item in recon_map:
            if isinstance(item, dict) and "recon_map" in item:
                persona = item.get("persona")
                pairs.append((str(persona) if persona else "", item.get("recon_map")))
            else:
                pairs.append(("", item))
        return [(persona, inner) for persona, inner in pairs if inner]
    if isinstance(recon_map, dict):
        return [("", recon_map)]
    return []


#: Substitution token for one file entry in `_recon_json`: indexed, with a per-render nonce because the
#: serialised JSONB is runner-authored and a stored value equal to a fixed token would corrupt the output.
_FOOTPRINT_ENTRY_TOKEN = "@@footprint-{nonce}-{index}@@"


def _recon_json(doc: object) -> str:
    """The document as JSON: pretty at the top level, each file entry on one compact line.

    Compacting roughly triples what fits in the budget, so a large file degrades to its outline instead of
    being dropped. Structure comes from `json.dumps`, so the result is always valid JSON."""
    compact: dict[str, str] = {}
    nonce = secrets.token_hex(8)

    def swap(body: object) -> object:
        files = body.get("files") if isinstance(body, dict) else None
        if not isinstance(files, list):
            return body
        tokens = []
        for entry in files:
            token = _FOOTPRINT_ENTRY_TOKEN.format(nonce=nonce, index=len(compact))
            compact[token] = json.dumps(entry)
            tokens.append(token)
        return {**{key: value for key, value in body.items() if key != "files"}, "files": tokens}

    if isinstance(doc, list):
        swapped: object = [
            {**item, "recon_map": swap(item["recon_map"])}
            if isinstance(item, dict) and "recon_map" in item else swap(item)
            for item in doc
        ]
    else:
        swapped = swap(doc)
    text = json.dumps(swapped, indent=2)
    for token, entry_json in compact.items():
        # `count=1`: only the one placeholder this render emitted; unbounded, a crafted stored value could
        # splice another entry's record into itself.
        text = text.replace(json.dumps(token), entry_json, 1)
    # Backstop: nonce + `count=1` make a collision implausible, not impossible, so fall back to a plain
    # dump (fatter, never invalid; `fits()` still measures it). `RecursionError` is not a `ValueError` and
    # both `json.dumps(indent=…)` and `json.loads` raise it on deep nesting; uncaught, a stored
    # `recon_map` would wedge every dependent dispatch.
    try:
        json.loads(text)
    except (ValueError, RecursionError):
        return json.dumps(doc, indent=2)
    return text


def _render_recon_map(recon_map: object) -> str:
    """The fenced RECON MAP section, or "" when there is nothing to render.

    The clamp drops whole units, never slices (a sliced map cannot be parsed or known incomplete):
      • a unit is one file entry, or one indivisible body (a legacy map, or a footprint's non-`files` keys);
      • entry-dropping spans all prerequisites (one budget);
      • units are kept in order only if the whole document still fits, so a large one does not starve
        smaller ones;
      • drop counts go in prose lines beneath the JSON (there is no top-level object to hold them);
      • the section always carries parseable JSON, even when empty.

    `_RECON_MAP_MAX_CHARS` bounds the JSON payload; header and drop lines sit outside it."""
    wrappers = _recon_wrappers(recon_map)
    if not wrappers:
        return ""
    as_list = isinstance(recon_map, list)
    # Per wrapper: whether it is a footprint, its droppable entries, the indivisible body kept so far,
    # and the entries kept so far. A footprint's body is its non-`files` keys; a legacy map IS its body.
    footprints: list[list | None] = [_recon_footprint_files(inner) for _persona, inner in wrappers]
    bodies: list[dict | object] = []
    for index, (_persona, inner) in enumerate(wrappers):
        if footprints[index] is None:
            bodies.append(inner)
        else:
            bodies.append({key: value for key, value in inner.items() if key != "files"})
    kept_body: list[object] = [None for _ in wrappers]
    kept_files: list[list] = [[] for _ in wrappers]

    def document() -> object:
        entries = []
        for index, (persona, _inner) in enumerate(wrappers):
            if footprints[index] is None:
                if kept_body[index] is None:
                    continue
                body: object = kept_body[index]
            else:
                base = dict(kept_body[index] or {})
                if not base and not kept_files[index]:
                    continue
                base["files"] = kept_files[index]
                body = base
            entries.append({"persona": persona, "recon_map": body} if persona else body)
        if not entries:
            return None
        return entries if as_list else entries[0]

    def rendered(doc: object) -> str:
        return defang_fence_markers(_recon_json(doc))

    def fits() -> bool:
        """Whether the document as currently kept renders inside the budget.

        An unrenderable unit (`RecursionError` on deep nesting, `TypeError` from a direct caller) does
        not fit, so it is dropped and counted rather than silently rendered as `{}` or wedging every
        dependent dispatch."""
        try:
            return len(rendered(document())) <= _RECON_MAP_MAX_CHARS
        except (ValueError, TypeError, RecursionError):
            return False

    dropped_entries = 0
    dropped_maps = 0
    units_seen = 0

    # Pass 1: indivisible bodies first, since a body either renders or is lost while entries degrade one
    # at a time. Dropping an oversized body here also bounds work: `document()` re-serialises every kept
    # body on each probe.
    for index, body in enumerate(bodies):
        if not body:
            continue
        units_seen += 1
        if units_seen > _RECON_MAP_MAX_UNITS:
            dropped_maps += 1
            continue
        kept_body[index] = body
        if not fits():
            kept_body[index] = None
            dropped_maps += 1

    # Pass 2: footprint entries, flattened across prerequisites; each kept only if the document still fits.
    for index, files in enumerate(footprints):
        if files is None:
            continue
        for entry in files:
            units_seen += 1
            if units_seen > _RECON_MAP_MAX_UNITS:
                dropped_entries += 1
                continue
            # An entry bigger than the whole budget can never fit; priced once here instead of probed.
            # This is also where an unserialisable unit is dropped and counted rather than raising.
            try:
                entry_size = len(json.dumps(entry))
            except (TypeError, ValueError, RecursionError):
                dropped_entries += 1
                continue
            if entry_size > _RECON_MAP_MAX_CHARS:
                dropped_entries += 1
                continue
            kept_files[index].append(entry)
            if not fits():
                kept_files[index].pop()
                dropped_entries += 1

    doc = document()
    if doc is None and not dropped_entries and not dropped_maps:
        return ""
    lines = [_RECON_MAP_HEADER]
    # An empty document rather than none: whatever renders under the header must parse.
    lines.append(rendered(doc if doc is not None else ([] if as_list else {})))
    if dropped_entries:
        noun = "entry" if dropped_entries == 1 else "entries"
        lines.append(f"— {dropped_entries} file {noun} dropped to fit")
    if dropped_maps:
        noun = "map" if dropped_maps == 1 else "maps"
        lines.append(
            f"— {dropped_maps} prerequisite {noun} dropped whole to fit"
        )
    return "\n\n" + "\n".join(lines)
