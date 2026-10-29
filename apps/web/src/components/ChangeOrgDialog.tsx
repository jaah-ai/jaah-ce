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
import type { Org } from '../types'
import Banner from '../ui/Banner'
import type { OverflowItem } from '../ui/feed/types'
import FormDialog from '../ui/FormDialog'
import { tapTargetSx } from '../ui/listStyles'
import { OPERATOR_PLANE_SLUG, OrgSelect } from '../ui/OrgSelect'
import TypedNameConfirm, { typedNameMatches } from '../ui/TypedNameConfirm'

/* THE OPERATOR'S "CHANGE ORG…" — one dialog for every movable kind:
 *
 *   1. pick the target org (the current one left out; the operator plane refused, with its reason);
 *   2. read the server's manifest — Warnings (what the move would break: shown, never stopping it),
 *      Released, Left behind, Dropped (each visible without expanding: they are what the move costs), then Moves
 *      by type (expandable), Reassigned and the notes;
 *   3. type the entity's name — "Move <name> from <A> to <B>", disabled with its reason beside it;
 *   4. the result, linking to the entity — or the server's reason, with nothing changed (all-or-nothing).
 *
 * A 409 `plan_changed` re-plans in place and says what changed. Every rule is the server's
 * (`routes/org_transfer.py`); nothing here is recomputed. The bulk dialog, `BulkChangeOrgDialog.tsx`, moves a
 * selection the same way and shares what is exported here: the frame (`ChangeOrgFrame`), the pick → plan state
 * (`useTargetPlan`) and the manifest view (`ManifestView`). The target picker is the console's one org control,
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

type Outcome = { manifest: Manifest }

/** A same-origin path from the server (a warning's link), or null: a `RouterLink` follows an absolute URL
 *  natively, `javascript:` included, so nothing else may reach `to`. */
const internalPath = (v: string | null | undefined): string | null => (v && /^\/(?![/\\])/.test(v) ? v : null)

export function ChangeOrgDialog({
  kind,
  subject,
  onClose,
  onMoved,
}: {
  kind: TransferKind
  subject: TransferSubject
  onClose: () => void
  /** After a move: a surface whose rows are not react-query reads re-reads them here. */
  onMoved?: () => void
}): JSX.Element {
  const queryClient = useQueryClient()
  // The current org: from the row when it carries one, else from the first plan (or a `same_org` refusal).
  const [currentOrgId, setCurrentOrgId] = useState<string | null>(subject.org?.id ?? null)
  const [manifest, setManifest] = useState<Manifest | null>(null)
  const [planError, setPlanError] = useState('')
  const { busy, formError: error, setFormError: setError, run } = useSubmit()
  const [changed, setChanged] = useState<string[] | null>(null)
  const [outcome, setOutcome] = useState<Outcome | null>(null)
  const { target, typed, setTyped, planning, pick, unpick } = useTargetPlan(
    async (org, live) => {
      setPlanError('')
      setManifest(null)
      setChanged(null)
      setError('')
      try {
        const next = await planTransfer(kind, subject.id, org.id)
        if (!live()) return
        setCurrentOrgId(next.from_org.id)
        setManifest(next)
      } catch (e) {
        if (!live()) return
        const refusal = e instanceof ApiError ? refusalOf(e.body) : null
        if (refusal?.reason === 'same_org') {
          // Learned only now (the row carried no org): drop it from the picker.
          setCurrentOrgId(org.id)
          unpick()
          setPlanError(`${subject.name} is already in ${org.name}. Pick another org.`)
        } else {
          setPlanError(refusal?.detail ?? (e instanceof Error ? e.message : 'Could not plan the move.'))
        }
      }
    },
    () => {
      setManifest(null)
      setPlanError('')
    },
  )

  function move() {
    if (!manifest) return
    return run('the move failed', async () => {
      // The org the plan on screen names: the one the operator read and confirmed.
      const done = await executeTransfer(kind, subject.id, manifest.to_org.id, manifest.fingerprint, typed.trim())
      setOutcome({ manifest: done.manifest })
      // Every list and page may show the entity or a child of it: all of them re-read.
      void queryClient.invalidateQueries()
      onMoved?.()
    }, {
      mapError: (e, fallback) => {
        const refusal = e instanceof ApiError ? refusalOf(e.body) : null
        if (refusal?.reason === 'plan_changed' && refusal.manifest) {
          setChanged(manifestChanges(manifest, refusal.manifest))
          setManifest(refusal.manifest)
          return ''
        }
        if (refusal?.manifest) setManifest(refusal.manifest)
        return `Nothing moved: ${refusal?.detail ?? (e instanceof Error ? e.message : fallback)}.`
      },
    })
  }

  if (outcome) {
    const m = outcome.manifest
    const children = m.moves.reduce((n, g) => n + g.count, 0)
    return (
      <FormDialog title={`Moved ${m.name}`} submitLabel="Close" hideCancel onSubmit={onClose} onClose={onClose}
        testId="change-org-dialog" submitTestId="change-org-close">
        <Banner severity="success" testId="change-org-result">
          {m.name} moved from {m.from_org.name} to {m.to_org.name}
          {children > 0 ? ` — ${children} item${children === 1 ? '' : 's'} in all` : ''}.
        </Banner>
        {internalPath(subject.href) && (
          <Link component={RouterLink} to={internalPath(subject.href)!} onClick={onClose} data-testid="change-org-open" sx={tapTargetSx}>
            Open {m.name}
          </Link>
        )}
      </FormDialog>
    )
  }

  const nameOk = manifest !== null && typedNameMatches(typed, manifest.name)
  const disabledReason = !target ? 'Pick an org'
    : planning ? 'Planning…'
      : !manifest ? 'Pick another org'
        : !nameOk ? `Type ${manifest.name} to confirm`
          : undefined

  return (
    <ChangeOrgFrame
      kind={kind}
      title={`Change org — ${manifest?.name ?? subject.name}`}
      prefix="change-org"
      excludeOrgId={currentOrgId}
      target={target}
      onPick={pick}
      busy={busy}
      error={error}
      disabledReason={disabledReason}
      confirming={Boolean(target) && !planning && manifest !== null && !nameOk}
      onSubmit={() => void move()}
      onClose={onClose}
    >
      {planError && <Banner severity="error" testId="change-org-plan-error">{planError}</Banner>}
      {planning && <Typography variant="body2" sx={{ color: 'text.secondary' }}>Working out what moves…</Typography>}

      {manifest && (
        <>
          <Typography variant="body2" data-testid="change-org-sentence">
            {/* A host's display name can differ from its node's name, which is what the confirm wants typed. */}
            Move{' '}
            {kind === 'host' && subject.name && subject.name !== manifest.name
              ? <><strong>{subject.name}</strong> (node <strong>{manifest.name}</strong>)</>
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
          {/* Not focused on arrival: on a phone that opens the keyboard and scrolls the cost out of view. */}
          <TypedNameConfirm name={manifest.name} value={typed} onChange={setTyped} action="the move"
            testId="change-org-confirm" autoFocus={false} />
        </>
      )}
    </ChangeOrgFrame>
  )
}

/** The pick → plan state both dialogs share: the target, the typed confirm (cleared on every pick), whether a
 *  plan round is in flight, and the round counter — a slower answer to an earlier pick must never land on screen.
 *  `plan(org, live)` plans a pick and writes only while `live()` says the round is still the latest; `clear()`
 *  drops the plans when the pick is cleared. `unpick` empties the picker (a `same_org` answer). */
export function useTargetPlan(plan: (org: Org, live: () => boolean) => Promise<void>, clear: () => void): {
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

/** The frame both dialogs share: the destructive Move dialog with the disabled reason beside Move — unless it
 *  is the typed-confirm gate (`confirming`), whose field's own line already says it — and the target picker
 *  first. `prefix` keys every test id: `<prefix>-dialog`, `-submit`, `-error`, `-disabled-reason`, `-target`. */
export function ChangeOrgFrame({
  kind,
  title,
  prefix,
  excludeOrgId,
  target,
  onPick,
  busy,
  error,
  disabledReason,
  confirming,
  onSubmit,
  onClose,
  children,
}: {
  kind: TransferKind
  title: string
  prefix: string
  excludeOrgId: string | null
  target: Org | null
  onPick: (org: Org | null) => void
  busy: boolean
  error?: string
  disabledReason: string | undefined
  confirming: boolean
  onSubmit: () => void
  onClose: () => void
  children: ReactNode
}): JSX.Element {
  return (
    <FormDialog
      title={title}
      submitLabel="Move"
      busyLabel="Moving…"
      busy={busy}
      error={error}
      destructive
      onSubmit={onSubmit}
      onClose={onClose}
      submitDisabledReason={disabledReason}
      secondaryAction={disabledReason && !confirming && (
        <Typography variant="caption" data-testid={`${prefix}-disabled-reason`}
          sx={{ color: 'text.secondary', mr: 1, overflowWrap: 'anywhere' }}>
          {disabledReason}
        </Typography>
      )}
      fullScreenOnPhone
      testId={`${prefix}-dialog`}
      submitTestId={`${prefix}-submit`}
      errorTestId={`${prefix}-error`}
    >
      {/* The target: the current org left out; the plane refused with its reason for a kind whose rule refuses it.
          `<prefix>-target` is the lookup's root (`ui/AsyncLookup`); its combobox is the input. */}
      <OrgSelect
        id={`${prefix}-target`}
        label="Move to"
        value={target?.id ?? null}
        valueLabel={target?.name}
        onChange={(_id, org) => onPick(org)}
        excludeOrgId={excludeOrgId}
        optionDisabled={operatorPlaneRefusal(kind)}
        disabled={busy}
        testId={`${prefix}-target`}
      />
      {children}
    </FormDialog>
  )
}

/** The manifest, in the order a reader must weigh it: what it would break, what it costs, then what goes. */
export function ManifestView({ manifest: m }: { manifest: Manifest }): JSX.Element {
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
            <Box component="details" sx={{ '& summary': { cursor: 'pointer' } }}>
              <summary>{g.label}: {g.count}</summary>
              <Box component="span" sx={{ display: 'block', pl: 2, color: 'text.secondary', overflowWrap: 'anywhere' }}>
                {g.names.join(', ')}{g.count > g.names.length ? `, and ${g.count - g.names.length} more` : ''}
              </Box>
            </Box>
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
 * `menuItem(entity)` for a row's ⋯, `button(entity)` for a detail page, and `dialog` rendered once by the page.
 * Both controls exist only for the operator and only for a kind the server lists (`GET …/transfers/kinds`). */
export function useChangeOrgAction(kind: TransferKind, { onMoved }: { onMoved?: () => void } = {}): {
  available: boolean
  open: (subject: TransferSubject) => void
  menuItem: (subject: TransferSubject) => OverflowItem | null
  /** `strip`: a record tab strip's verb, a text button at the tap floor so it reads lighter than the tabs. */
  button: (subject: TransferSubject, opts?: { strip?: boolean }) => JSX.Element | null
  dialog: JSX.Element | null
} {
  const { admin } = useAuth()
  const available = useTransferKinds(admin?.is_operator === true).has(kind)
  const [subject, setSubject] = useState<TransferSubject | null>(null)
  // An operator-plane row of a kind that never leaves the plane: the server would only refuse.
  const offered = (s: TransferSubject) => available
    && (s.org?.slug !== OPERATOR_PLANE_SLUG || OPERATOR_PLANE_TARGETS.has(kind) || OUT_OF_OPERATOR_PLANE.has(kind))
  return {
    available,
    open: setSubject,
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
