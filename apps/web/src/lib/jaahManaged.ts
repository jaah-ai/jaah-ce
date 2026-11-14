import type { Admin } from '../auth'
import type { Project } from '../types'

/* A JAAH-MANAGED PROJECT (`Project.jaah_managed`, `apps/api/app/jaah_repos.py::managed_ids`): on a repository in
 * Jaah's GitHub org, which Jaah runs — and hosts, for a web app. An org member sees only its name (renamed in the trail), Default agent and
 * persona, Agent instructions, Access (while the org may hold more than one user) and Budget, plus Transfer repository
 * to put it on a repository of their own; its repositories by name only ("at Jaah"), never their branches or other
 * settings. The operator sees
 * everything. The API refuses the rest on its own (`jaah_repos.refuse_member_edit`,
 * `routes/projects.py::_refuse_managed_fields`, whose `_MEMBER_MANAGED_FIELDS` this list follows, less `name`). */

/** The project-row fields a member's Save still sends (the name is the trail's own write, `ProjectEditPage`'s `rename`). */
export const MEMBER_MANAGED_KEYS: readonly string[] = [
  'default_persona', 'agent_instructions', 'default_agent_id', 'default_provider',
]

/** Whether `admin` is Jaah's operator, acting as an org included (whose payload reads `is_operator` false). */
export function isJaahOperator(admin: Admin | null | undefined): boolean {
  return admin?.is_operator === true || Boolean(admin?.acting_as)
}

/** Whether `admin` sees `project` as a Jaah-managed one: anyone but the operator. */
export function lockedForMember(project: Pick<Project, 'jaah_managed'> | null | undefined, admin: Admin | null | undefined): boolean {
  return project?.jaah_managed === true && !isJaahOperator(admin)
}

/** A member's Save body: `MEMBER_MANAGED_KEYS` only, and `repos` while transferring the repository. */
export function memberBody(body: Record<string, unknown>, transferring: boolean): Record<string, unknown> {
  return Object.fromEntries(Object.entries(body).filter(
    ([key]) => MEMBER_MANAGED_KEYS.includes(key) || (transferring && key === 'repos'),
  ))
}
