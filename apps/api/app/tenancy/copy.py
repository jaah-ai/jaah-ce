"""The words a tenant is never told: the one home for the tenant-copy vocabulary rule, and
its refusal-side sentences.

A tenant administrator reads the same catalogue prose, refusal sentences and placeholders the
operator does (`GET /api/settings`, `GET /api/credentials/types`, every 403 a tenant can earn), but
cannot see a fleet, deployment, hub or control plane, so copy naming one describes a page they are
not on. The censuses that enforce the rule import one list from here.

"operator" is deliberately not on the list: a refusal must name who can act. Matching is
word-bounded so "hub" does not fire inside "GitHub".
"""

from __future__ import annotations

import re
from typing import Iterable

#: The operator's own vocabulary, never shown to a tenant — lower-case, matched on word boundaries.
OPERATOR_CONSOLE_VOCABULARY: tuple[str, ...] = (
    "fleet",
    "deployment",
    "control plane",
    "superadmin",
    "hub",
    "system > settings",
)


def operator_vocabulary_in(
    text: str | None, *, words: Iterable[str] = OPERATOR_CONSOLE_VOCABULARY
) -> list[str]:
    """The words of `words` that `text` carries, matched case-insensitively on word boundaries, in list
    order so an assertion message names them stably. `None` carries nothing.
    """
    lowered = (text or "").lower()
    return [word for word in words if re.search(rf"\b{re.escape(word)}\b", lowered)]


def not_available(subject: str) -> str:
    """The one sentence a tenant reads for a fleet-unique value another org holds; never "already exists".

    Columns like `accounts.slug`, `admin_users.email` are unique fleet-wide while their rows are
    org-scoped, so "already exists" would be a cross-org existence oracle. The operator keeps "already
    exists" on their own doors. `subject` is the caller's noun phrase for what collided. Project names are
    unique per org and node and host names not at all, so none collides across orgs or reaches this."""
    return f"{subject} is not available"


def administrator_changes(subject: str) -> str:
    """The one sentence a tenant reads at a superadmin door: who can act, never the tier's name.

    `subject` is the plural noun for what the door writes ("Account records", "Worktree slots").
    """
    return f"{subject} are changed by an administrator of your organization — ask them for the change you need."


def unavailable_now(subject: str | None = None) -> str:
    """The one sentence a tenant reads for a control the deployment cannot serve their organization: who
    can act, never what is missing.

    The operator's refusal names the missing piece (e.g. "no DeepSeek API key"), which describes the
    deployment. `subject` is the control's name as the page shows it ("Re-tag"); `None` keeps the
    Efficiency tab's subject-less form.
    """
    if subject is None:
        return "Not available for your organization right now. Ask an operator."
    return f"{subject} is not available for your organization right now. Ask an operator."


def operator_hosted(subject: str, alternative: str) -> str:
    """The one sentence for a control that runs on the operator's own machines: what the tenant may do
    instead, never what those machines are.

    Used by credential Test and OAuth start, which are refused on tenant orgs, so only tenants read it.
    Unlike `unavailable_now` (fixable by an operator, so it ends "Ask an operator"), this is a plane
    boundary, so `alternative` (one punctuated sentence) is required.

    `subject` is the control's name as the page shows it; name a heading the reader can find, never a
    coined noun. Both halves must be server-owned constants (a literal or a registry label), never
    request values: the sentence is served verbatim as a 403 body.
    """
    return f"{subject} is not available for your organization. {alternative}"


def governance_authority(named: Iterable[str]) -> str:
    """The one sentence a tenant reads at a project's governance door, in a tenant's words.

    `routes/projects.py::_require_authority_to_author_governance` serves operators and tenants; tenants
    get the two doors their organization actually has. `named` is the governance fields the payload
    carried, in the guard's order.
    """
    fields = ", ".join(named)
    return (
        "An administrator of your organization, holding nodes:edit org-wide and write reach "
        f"on a node in your organization, is required to change {fields}: the gate and stack commands "
        "run as a shell on every node that builds this project, and the branch/push fields decide what "
        "a runner may push — so they are not editable with the projects:edit permission alone. Ask "
        "them, or send only the non-governance fields."
    )


def node_home_unrecorded(node_id: str, consequence: str) -> str:
    """The one sentence a tenant reads when a node's home directory is not recorded: what did not happen
    and who can act, never the host field's name.

    A tenant administrator records it on their own node (`routes/nodes/update.py::update_node`); other tenant
    readers are not served `jaah_home`, and the operator keeps the sentences naming it. The fork
    discloses one bit (whether the tenant's own node has a recorded home, never the value), because
    without it the refusal is not actionable.

    `node_id` is the node as the caller named it; `consequence` is the door's clause for what did not
    happen.
    """
    return (
        f"node {node_id!r} has no recorded home directory, so {consequence} — "
        "an administrator of your organization can record it on the node's page"
    )


#: The label a tenant sees for an actor the read seam hides: a name, not a blank.
#:
#: `actor_names` joins `admin_users` under the caller's org, so a row an operator wrote into a tenant
#: resolves to nothing there; "not known" would be false. Only an operator can write into another org,
#: so Jaah's own staff is true and discloses no individual; it is the name the tenant's copy already uses for
#: them ("Contact Jaah support to change"). Capitalised because it renders beside names like "System".
WITHHELD_ACTOR = "Jaah support"
