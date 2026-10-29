"""The deliverable vocabularies — what a runner's output may be, and how a settle call may end.

- artifact kinds: `ARTIFACT_KINDS` and its partitions (`DOCUMENT_KINDS` = `KIND_MEDIA` +
  `FREEFORM_KINDS`, `DERIVED_KINDS`, `REFERENCE_KINDS`), plus `ARTIFACT_KIND_LABELS`;
- manifest levels `REQUIRED_HARD` / `REQUIRED_ADVISORY`;
- terminal types (`TERMINAL_TYPES`) and `OUTCOME_SUMMARY_TARGET_CHARS`;
- `INPUT_MANIFEST`, a property of the dependency edge rather than a persona.

Pure data, stdlib only: `app/models/` derives `ATTACHMENT_KINDS` from here (an `app.*` import would
cycle), and a tools test loads it off-path to compare against the mirror in
`apps/jaahd/agent_vocabulary.py`. `app/persona_registry.py` imports from here, never the reverse; the
persona tables (`PERSONA_TERMINALS`, `PERSONA_MANIFEST`) are derived there.
"""
from __future__ import annotations

# ── Artifact kinds ──────────────────────────────────────────────────────────────────────────────────
# The complete deliverable vocabulary. The three subsets below partition it exactly (asserted).
ARTIFACT_KINDS: tuple[str, ...] = (
    "report",
    "prd",
    "architecture",
    # Grouped with the other row kinds for reading; compared as a set everywhere. `file` is a deliverable;
    # `app/models/`'s `ATTACHMENT_KIND_FILE` (`"attachment"`) is the human's dispatch input — opposite
    # directions through one table.
    "file",
    "coders",
    "merged_pr",
    "no_pr",
    "finish_report",
)

#: The kinds that become rows; `app/models/` derives `ATTACHMENT_KINDS` from it. Partitioned into
#: `KIND_MEDIA` and `FREEFORM_KINDS` (asserted). `file` is owed by no manifest and has no `INPUT_MANIFEST`
#: row (`materialized_input_name` needs a `KIND_MEDIA` extension). Append only: the jaahd mirror is
#: compared order-sensitively by a parity test.
DOCUMENT_KINDS: tuple[str, ...] = ("report", "prd", "architecture", "file")

#: CP-computed; the runner attests nothing, so neither is a manifest row. Kept in `ARTIFACT_KINDS`
#: because refusal text and the charter name them.
DERIVED_KINDS: tuple[str, ...] = ("coders", "finish_report")

#: A pointer the settle call carries (a merged PR url, or a no-PR reason plus evidence). Owed via the
#: terminal type, never a manifest row.
REFERENCE_KINDS: tuple[str, ...] = ("merged_pr", "no_pr")

#: The format-pinned document kinds: (accepted extensions, forced stored content_type). Forced
#: (`agent_upload_artifact`) because a runner-chosen type decides how a browser reads the bytes.
#: `task_settle._document_satisfied` and `dispatch_compose.input_staging.materialized_input_name` index it directly,
#: safe only while no manifest or input row names a free-form kind (asserted in the shape test).
KIND_MEDIA: dict[str, tuple[tuple[str, ...], str]] = {
    "report": ((".html", ".htm"), "text/html; charset=utf-8"),
    "prd": ((".md", ".markdown"), "text/markdown; charset=utf-8"),
    "architecture": ((".md", ".markdown"), "text/markdown; charset=utf-8"),
}

#: The document kinds with no pinned format: any extension, but a non-empty filename. The stored type is
#: derived from the suffix via our own table, never the upload (`attachments.py::_derived_content_type`).
#: Served as a download unless on the preview allowlist in `app/routes/attachments.py`. For inline
#: previews the response CSP (`sandbox` + `default-src 'none'`) is the sole guard between agent-authored
#: `.html`/`.svg` and the console session cookie: never weaken it or make it kind-conditional.
FREEFORM_KINDS: tuple[str, ...] = ("file",)

#: Labels for all eight kinds (refusal text and the charter use them). Key set is exactly
#: `ARTIFACT_KINDS`, asserted both ways.
ARTIFACT_KIND_LABELS: dict[str, str] = {
    "report": "Report",
    "prd": "Requirements (PRD)",
    "architecture": "Architecture",
    # "File", not "Attachment" — that word already means the human's dispatch input.
    "file": "File",
    "coders": "Coders",
    "merged_pr": "Merged PR",
    "no_pr": "No-PR terminal",
    "finish_report": "Finish report",
}

# ── The outcome-summary length target ───────────────────────────────────────────────────────────────
#: How long an outcome summary should be, in characters. A target, not a cap: stated in the settle tool
#: schema, `SettleIn` and `dispatch_compose.settle_terms._settle_tool_for`, enforced nowhere (refusing a runner's last call
#: strands finished work). The hard cap is `routes/tasks/policy.py::MAX_TASK_TEXT_LEN`. Mirrored in
#: `apps/jaahd/agent_vocabulary.py`; pinned by a parity test.
OUTCOME_SUMMARY_TARGET_CHARS = 256

# ── Terminal types ──────────────────────────────────────────────────────────────────────────────────
#: The named endings a settle call may declare. Mirrored in `apps/jaahd/agent_vocabulary.py` (rendered
#: into `task.complete`'s input schema); pinned by a parity test.
TERMINAL_TYPES: tuple[str, ...] = (
    "analysis",
    "coders",
    "merged_pr",
    "no_pr",
    "finish",
    "outcome",
)

# ── The input manifest ──────────────────────────────────────────────────────────────────────────────
#: What a dispatched persona is handed: prerequisite artifacts materialised as files, named by path (D5).
#: Each value must be a hard-required kind of some `persona_registry.PERSONA_MANIFEST` (shape test).
#: Analysis stages only (D12): `coder` already gets a distilled spec. Absent personas get no artifact
#: paths (`routes/tasks/task_wire.py::_task_prerequisite_artifacts`); summaries still thread through the prompt.
INPUT_MANIFEST: dict[str, tuple[str, ...]] = {
    "architecture": ("prd",),
    "plan": ("prd", "architecture"),
}

# ── The manifest levels ─────────────────────────────────────────────────────────────────────────────
#: HARD refuses the settle without the kind; ADVISORY records the absence and proceeds. Strings, not a
#: bool, because refusal text and the charter name the level. Rows live in
#: `app/persona_registry.py::PERSONA_MANIFEST`.
REQUIRED_HARD = "hard"
REQUIRED_ADVISORY = "advisory"
