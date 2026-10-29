// The WRITE half of a slot's Title and Description (one `PUT .../title` endpoint for both) and what the
// CP's answer means; `lib/worktree.ts::resolveTitle` is the read half. Shared by the Worktrees cell editor
// and the Console overlay header so they cannot diverge on a stored-but-not-renamed reply. Validates
// nothing: `apps/api/app/slot_title.py` is the authority.
import { apiDetail, put } from '../api'

// `slot_title.FREE_TITLE`, the one place that word is decided; rendered as-is.
export const FREE_TITLE = 'free'

// TWO SEVERITIES (AC6.2): a refused edit stored nothing (error); a stored edit whose tmux window could not
// be renamed (offline node) is partial — a red "Title saved" would read as a failed save.
export type TitleNotice = { text: string; kind: 'error' | 'info' }

// `nodeId` is required: the API defaults an omitted `node_id` to the CP's own node, silently retitling the
// LOCAL `acme#3`. A caller that cannot name the node must not offer the edit. `projectId` and the node uuid
// address the slot; `project` (the name) only words the notices.
export type SlotRef = { project: string; projectId: number; number: number; nodeId: string }

// `title` is the RESOLVED Title (differs from the typed value on a CLEAR, which resolves to
// `{TaskId}|{Task Description}` or `free`); `null` = never landed, drop the optimistic paint.
export type SavedTitle = { title: string | null; notice: TitleNotice | null }

// The Description's twin; `null` means the same. `TitleNotice` is reused (severity belongs to the endpoint).
export type SavedDescription = { description: string | null; notice: TitleNotice | null }

// The reply for either field. Paint only the field you sent: echoing the other would overwrite a
// concurrent edit with stale text.
type SlotFieldsReply = {
  title: string
  description: string
  stored: boolean
  renamed: boolean
  rename_error?: string | null
}

// One URL: body `{title?, description?}`, an OMITTED key leaves that field alone (`""` is a real clear).
function slotFieldsPath(slot: SlotRef): string {
  return `/api/worktrees/${slot.projectId}/${slot.number}/title`
    + `?node_id=${encodeURIComponent(slot.nodeId)}`
}

// The editor's starting value. `free` opens EMPTY (BA R6): prefilling the CP's word makes the operator
// delete it first; every other Title prefills.
export function titleEditorSeed(title: string): string {
  return title === FREE_TITLE ? '' : title
}

// The Title as the Slots row and drawer SHOW it: `free` is the occupancy word, which the row disc and the
// drawer chip already say, so it reads as the panel's absence mark `—` instead of a second `free`. The CP's
// composed `{TaskId}|{Task Description}` reads as the console names a task, `#5 Title`.
export function titleShown(title: string): string {
  if (title === FREE_TITLE) return '—'
  const composed = /^(\d+)\|(.*)$/s.exec(title)
  return composed ? `#${composed[1]} ${composed[2]}` : title
}

// Save a Title (BA R6/AC6.1-AC6.4). The CP is the sole author: it persists, THEN best-effort renames the
// live tmux window (the old direct `rename_tmux` command stored nothing and lost edits on offline or free
// slots). An empty `value` is a CLEAR, not a cancel.
export async function saveSlotTitle(slot: SlotRef, value: string): Promise<SavedTitle> {
  const label = `${slot.project} #${slot.number}`
  try {
    // `description` not sent: echoing the one held here would overwrite a newer save.
    const result = await put<SlotFieldsReply>(slotFieldsPath(slot), { title: value })
    if (!result.stored) {
      return {
        title: result.title,
        notice: { kind: 'error', text: `Title not stored for ${label} — the CP has no row for that slot yet` },
      }
    }
    if (!result.renamed) {
      return {
        title: result.title,
        notice: {
          kind: 'info',
          text: `Title saved. The live tmux window was not renamed — ${result.rename_error || 'the node did not confirm'}`,
        },
      }
    }
    return { title: result.title, notice: null }
  } catch (err) {
    // Never landed: `null`, with the 422 reason (reserved/control-character Title,
    // `apps/api/app/slot_title.py`) verbatim.
    return { title: null, notice: { kind: 'error', text: apiDetail(err, `Could not set the Title for ${label}`) } }
  }
}

// Save a Description — the same PUT, but the `renamed` half of the answer is not this edit's business:
// the CP renames only for a SUPPLIED title (`apps/api/app/routes/pool_slots.py`), so reporting it would
// name an unrelated failure. An empty `value` is a clear, stored as `""` (no resolved fallback).
export async function saveSlotDescription(slot: SlotRef, value: string): Promise<SavedDescription> {
  const label = `${slot.project} #${slot.number}`
  try {
    // `title` omitted: re-sending a held value would re-store a CP-composed Title as operator-set.
    const result = await put<SlotFieldsReply>(slotFieldsPath(slot), { description: value })
    if (!result.stored) {
      return {
        description: result.description,
        notice: { kind: 'error', text: `Description not stored for ${label} — the CP has no row for that slot yet` },
      }
    }
    return { description: result.description, notice: null }
  } catch (err) {
    // `null`: a refused Description still showing its text would look exactly like success.
    return {
      description: null,
      notice: { kind: 'error', text: apiDetail(err, `Could not set the Description for ${label}`) },
    }
  }
}
