import { type JSX, type ReactNode, useRef, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import { ApiError } from '../api'
import { useAuth } from '../auth'
import { useSubmit } from '../hooks/useSubmit'
import {
  executeTransfer, manifestChanges, planTransfer, refusalOf, useTransferKinds,
  type Manifest, type TransferKind, type TransferSubject,
} from '../lib/orgTransfer'
import { countOf } from '../lib/humanize'
import type { Org } from '../types'
import AdvancedSection from '../ui/AdvancedSection'
import Banner from '../ui/Banner'
import type { OverflowItem } from '../ui/feed/types'
import FormDialog from '../ui/FormDialog'
import FormField from '../ui/FormField'
import { tapTargetSx } from '../ui/listStyles'
import { OPERATOR_PLANE_SLUG, OrgSelect } from '../ui/OrgSelect'
import TypedNameConfirm, { typedNameMatches } from '../ui/TypedNameConfirm'

/* THE OPERATOR'S "CHANGE ORG…" — one dialog for every movable kind, for one entity or a selection of one kind:
 *
 *   1. pick the target org (the current one left out; the operator plane refused, with its reason);
 *   2. read the server's manifest — Warnings (what the move would break: shown, never stopping it),
 *      Released, Left behind, Dropped (each visible without expanding: they are what the move costs), then Moves
 *      by type (expandable), Reassigned and the notes. A selection is planned all at once — one line each: its
 *      refusal (same_org, …) or what its move costs, the full manifest a click away;
 *   3. type the entity's name — "Move <name> from <A> to <B>" — or, for a selection, the COUNT that can move —
 *      "Move <n> <kind>s from their orgs to <B>"; Move is disabled with its reason beside it;
 *   4. the result, linking to the entity — or the server's reason, with nothing changed (all-or-nothing). A
 *      selection moves each in turn, its own manifest's fingerprint and name sent; one refusal (a
 *      `plan_changed` included) is that row's failure and the run goes on; the result counts and lists what
 *      failed in the server's words.
 *
 * For one entity a 409 `plan_changed` re-plans in place and says what changed. Every rule is the server's
 * (`routes/org_transfer.py`); nothing here is recomputed. The target picker is the console's one org control,
 * `ui/OrgSelect`, given the current org to leave out and the plane rule (`operatorPlaneRefusal`). */

/** Kinds whose rule lets the operator plane be a move's SOURCE or TARGET (`tenancy/transfer_objects.py`,
 *  `operator_plane=True`): an automation, and the schedule under it. For every other kind the server refuses
 *  both (`_build`, `_refuse_target`), so the plane is no target and its own rows offer no move. */
const OPERATOR_PLANE_TARGETS: ReadonlySet<TransferKind> = new Set(['automation', 'cron'])

/** Kinds the operator plane may be the SOURCE of only (`out_of_operator_plane=True`, `tenancy/transfer_node.py`): the
 *  operator allocating its own machine to a tenant (`app/node_allocation.py`). Never a target. */
const OUT_OF_OPERATOR_PLANE: ReadonlySet<TransferKind> = new Set(['node', 'host'])

const OPERATOR_PLANE_REASON = 'The operator plane holds no tenant data'

/** `ui/OrgSelect`'s `optionDisabled` for a move of `kind`: the operator plane is offered only to a kind whose rule
 *  allows it, else shown refused with the reason. Also the credentials roster's bulk Change org (`CredentialBulk`). */
export const operatorPlaneRefusal = (kind: TransferKind) => (org: Org): string | null =>
  org.is_operator_plane && !OPERATOR_PLANE_TARGETS.has(kind) ? OPERATOR_PLANE_REASON : null

/** A subject's plan: the server's manifest, or why it refused to make one. */
type Plan = { manifest: Manifest } | { refusal: string }

/** What moved (each move's manifest, as the server answered) and, for a selection, what did not. */
type Outcome = { moved: Manifest[]; failures: Array<{ name: string; detail: string }> }

/** A same-origin path from the server (a warning's link), or null: a `RouterLink` follows an absolute URL
 *  natively, `javascript:` included, so nothing else may reach `to`. */
const internalPath = (v: string | null | undefined): string | null => (v && /^\/(?![/\\])/.test(v) ? v : null)

/** "3 warnings · 2 released · 1 dropped" — the non-zero parts; "no cost" when there are none. */
function costOf(m: Manifest): string {
  const parts = [
    [m.warnings.length, m.warnings.length === 1 ? 'warning' : 'warnings'],
    [m.released.length, 'released'],
    [m.left_behind.length, 'left behind'],
    [m.dropped.length, 'dropped'],
  ] as const
  const named = parts.filter(([n]) => n > 0).map(([n, label]) => `${n} ${label}`)
  return named.length ? named.join(' · ') : 'no cost'
}

const kindLabel = (kind: TransferKind, n: number) => `${kind.replace('_', ' ')}${n === 1 ? '' : 's'}`

export function ChangeOrgDialog({
  kind,
  subject,
  onClose,
  onMoved,
}: {
  kind: TransferKind
  /** One entity, or a selection of them (one kind, one target org). */
  subject: TransferSubject | TransferSubject[]
  onClose: () => void
  /** After a move, with how many the server refused — a caller keeps those picked; a surface whose rows are
   *  not react-query reads re-reads them here. */
  onMoved?: (outcome: { moved: number; failed: number }) => void
}): JSX.Element {
  const queryClient = useQueryClient()
  const subjects = Array.isArray(subject) ? subject : [subject]
  // One entity: the typed-name confirm, its manifest in full, a `plan_changed` re-planned in place.
  const single = Array.isArray(subject) ? null : subject
  // Left out of the picker only when every row names the same org; a mixed selection keeps the list whole and
  // the server's `same_org` refusal marks the rows already there. One entity without an org: the first plan
  // (or a `same_org` refusal) names it.
  const [currentOrgId, setCurrentOrgId] = useState<string | null>(
    subjects.length > 0 && subjects.every(s => s.org?.id && s.org.id === subjects[0].org?.id) ? subjects[0].org!.id : null,
  )
  const [plans, setPlans] = useState<Record<string, Plan>>({})
  const [planError, setPlanError] = useState('')
  const { busy, formError: error, setFormError: setError, run } = useSubmit()
  const [changed, setChanged] = useState<string[] | null>(null)
  const [progress, setProgress] = useState<{ i: number; n: number } | null>(null)
  const [outcome, setOutcome] = useState<Outcome | null>(null)
  const setPlan = (s: TransferSubject, plan: Plan) => setPlans(prev => ({ ...prev, [String(s.id)]: plan }))
  const { target, typed, setTyped, planning, pick, unpick } = useTargetPlan(
    async (org, live) => {
      setPlans({})
      setPlanError('')
      setChanged(null)
      setError('')
      await Promise.all(subjects.map(async s => {
        let plan: Plan
        try {
          plan = { manifest: await planTransfer(kind, s.id, org.id) }
        } catch (e) {
          if (!live()) return
          const refusal = e instanceof ApiError ? refusalOf(e.body) : null
          if (single && refusal?.reason === 'same_org') {
            // Learned only now (the row carried no org): drop it from the picker.
            setCurrentOrgId(org.id)
            unpick()
            setPlanError(`${s.name} is already in ${org.name}. Pick another org.`)
            return
          }
          plan = { refusal: refusal?.detail ?? (e instanceof Error ? e.message : single ? 'Could not plan the move.' : 'could not plan the move') }
        }
        if (!live()) return
        if (single) {
          if ('manifest' in plan) setCurrentOrgId(plan.manifest.from_org.id)
          else setPlanError(plan.refusal)
        }
        setPlan(s, plan)
      }))
    },
    () => {
      setPlans({})
      setPlanError('')
    },
  )

  const movable = subjects.flatMap(s => {
    const p = plans[String(s.id)]
    return p && 'manifest' in p ? [{ subject: s, manifest: p.manifest }] : []
  })
  const refused = Object.values(plans).filter(p => 'refusal' in p).length
  const n = movable.length
  const manifest = single ? movable[0]?.manifest ?? null : null

  function move() {
    if (n === 0) return
    return run('the move failed', async () => {
      const moved: Manifest[] = []
      const failures: Outcome['failures'] = []
      for (const [i, { subject: s, manifest: m }] of movable.entries()) {
        if (!single) setProgress({ i: i + 1, n })
        try {
          // The org the plan on screen names: the one the operator read and confirmed. A selection's confirm is
          // the typed COUNT, so the server's typed-name guard (`tenancy/transfer.py::execute`) is met with each
          // entity's own name — the one its manifest carries.
          const done = await executeTransfer(kind, s.id, m.to_org.id, m.fingerprint, single ? typed.trim() : m.name)
          moved.push(done.manifest)
        } catch (e) {
          if (single) throw e
          const refusal = e instanceof ApiError ? refusalOf(e.body) : null
          /* A `confirm_name` refusal means the node was RENAMED between the plan and the move (the guard reads the
             name before the fingerprint): a selection cannot type a name, so say what happened and what to do. */
          const detail = refusal?.reason === 'confirm_name'
            ? 'its name changed since it was planned — pick the org again to re-plan'
            : refusal?.detail ?? (e instanceof Error ? e.message : 'the move failed')
          failures.push({ name: m.name, detail })
        }
      }
      setProgress(null)
      setOutcome({ moved, failures })
      // Every list and page may show an entity or a child of it: all of them re-read.
      void queryClient.invalidateQueries()
      onMoved?.({ moved: moved.length, failed: failures.length })
    }, {
      mapError: (e, fallback) => {
        const refusal = e instanceof ApiError ? refusalOf(e.body) : null
        if (single && refusal?.reason === 'plan_changed' && refusal.manifest && manifest) {
          setChanged(manifestChanges(manifest, refusal.manifest))
          setPlan(single, { manifest: refusal.manifest })
          return ''
        }
        if (single && refusal?.manifest) setPlan(single, { manifest: refusal.manifest })
        return `Nothing moved: ${refusal?.detail ?? (e instanceof Error ? e.message : fallback)}.`
      },
    })
  }

  if (outcome) {
    const m = outcome.moved[0]
    if (single && m) {
      const children = m.moves.reduce((c, g) => c + g.count, 0)
      return (
        <FormDialog title={`Moved ${m.name}`} submitLabel="Close" hideCancel onSubmit={onClose} onClose={onClose}
          testId="change-org-dialog" submitTestId="change-org-close">
          <Banner severity="success" testId="change-org-result">
            {m.name} moved from {m.from_org.name} to {m.to_org.name}
            {children > 0 ? ` — ${countOf(children, 'item')} in all` : ''}.
          </Banner>
          {internalPath(single.href) && (
            <Link component={RouterLink} to={internalPath(single.href)!} onClick={onClose} data-testid="change-org-open" sx={tapTargetSx}>
              Open {m.name}
            </Link>
          )}
        </FormDialog>
      )
    }
    const of = outcome.moved.length + outcome.failures.length
    const title = `Moved ${outcome.moved.length} of ${of}`
    return (
      <FormDialog title={title} submitLabel="Close" hideCancel onSubmit={onClose} onClose={onClose}
        testId="change-org-dialog" submitTestId="change-org-close">
        <Banner severity={outcome.failures.length ? 'warning' : 'success'} testId="change-org-result">
          {title} {kindLabel(kind, of)} to {target?.name}.
          {outcome.failures.length > 0 && (
            <Box component="ul" sx={{ m: 0, mt: 0.5, pl: 2.5, overflowWrap: 'anywhere' }}>
              {/* By position: names are unique per org only, and a selection may span orgs. */}
              {outcome.failures.map((f, index) => <li key={index}>{f.name}: {f.detail}</li>)}
            </Box>
          )}
        </Banner>
      </FormDialog>
    )
  }

  const confirmName = single ? manifest?.name ?? '' : String(n)
  const confirmOk = n > 0 && typedNameMatches(typed, confirmName)
  const disabledReason = !target ? 'Pick an org'
    : planning ? 'Planning…'
      : n === 0 ? (single ? 'Pick another org' : 'Nothing can move')
        : !confirmOk ? `Type ${confirmName} to confirm`
          : undefined
  // The typed-confirm gate: the field's own line already says it, so no caption repeats it beside Move.
  const confirming = Boolean(target) && !planning && n > 0 && !confirmOk

  return (
    <FormDialog
      title={`Change org — ${single ? manifest?.name ?? single.name : `${subjects.length} ${kindLabel(kind, subjects.length)}`}`}
      submitLabel="Move"
      busyLabel="Moving…"
      busy={busy}
      error={error}
      destructive
      onSubmit={() => void move()}
      onClose={onClose}
      submitDisabledReason={disabledReason}
      secondaryAction={disabledReason && !confirming && (
        <Typography variant="caption" data-testid="change-org-disabled-reason"
          sx={{ color: 'text.secondary', mr: 1, overflowWrap: 'anywhere' }}>
          {disabledReason}
        </Typography>
      )}
      fullScreenOnPhone
      testId="change-org-dialog"
      submitTestId="change-org-submit"
      errorTestId="change-org-error"
    >
      {/* The target: the current org left out; the plane refused with its reason for a kind whose rule refuses it.
          `change-org-target` is the lookup's root (`ui/AsyncLookup`); its combobox is the input. */}
      <FormField label="Move to" help="Org that receives it; the move's costs show below.">
        <OrgSelect
          id="change-org-target"
          value={target?.id ?? null}
          valueLabel={target?.name}
          onChange={(_id, org) => pick(org)}
          excludeOrgId={currentOrgId}
          optionDisabled={operatorPlaneRefusal(kind)}
          disabled={busy}
          testId="change-org-target"
        />
      </FormField>
      {planError && <Banner severity="error" testId="change-org-plan-error">{planError}</Banner>}
      {single && planning && <Typography variant="body2" sx={{ color: 'text.secondary' }}>Working out what moves…</Typography>}

      {single && manifest && (
        <>
          <Typography variant="body2" data-testid="change-org-sentence">
            {/* A host's display name can differ from its node's name, which is what the confirm wants typed. */}
            Move{' '}
            {kind === 'host' && single.name && single.name !== manifest.name
              ? <><strong>{single.name}</strong> (node <strong>{manifest.name}</strong>)</>
              : <strong>{manifest.name}</strong>}{' '}from <strong>{manifest.from_org.name}</strong> to{' '}
            <strong>{manifest.to_org.name}</strong>.
          </Typography>
          {changed && (
            <Banner severity="warning" testId="change-org-changed">
              What this move would do changed since you reviewed it
              {changed.length ? ` (${changed.join('; ')})` : ''}. Review it and move again.
            </Banner>
          )}
          <ManifestView manifest={manifest} />
        </>
      )}

      {!single && target && (
        <Box component="ul" sx={{ m: 0, pl: 0, listStyle: 'none', typography: 'body2', overflowWrap: 'anywhere' }}>
          {subjects.map(s => {
            const p = plans[String(s.id)]
            return (
              <li key={String(s.id)} data-testid="change-org-row">
                {!p ? <><strong>{s.name}</strong> — planning…</>
                  : 'refusal' in p
                    ? <><strong>{s.name}</strong> — <Box component="span" sx={{ color: 'error.main' }}>can't move: {p.refusal}</Box></>
                    : (
                      <AdvancedSection dense label={<><strong>{p.manifest.name}</strong> — {costOf(p.manifest)}</>}
                        testId="change-org-row-plan">
                        <Box sx={{ pl: 2, pt: 0.5 }}><ManifestView manifest={p.manifest} /></Box>
                      </AdvancedSection>
                    )}
              </li>
            )
          })}
        </Box>
      )}
      {progress && <Typography variant="body2" sx={{ color: 'text.secondary' }} role="status">Moving {progress.i} of {progress.n}…</Typography>}
      {!single && target && !planning && n > 0 && (
        <Typography variant="body2" data-testid="change-org-sentence">
          Move <strong>{n}</strong> {kindLabel(kind, n)} from {n === 1 ? 'its org' : 'their orgs'} to <strong>{target.name}</strong>.
          {refused > 0 && <> {refused} can't move.</>}
        </Typography>
      )}

      {/* Not focused on arrival: on a phone that opens the keyboard and scrolls the cost out of view. */}
      {(single ? manifest !== null : target && !planning && n > 0) && (
        <TypedNameConfirm name={confirmName} value={typed} onChange={setTyped} action="the move"
          testId="change-org-confirm" autoFocus={false} />
      )}
    </FormDialog>
  )
}

/** The pick → plan state: the target, the typed confirm (cleared on every pick), whether a plan round is in
 *  flight, and the round counter — a slower answer to an earlier pick must never land on screen. `plan(org, live)`
 *  plans a pick and writes only while `live()` says the round is still the latest; `clear()` drops the plans when
 *  the pick is cleared. `unpick` empties the picker (a `same_org` answer). */
function useTargetPlan(plan: (org: Org, live: () => boolean) => Promise<void>, clear: () => void): {
  target: Org | null
  typed: string
  setTyped: (v: string) => void
  planning: boolean
  pick: (org: Org | null) => void
  unpick: () => void
} {
  const [target, setTarget] = useState<Org | null>(null)
  const [typed, setTyped] = useState('')
  const [planning, setPlanning] = useState(false)
  const planSeq = useRef(0)
  const pick = (org: Org | null) => {
    const seq = ++planSeq.current
    const live = () => seq === planSeq.current
    setTarget(org)
    setTyped('')
    if (!org) {
      setPlanning(false)
      clear()
      return
    }
    setPlanning(true)
    void plan(org, live).finally(() => { if (live()) setPlanning(false) })
  }
  return { target, typed, setTyped, planning, pick, unpick: () => setTarget(null) }
}

/** The manifest, in the order a reader must weigh it: what it would break, what it costs, then what goes. */
function ManifestView({ manifest: m }: { manifest: Manifest }): JSX.Element {
  const moved = m.moves.reduce((n, g) => n + g.count, 0)
  return (
    <Box data-testid="change-org-manifest" sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
      {m.warnings.length > 0 && (
        <Group title="Warnings" count={m.warnings.length} testId="manifest-warnings" tone="warning.main">
          {m.warnings.map(w => <WarningItem key={w.code + w.message} warning={w} />)}
        </Group>
      )}
      <Group title="Released" count={m.released.length} testId="manifest-released"
        tone={m.released.length ? 'warning.main' : undefined}>
        {m.released.map(r => (
          <li key={r.node + r.slot}>
            Slot {r.slot} on {r.node}{r.task_title ? ` (${r.task_title})` : ''} —{' '}
            {r.uncommitted === true ? <strong>uncommitted work is lost</strong>
              : r.uncommitted === false ? 'nothing uncommitted' : 'uncommitted work unknown'}
          </li>
        ))}
      </Group>
      <Group title="Left behind" count={m.left_behind.length} testId="manifest-left-behind"
        tone={m.left_behind.length ? 'warning.main' : undefined}>
        {m.left_behind.map(l => (
          <li key={l.type + l.name}>
            {l.type} {l.name} stays in {m.from_org.name}
            {l.still_used_by.length ? `, still used by ${l.still_used_by.join(', ')}` : ''}
          </li>
        ))}
      </Group>
      <Group title="Dropped" count={m.dropped.length} testId="manifest-dropped"
        tone={m.dropped.length ? 'warning.main' : undefined}>
        {m.dropped.map(d => <li key={d.type + d.name + d.reason}>{d.type} {d.name}: {d.reason}</li>)}
      </Group>
      <Group title="Moves" count={moved} testId="manifest-moves">
        {m.moves.map(g => (
          <li key={g.type}>
            <AdvancedSection dense label={`${g.label}: ${g.count}`} testId="manifest-moves-names">
              <Box component="span" sx={{ display: 'block', pl: 2, color: 'text.secondary', overflowWrap: 'anywhere' }}>
                {g.names.join(', ')}{g.count > g.names.length ? `, and ${g.count - g.names.length} more` : ''}
              </Box>
            </AdvancedSection>
          </li>
        ))}
      </Group>
      {m.reassigned.length > 0 && (
        <Group title="Reassigned" count={m.reassigned.length} testId="manifest-reassigned">
          {m.reassigned.map(r => (
            <li key={r.type + r.name}>{r.type} {r.name}: {r.from_person} → {r.to_person}</li>
          ))}
        </Group>
      )}
      {m.notes.length > 0 && (
        <Box component="ul" data-testid="manifest-notes" sx={{ m: 0, pl: 2.5, color: 'text.secondary', typography: 'body2' }}>
          {m.notes.map(n => <li key={n}>{n}</li>)}
        </Box>
      )}
    </Box>
  )
}

function WarningItem({ warning: w }: { warning: Manifest['warnings'][number] }): JSX.Element {
  return (
    <li>
      {w.message}
      {internalPath(w.link) && <> — <Link component={RouterLink} to={internalPath(w.link)!}>open</Link></>}
    </li>
  )
}

function Group({ title, count, testId, tone, children }: {
  title: string
  count: number
  testId: string
  tone?: string
  children: ReactNode
}): JSX.Element {
  return (
    <Box data-testid={testId}>
      <Typography variant="subtitle2" sx={{ color: tone }}>{title} ({count})</Typography>
      {count === 0
        ? <Typography variant="body2" sx={{ color: 'text.secondary' }}>None</Typography>
        : <Box component="ul" sx={{ m: 0, pl: 2.5, typography: 'body2', overflowWrap: 'anywhere' }}>{children}</Box>}
    </Box>
  )
}

/* ── THE ONE WIRING EVERY SURFACE USES ──
 * `menuItem(entity)` for a row's ⋯, `button(entity)` for a detail page, `open(selection)` for a list's bulk bar,
 * and `dialog` rendered once by the page. Every control exists only for the operator and only for a kind the
 * server lists (`GET …/transfers/kinds`). */
export function useChangeOrgAction(
  kind: TransferKind,
  { onMoved }: { onMoved?: (outcome: { moved: number; failed: number }) => void } = {},
): {
  available: boolean
  /** One entity, or a selection (an empty one opens nothing). */
  open: (subject: TransferSubject | TransferSubject[]) => void
  menuItem: (subject: TransferSubject) => OverflowItem | null
  /** `strip`: a record tab strip's verb, a text button at the tap floor so it reads lighter than the tabs. */
  button: (subject: TransferSubject, opts?: { strip?: boolean }) => JSX.Element | null
  dialog: JSX.Element | null
} {
  const { admin } = useAuth()
  const available = useTransferKinds(admin?.is_operator === true).has(kind)
  const [subject, setSubject] = useState<TransferSubject | TransferSubject[] | null>(null)
  // An operator-plane row of a kind that never leaves the plane: the server would only refuse.
  const offered = (s: TransferSubject) => available
    && (s.org?.slug !== OPERATOR_PLANE_SLUG || OPERATOR_PLANE_TARGETS.has(kind) || OUT_OF_OPERATOR_PLANE.has(kind))
  return {
    available,
    open: s => setSubject(Array.isArray(s) && !s.length ? null : s),
    menuItem: s => offered(s)
      ? { key: 'change-org', icon: '⇄', label: 'Change org…', ariaLabel: `Change the org of ${s.name}`, onClick: () => setSubject(s) }
      : null,
    button: (s, opts) => offered(s)
      ? (
        <Button
          variant={opts?.strip ? 'text' : 'outlined'}
          size="small"
          onClick={() => setSubject(s)}
          data-testid="change-org-button"
          sx={tapTargetSx}
        >
          Change org…
        </Button>
      )
      : null,
    dialog: subject
      ? <ChangeOrgDialog kind={kind} subject={subject} onClose={() => setSubject(null)} onMoved={onMoved} />
      : null,
  }
}
