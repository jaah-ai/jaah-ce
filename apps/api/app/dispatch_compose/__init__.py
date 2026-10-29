"""Per-persona pipeline-dispatch composer; the only copy.

Owns dispatch decision logic: the persona→(charter, model) lookup (derived from `app/persona_registry.py`),
system- and task-prompt assembly (with the untrusted fence), model precedence and the tri-state verdict. It
reads doctrine files off disk, so the CP's version scope names them and a charter edit versions the CP.

Attachment materialization stays on the node; only `input_staging._attachment_workdir`'s pure path calculation
lives here, so both sides compute the same path.

Module map (import each name from its owning module; this file exports nothing):

- `compose.py` — one dispatch from the DB: model and node resolution, `compose()` and its tri-state verdict.
- `system_prompt.py` — `build_system_prompt*`: the charter, the role section, provider rendering, trusted add-ons.
- `task_prompt.py` — `build_task_prompt`: the trusted Start-here, resume and repos blocks around the fence.
- `directives.py` — the autonomy directives: interview, Full Auto, the security-surface gate.
- `settle_terms.py` — `SettleTerms` and the settle section each persona renders.
- `doctrine.py` — the edition's doctrine bundle (`doctrine_types.DoctrineBundle`): every role row, the prose they
  share, the rewrite tables and the autonomy directives; `role_tables.py` — the behaviour tables and helpers both
  bundles share; `roles.py` — the `_ROLES` table (the bundle's rows) and its readers; `role_render.py` — the "Your Role"
  section.
- `placeholders.py` — project-value placeholders, conditional blocks, name respelling.
- `fencing.py` — defanging caller text: the untrusted-data fence and conditional-marker openers.
- `recon_map.py`, `project_memory.py` — the two fenced sections a prerequisite stage and project memory add.
- `input_staging.py` — where attachments and prerequisite input artifacts are staged on the node.
"""
