"""ORM models, one module per domain. Importing the package registers every mapper on `base.Base`;
nothing is re-exported — import each name from the module that defines it.
"""
from app import edition_features
import app.models.base  # registers mappers
import app.models.orgs  # registers mappers
import app.models.identity  # registers mappers
import app.models.access  # registers mappers
import app.models.audit  # registers mappers
import app.models.worktrees  # registers mappers
import app.models.agent_sessions  # registers mappers
import app.models.projects  # registers mappers
import app.models.tasks  # registers mappers
import app.models.task_extras  # registers mappers
import app.models.nodes  # registers mappers
import app.models.node_groups  # registers mappers
import app.models.placement_groups  # registers mappers
import app.models.nodelinks  # registers mappers
import app.models.billing  # registers mappers
import app.models.merge_lane  # registers mappers
import app.models.production_qa  # registers mappers
import app.models.releases  # registers mappers
import app.models.outcomes  # registers mappers
import app.models.routing  # registers mappers
import app.models.review  # registers mappers
import app.models.credentials  # registers mappers
import app.models.automations  # registers mappers
import app.models.notifications  # registers mappers
import app.models.hosting  # registers mappers
import app.models.products  # registers mappers
import app.models.control_plane  # registers mappers
import app.models.metering  # registers mappers
import app.models.operator_dashboard  # registers mappers
import app.models.support  # registers mappers
import app.models.governance  # registers mappers
import app.models.api_keys  # registers mappers
import app.models.tenant_email  # registers mappers
import app.models.task_templates  # registers mappers
import app.models.integrations  # registers mappers
import app.models.hosted_apps  # registers mappers
import app.models.jaah_repos  # registers mappers
import app.models.trackers  # registers mappers
import app.models.webhooks  # registers mappers
# The feature model modules import `Base` from `app.models.base`, so they register after the domain modules.
# The community edition has no project memory: its tables are not mapped there.
if edition_features.enabled("project_memory"):
    import app.project_memory.models  # registers mappers
import app.live_cast.models  # noqa: F401 — registers mappers
# The `pipeline.changed` live event's flush and commit listeners (`app/pipeline/events.py`); imports no model.
import app.pipeline  # noqa: F401,E402
