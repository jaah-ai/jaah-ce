"""What hangs off a task: comments, mentions, reactions, tags, attachments, PRs and compliance manifests."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    desc,
    text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.pr_changes import PR_CHANGES_STATUSES, PR_CHANGES_TABLE
from app.pr_stats import PR_STATS_STATUSES, PR_STATS_TABLE
# Leaf import (stdlib-only, guarded by a shape test): deriving
# `ATTACHMENT_KINDS` from it below cannot cycle at import time.
from app.task_manifest import DOCUMENT_KINDS
from app.models.base import Base, EntityMixin, org_unique, OrgScoped, timestamp_column, UuidPkMixin


VISIBILITY_PUBLIC = "public"


VISIBILITY_INTERNAL = "internal"


# The emoji a comment reaction may carry, in palette order — the CHECK on `task_comment_reactions` and
# the one server-side list (`app/comment_reactions.py` adds each one's Slack spelling; the web's glyphs
# are `apps/web/src/lib/reactions.ts`, held to these keys by `test_comment_reactions_palette.py`).
REACTION_EMOJIS: tuple[str, ...] = (
    "thumbs_up", "heart", "laugh", "tada", "surprised", "sad", "eyes", "rocket",
)


def subject_checks(
    table: str, *, visibility: bool, subjects: tuple[str, ...] = ("task_id", "ticket_id")
) -> list[CheckConstraint]:
    """The CHECKs of a row that belongs to a Task OR a Ticket (`task_comments`, `task_comment_mentions`,
    `task_attachments`; revision 0302) — or, for `task_attachments` alone, a support conversation (revision
    0308): exactly one of `subjects`, and — where the row has a `visibility` — only a ticket's row may be
    `internal`, so a task comment is never hidden from its readers."""
    checks = [
        CheckConstraint(f"num_nonnulls({', '.join(subjects)}) = 1", name=f"ck_{table}_one_subject")
    ]
    if visibility:
        checks += [
            CheckConstraint(
                f"visibility IN ('{VISIBILITY_PUBLIC}', '{VISIBILITY_INTERNAL}')", name=f"ck_{table}_visibility"
            ),
            CheckConstraint(
                f"visibility = '{VISIBILITY_PUBLIC}' OR ticket_id IS NOT NULL",
                name=f"ck_{table}_internal_is_ticket",
            ),
        ]
    return checks


# A task's own status is `Task.state` (`app/task_lifecycle.py`).

# `task_attachments.kind` is direction (app-layer vocabulary, no CHECK): `attachment` is the human's
# dispatch input, materialized into the runner's workspace; `comment_image` is a human's image pasted
# into a comment, owned by that comment (`TaskAttachment.comment_id`) — never materialized, never an
# artifact; every other kind is a runner-authored deliverable (`report` HTML, `prd`/`architecture`
# markdown, free-form `file`). One table: all are task-owned bytes with one cascade, size cap and content
# routes (0115). Only input is materialized (`app/routes/tasks/task_wire.py::_task_attachments_meta`). Document
# kinds are derived from `app/task_manifest.py::DOCUMENT_KINDS`, never re-spelled.
ATTACHMENT_KIND_FILE = "attachment"


ATTACHMENT_KIND_COMMENT_IMAGE = "comment_image"


ATTACHMENT_KIND_REPORT = "report"


ATTACHMENT_KINDS: tuple[str, ...] = (
    (ATTACHMENT_KIND_FILE, ATTACHMENT_KIND_COMMENT_IMAGE) + DOCUMENT_KINDS
)


class TaskComment(OrgScoped, Base):
    """One person's comment on one Task or one Ticket: a task's Comments tab, a ticket's thread.

    Posting needs only `tasks:view` (`app/routes/task_comments.py` also owns the narrower edit/delete
    rules). `author_id` instead of the entity actor pair: a comment has one writer for life (others can
    only delete it).

    `body` is the raw typed text, empty only on a comment carrying images (`TaskAttachment.comment_id`,
    rule in `app/comment_service.py::clean_body`); the micro-format (`*bold*`, backticks, URLs, `@First Last`) is parsed
    on render (`apps/web/src/lib/commentText.ts`; `app/comment_mentions.py` server-side). `edited_at`
    NULL means never revised, and it is the only revision stamp (no `updated_at` that mechanical writes
    could move); see revision 0259."""

    __tablename__ = "task_comments"
    __table_args__ = (
        *subject_checks(__tablename__, visibility=True),
        Index("ix_task_comments_ticket_id", "ticket_id", "id"),
        Index("ix_task_comments_task_id", "task_id", "id"),
    )

    # BIGINT: the highest-volume child a task grows, and widening a PK later is costly.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Exactly one of the two subjects is set (`subject_checks`; revision 0302).
    task_id: Mapped[Optional[int]] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True)
    ticket_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("tickets.id", ondelete="CASCADE", name="fk_task_comments_ticket_id"),
        nullable=True,
    )
    # `internal` is an operator's note on a ticket, never returned to anyone else (`app/support_access.py`).
    visibility: Mapped[str] = mapped_column(Text, nullable=False, server_default=VISIBILITY_PUBLIC)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # RESTRICT, per the actor-column standard. An author with comments cannot be deleted
    # (`routes/users.py::delete_user` answers 409 naming the table); a per-author purge is the follow-up.
    author_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = timestamp_column()
    edited_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


class TaskCommentMention(OrgScoped, Base):
    """One person named in one comment (a Task's or a Ticket's) — the row that makes a mention a fact
    rather than a spelling.

    Reconciled from the text by `app/comment_mentions.py` on every post/edit (resolving `@First Last`
    against the org roster); `read_at` survives for names still present. Needed for the notification leg
    (the person at write time) and the mentions inbox (per-reader state); `read_at` NULL is unread. A pure
    edge, so no entity shape."""

    __tablename__ = "task_comment_mentions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    comment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("task_comments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The derivation: org is inherited from the task OR the ticket, one hop (`app/tenancy/manifest.py`;
    # `task_comments` is itself derived). `comment_id` is a second tenant-owned reference in
    # the tenancy composite-key test's unkeyed-reference register. The mentions inbox links by these.
    task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    ticket_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("tickets.id", ondelete="CASCADE", name="fk_task_comment_mentions_ticket_id"),
        nullable=True,
        index=True,
    )
    # CASCADE: a mention of a deleted person can never be read.
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = timestamp_column()
    read_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)

    __table_args__ = (
        UniqueConstraint("comment_id", "user_id", name="uq_task_comment_mentions_comment_user"),
        *subject_checks("task_comment_mentions", visibility=False),
        # The mentions inbox's unread read; `user_id` has no index of its own.
        Index("ix_task_comment_mentions_unread", "user_id", "id", postgresql_where=text("read_at IS NULL")),
    )


class TaskCommentReaction(OrgScoped, Base):
    """One person's emoji on one comment (a Task's or a Ticket's) — 👍 is the "like".

    `emoji` is a key out of the fixed set `REACTION_EMOJIS` (the CHECK is that set);
    one row per (comment, person, emoji). The row is also the comment author's bell entry: `read_at` NULL
    is unread for the AUTHOR, not for `user_id` (who reacted). Removing a reaction stamps `removed_at` rather
    than deleting, so taking one back and re-adding it restores the row instead of owing the author another
    notice (a toggle would otherwise DM without limit). Subject columns and tenancy exactly as
    `TaskCommentMention`'s. A pure edge, so no entity shape; revision 0304."""

    __tablename__ = "task_comment_reactions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    comment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("task_comments.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    ticket_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("tickets.id", ondelete="CASCADE", name="fk_task_comment_reactions_ticket_id"),
        nullable=True,
        index=True,
    )
    # CASCADE: a deleted person's reactions go with them, as their mentions do.
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("admin_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    emoji: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = timestamp_column()
    read_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # NULL while the reaction stands; every read of the live set filters on it.
    removed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)

    __table_args__ = (
        UniqueConstraint("comment_id", "user_id", "emoji", name="uq_task_comment_reactions_comment_user_emoji"),
        CheckConstraint(
            "emoji IN (" + ", ".join(f"'{key}'" for key in REACTION_EMOJIS) + ")",
            name="ck_task_comment_reactions_emoji",
        ),
        *subject_checks("task_comment_reactions", visibility=False),
    )


class TagGroup(EntityMixin, OrgScoped, Base):
    """A named set of related tags an operator can put on a Task ("By type", "By function").

    `name` is unique per org, `(org_id, name)`: it is the operator's only handle on the set, and a
    table-wide unique would let one tenant's names block another's and leak existence via 409. No `slug`:
    nothing materializes outside Postgres (the board's group choice is in sessionStorage).

    `position` makes ordering the operator's: the API assigns `max(position) + 1`, since a row left at
    the raw-SQL default 0 would sort above seeded rows.
    """

    __tablename__ = "tag_groups"
    __table_args__ = (
        org_unique(__tablename__),
        CheckConstraint("state IN ('draft', 'active')", name="ck_tag_groups_state"),
        UniqueConstraint("org_id", "name", name="uq_tag_groups_org_name"),
    )

    # Lifecycle, not authorization: a `draft` group is not yet offered as a tag source. Pinned in the
    # schema: two values with no transitions are cheaper to enforce here.
    state: Mapped[str] = mapped_column(
        Text, nullable=False, default="draft", server_default="draft"
    )
    # Whether a Task may carry several of this group's tags ("By function") or one ("By type"); enforced
    # by the assignment API.
    multi_select: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class Tag(EntityMixin, OrgScoped, Base):
    """One tag inside a group: `tag` is the code the operator types ("feature"), `name` the chip label
    ("Features").

    `tag` is unique within a group only ("other" fits several groups). No `slug`, and `tag` is an input
    key, never a placement handle.
    """

    __tablename__ = "tags"
    __table_args__ = (
        UniqueConstraint("tag_group_id", "tag", name="uq_tags_group_tag"),
        Index("ix_tags_tag_group_id", "tag_group_id"),
    )

    tag_group_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tag_groups.id", ondelete="CASCADE"), nullable=False
    )
    tag: Mapped[str] = mapped_column(Text, nullable=False)
    # The operator's order within the group, as `TagGroup.position`.
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class TaskTag(OrgScoped, Base):
    """Assignment edge: this Task carries this Tag. A pure join row, so no entity shape
    (`tests/test_entity_shape.py`). Both FKs cascade; `tag_id` is indexed for the reverse lookup (the
    board's per-tag counts and cascade deletes), as `TaskDependency.depends_on_task_id`."""

    __tablename__ = "task_tags"

    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )


class TaskAttachment(OrgScoped, Base):
    """Uploaded file, image, or runner-authored report attached to a Task — or a file on a support Ticket:
    the metadata row and a pointer to its bytes.

    `kind` discriminates (open vocabulary at `ATTACHMENT_KINDS`); list/render readers filter on it
    (`app/routes/attachments.py::artifact_content`). `server_default` is the database default the
    rolling-deploy overlap needs (0116).

    S3 is the only system of record for the bytes (the shared session-transcript bucket, `s3_key`); this
    table holds none. Why that bucket, presigning, and the one non-redirecting read:
    `app/attachment_storage.py`. Never a filesystem (deploys erase container disk); disk is only a
    runner-side cache. jaahd reads bytes through the agent-token content route, never the DB.

    `task_id` cascades rows only: the CP has no `s3:ListBucket`, so orphaned objects are unreclaimable,
    and delete paths must remove bytes explicitly while keys are readable
    (`app/routes/tasks/delete.py::delete_task_core`, `app/routes/attachments.py::delete_attachment`). A key may
    be shared by several rows (see `s3_key`). `uploaded_by_id` is `SET NULL`. An epic's final report, and a
    filer's artifact attached to the task it filed, are copies of another row (`source_task_id`).

    `comment_id` is set only on a `comment_image` row once its comment is posted (NULL while staged);
    CASCADE takes the rows with the comment, so the bytes are reaped by the comment routes
    (`app/routes/task_comments.py`, via `app/comment_service.py::image_keys`) like every other delete path."""

    __tablename__ = "task_attachments"
    __table_args__ = (
        *subject_checks(
            __tablename__,
            visibility=True,
            subjects=("task_id", "ticket_id", "conversation_id", "automation_id", "org_template_id"),
        ),
        # The per-org byte quota's sum (`app/org_quotas.py`), run on every upload (revision 0355).
        Index("ix_task_attachments_org_id", "org_id"),
        # A task's attachments of one kind, newest first.
        Index("ix_task_attachments_task_kind_id", "task_id", "kind", desc("id")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Exactly one of the three subjects is set (`subject_checks`; revisions 0302, 0308). A ticket's key is
    # `task-attachments/tickets/<ticket_id>/<id>`, a support chat's `task-attachments/support-chats/
    # <conversation_id>/<id>` (`app/attachment_storage.py::object_key`).
    task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    ticket_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("tickets.id", ondelete="CASCADE", name="fk_task_attachments_ticket_id"),
        nullable=True,
        index=True,
    )
    # A file the user pasted into a support chat, before (and after) the agent files the ticket — which
    # copies each confirmed one onto the ticket as a row of its own sharing this `s3_key`.
    conversation_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("support_conversations.id", ondelete="CASCADE", name="fk_task_attachments_conversation_id"),
        nullable=True,
        index=True,
    )
    # A file stored on an automation's Task action, copied (sharing this `s3_key`) onto every task it fires
    # (revision 0417). Its key is `task-attachments/automations/<id>/<row id>`.
    automation_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("automations.id", ondelete="CASCADE", name="fk_task_attachments_automation_id"),
        nullable=True,
        index=True,
    )
    # An image of an org's own task template (revision 202610281500), copied (sharing this `s3_key`) onto every task
    # started from it. Its key is `task-attachments/org-templates/<id>/<row id>`.
    org_template_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("org_task_templates.id", ondelete="CASCADE", name="fk_task_attachments_org_template_id"),
        nullable=True,
        index=True,
    )
    # `internal`: uploaded from an operator's internal note on a ticket; served to operators only.
    visibility: Mapped[str] = mapped_column(Text, nullable=False, server_default=VISIBILITY_PUBLIC)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=ATTACHMENT_KIND_FILE)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)  # up to `MAX_FILE_BYTES` (5 GiB): past INTEGER
    # The object holding this row's bytes, `task-attachments/<task_id>/<id>`, derived server-side
    # (`app/attachment_storage.py::object_key`). NULL means the bytes are nowhere: a minted ticket never
    # confirmed (`uploaded_at` tells it apart). Not unique: an epic's final report copy shares its
    # child's key (objects are immutable), so an object may be deleted only once no row references it.
    s3_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True, index=True)
    # The bucket `s3_key` was written to (revision 202610191600): the org's data region's
    # (`app/data_residency.py`). NULL = the configured transcript bucket (every row written before it). Copied with
    # `s3_key`; every read and delete targets it (`attachment_storage.target(bucket=...)`).
    s3_bucket: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # When the bytes were confirmed present: "readable" is `uploaded_at IS NOT NULL`. Between ticket and
    # confirmation (the client PUTs straight to S3) the row is listed and served by nothing.
    uploaded_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # The object's ETag when its bytes were confirmed (`attachment_storage.Landed`), copied with `s3_key`. It names
    # the BYTES: a same-length overwrite through a still-valid upload URL changes it, which is how Live Cast tells
    # confirmed bytes from a swap (`app/live_cast/drift.py`). NULL: confirmed before revision 202610131200, or not
    # confirmed yet.
    etag: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    uploaded_by_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    # The task a row was COPIED from, two producers: a report the CP copied onto an epic from a child
    # (`kind=report`; provenance, not the idempotency key — SET NULL could blank it, see `app/routes/
    # tasks/epic_finish.py::_publish_epic_final_report` — and every control removing such a copy purges before the FK can
    # blank it), and an artifact a filer attached to the task it filed (`kind=attachment`,
    # `app/routes/tasks/agent_filing.py::_attach_caller_artifacts`), which outlives its filer with the origin blanked.
    source_task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # The comment a `comment_image` belongs to (revision 0305); see the class docstring.
    comment_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("task_comments.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = timestamp_column()


class TaskPrStats(OrgScoped, Base):
    """A merged PR's diff stats, recorded at its task's `merged_pr` settle (R9) — one row per task, upserted.

    `app/pr_stats.py` owns the vocabulary and the write; this class says what columns mean. `additions`/`deletions`
    are after the node's generated-file exclusion (`excluded_*` hold what it excluded); `files_changed` counts ALL
    files. Counts are NULL when not captured. `reason` explains any status but `captured`. No row means the
    settle carried no stats, read as "not captured". Machine-written, so no entity shape."""

    __tablename__ = PR_STATS_TABLE
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{status}'" for status in PR_STATS_STATUSES) + ")",
            name="ck_task_pr_stats_status",
        ),
    )

    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True)
    pr_url: Mapped[str] = mapped_column(Text, nullable=False)
    forge: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    additions: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    deletions: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    files_changed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    excluded_additions: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    excluded_deletions: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    captured_at: Mapped[datetime] = timestamp_column()


class TaskPrChanges(OrgScoped, Base):
    """A merged PR's provenance — merge commit, commits, changed paths — recorded at its task's `merged_pr` settle,
    one row per `(task, PR)`, upserted. The daemon reads it off the forge, so a model cannot write it.

    `app/pr_changes.py` owns the vocabulary and the write; this class says what columns mean. `commits` is a JSON
    array of `{sha, author_name, author_login, message}` and `files` one of paths, both empty when not captured.
    `pr_number` is 0 only when no PR number could be named. `reason` explains any status but `captured`. No row
    means the settle carried no provenance (a settle before this shipped, too). Machine-written, so no entity
    shape."""

    __tablename__ = PR_CHANGES_TABLE
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{status}'" for status in PR_CHANGES_STATUSES) + ")",
            name="ck_task_pr_changes_status",
        ),
    )

    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True)
    pr_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    pr_url: Mapped[str] = mapped_column(Text, nullable=False)
    forge: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    merge_sha: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    commits: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), nullable=False)
    files: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), nullable=False)
    captured_at: Mapped[datetime] = timestamp_column()


#: `task_prs.state` (`ck_task_prs_state`, revision 202610152100).
TASK_PR_STATES: tuple[str, ...] = ("open", "merged", "closed")


class TaskPr(OrgScoped, Base):
    """One pull request a task opened, in any of its project's repositories.

    `app/task_prs.py` owns every rule and both writers: the node daemon's `pull_request.observed` (after `jaah-forge pr
    create`/`pr view`, the repository from the caller's checkout) and the `merged_pr` settle, which inserts the PRs
    it attests and refuses while any recorded PR is left unmerged. `project_repo_id` is NULL once its repository is
    disconnected; `repo_path` (`owner/name`) keeps the label. `is_primary`: the PR is in the project's primary
    repository. Machine-written, so no entity shape."""

    __tablename__ = "task_prs"
    __table_args__ = (
        CheckConstraint(
            "state IN (" + ", ".join(f"'{state}'" for state in TASK_PR_STATES) + ")", name="ck_task_prs_state",
        ),
        UniqueConstraint("task_id", "pr_url", name="uq_task_prs_task_url"),
        Index("ix_task_prs_project_repo_id", "project_repo_id", postgresql_where=text("project_repo_id IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE", name="fk_task_prs_task_id"), nullable=False
    )
    project_repo_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("project_repos.id", ondelete="SET NULL", name="fk_task_prs_project_repo_id"),
        nullable=True,
    )
    repo_path: Mapped[str] = mapped_column(Text, nullable=False)
    forge: Mapped[str] = mapped_column(Text, nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    pr_url: Mapped[str] = mapped_column(Text, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    opened_at: Mapped[datetime] = timestamp_column()
    merged_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)


#: `compliance_manifests.state` / `.trigger` (`ck_compliance_manifests_state` / `_trigger`, revision 202610120500).
COMPLIANCE_MANIFEST_STATES: tuple[str, ...] = ("sealed", "failed")


COMPLIANCE_MANIFEST_TRIGGERS: tuple[str, ...] = ("auto", "regenerate")


class ComplianceManifest(UuidPkMixin, OrgScoped, Base):
    """One seal attempt of one task's signed compliance manifest (`app/compliance_manifest.py` builds the payload).

    `version` counts up per task from `tasks.compliance_seal_seq`. A `sealed` row names the payload digest, the
    signing key and the two stored objects (`json_key`, `html_key`); a `failed` row only `failure_reason`.
    `session_digest_snapshot` is `{session_id: transcript_status}` at seal time, so a later upload can be told
    apart from what was sealed. Machine-written (the seal pipeline), so no entity shape."""

    __tablename__ = "compliance_manifests"
    __table_args__ = (
        UniqueConstraint("task_id", "version", name="uq_compliance_manifests_task_version"),
        CheckConstraint(
            "state IN (" + ", ".join(f"'{v}'" for v in COMPLIANCE_MANIFEST_STATES) + ")",
            name="ck_compliance_manifests_state",
        ),
        CheckConstraint(
            "trigger IN (" + ", ".join(f"'{v}'" for v in COMPLIANCE_MANIFEST_TRIGGERS) + ")",
            name="ck_compliance_manifests_trigger",
        ),
        CheckConstraint(
            "(state = 'sealed' AND payload_sha256 IS NOT NULL AND failure_reason IS NULL)"
            " OR (state = 'failed' AND payload_sha256 IS NULL AND failure_reason IS NOT NULL)",
            name="ck_compliance_manifests_outcome",
        ),
        CheckConstraint("version >= 1", name="ck_compliance_manifests_version"),
    )

    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    generated_by_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    generated_at: Mapped[datetime] = timestamp_column()
    payload_sha256: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    kid: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    json_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    html_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    session_digest_snapshot: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
