// The WRITE half of a pool clone's owner (`PUT /api/worktrees/{project_id}/clone-owner`). A (node, project)
// clone authenticates to GitHub as its earliest human declarer
// (`apps/api/app/git_access.py::resolve_for_worktree_clone`); this reassigns every slot of that pair.
import { apiDetail, put } from '../api'
import { actAsOnce } from './actAs'
import type { OrgRef } from '../types'
import type { TitleNotice } from './slotTitle'

type CloneOwnerReply = {
  node_id: string
  project: string
  owner_email: string
  slots: number
  git_access_warning?: string | null
}

// `nodeId` (the node uuid) is required (as `SlotRef.nodeId`): an omitted `node_id` defaults to the CP's own node.
// `projectId` addresses the call; `project` only words the notice. `otherOrg`: the slot's org when it is not the
// operator's own — the write goes as that org for this one request (`actAsOnce`), since an operator's
// write naming another org's project is refused (`authz/enforce.py::OtherOrgDenial`).
export type CloneRef = { project: string; projectId: number; nodeId: string; nodeLabel: string; otherOrg?: OrgRef | null }

// `ok` = the reassignment landed; a landed one may carry an `info` notice (the new owner has not connected
// GitHub yet).
export async function saveCloneOwner(
  clone: CloneRef, userId: string,
): Promise<{ ok: boolean; notice: TitleNotice }> {
  const label = `${clone.project} on ${clone.nodeLabel}`
  try {
    const reply = await put<CloneOwnerReply>(
      `/api/worktrees/${clone.projectId}/clone-owner`
        + `?node_id=${encodeURIComponent(clone.nodeId)}`,
      { user_id: userId },
      actAsOnce(clone.otherOrg ?? null),
    )
    if (reply.git_access_warning) {
      return { ok: true, notice: { kind: 'error', text: reply.git_access_warning } }
    }
    return {
      ok: true,
      notice: {
        kind: 'info',
        text: `${label} now clones as ${reply.owner_email}. Run Sync.`,
      },
    }
  } catch (err) {
    return { ok: false, notice: { kind: 'error', text: apiDetail(err, `Could not change the clone owner of ${label}`) } }
  }
}
